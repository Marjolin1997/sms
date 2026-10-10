"""M9-d: raportet kumulative të përdorimit financiar (Enterprise → Central). Pa HTTP në gjenerim; dërgimi te `deliver`.

**Snapshot:** `build_drafts` lexon NJË transaksion REPEATABLE READ vetëm-lexim (PostgreSQL): bilanci, holds, shumat e
ledger-it, grant-et, baseline dhe kursori vijnë nga e njëjta pamje (asnjë lexim "available tani, held 20 ms më vonë").
Çdo vlerë rillogaritet nga ledger-i i pandryshueshëm dhe tabelat e parave (s'ka numërues të ndryshueshëm të rinj).

**Ekuacioni** (shih `usage_v1`): gross = baseline_gross + grants_applied − grant_reversals − captured −
negative_adjustments − invoice_debits − other_debits + positive_local_credit, ku flukset janë ato PAS `ledger_max_id`
të baseline-it aktiv (ose gjithçka pa baseline). HOLD/RELEASE janë neto 0 mbi gross (lëvizin available↔held).

**Outbox:** `persist` shton raportin e ngrirë me `report_seq` monoton lokal per (enterprise, product, currency);
përmbajtje identike me raportin e fundit brenda heartbeat-it NUK dubloh. `deliver` është at-least-once me lease,
backoff dhe `superseded` (raportet kumulative të vjetra s'dërgohen kur ka më të reja në pritje). Dështimi i raportimit
NUK ndryshon kurrë wallet-in dhe NUK është në rrugën e dërgimit SMS."""

import logging
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import as_utc, utcnow
from app.models.enterprise import Enterprise
from app.models.money_authority import (
    BASELINE_ACTIVE,
    G_MISMATCH,
    G_RECON,
    G_UNMAPPED,
    MoneyBaseline,
    MoneyCursor,
    MoneyGrant,
)
from app.models.money_usage import (
    R_FAILED,
    R_PENDING,
    R_RETRY,
    R_SENDING,
    R_SENT,
    R_SUPERSEDED,
    UsageReport,
)
from app.models.wallet import EntryType, Hold, HoldStatus, LedgerEntry, Wallet
from app.services import money_authority as ma
from app.services.control_plane_client import (
    ControlPlaneClient,
    CpAuthError,
    CpError,
    CpForbidden,
    CpProtocolError,
    CpReportRejected,
    CpTransportError,
)
from packages.contracts.control_plane.money import usage_v1 as uv

log = logging.getLogger("sms.money.usage")
Q = Decimal("0.000001")
LEASE_S = 120
BACKOFF_BASE_S, BACKOFF_CAP_S = 30, 900
LOCK_KEY = 0x534D535552  # "SMSUR"


def _q(d) -> Decimal:
    return Decimal(d or 0).quantize(Q)


def _s(d) -> str:
    return format(_q(d), "f")


@contextmanager
def snapshot_session(engine: Engine):
    """Transaksion i vetëm snapshot-i: REPEATABLE READ + vetëm-lexim në PostgreSQL (SQLite: një transaksion)."""
    conn = engine.connect()
    try:
        if engine.dialect.name == "postgresql":
            conn = conn.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            )
        with Session(bind=conn) as session:
            yield session
    finally:
        conn.close()


# Vijë test: thirret pas leximit të bilancit, para pjesës tjetër të snapshot-it (prova e konsistencës nën konkurrencë).
_after_balance_hook: Callable[[], None] | None = None


@dataclass(slots=True)
class Draft:
    """Raport pa identitet (report_id/seq): `doc` përmban gjithçka tjetër."""

    wallet_id: int
    enterprise_id: uuid.UUID
    product_id: uuid.UUID
    currency: str
    doc: dict


@dataclass(slots=True)
class BuildResult:
    drafts: list[Draft] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # wallet pa mapim të pastër (nuk raportohet)


def _flows(db: Session, wallet_id: int, after_id: int) -> dict[str, Decimal]:
    net = LedgerEntry.available_delta + LedgerEntry.held_delta
    sign = case((net > 0, 1), (net < 0, -1), else_=0)
    rows = db.execute(
        select(LedgerEntry.entry_type, sign, func.sum(net), func.sum(LedgerEntry.available_delta))
        .where(LedgerEntry.wallet_id == wallet_id, LedgerEntry.id > after_id)
        .group_by(LedgerEntry.entry_type, sign)
    ).all()  # fmt: skip
    f = dict.fromkeys(("grants_applied", "grant_reversals", "captured", "negative_adjustments", "invoice_debits",
                       "other_debits", "positive_local_credit", "released"), Decimal(0))  # fmt: skip
    for etype, sg, total, avail in rows:
        total = _q(total)
        if etype == EntryType.RELEASE:
            f["released"] += _q(avail)
        if etype == EntryType.GRANT:
            f["grants_applied"] += total
        elif etype == EntryType.GRANT_REVERSAL:
            f["grant_reversals"] -= total
        elif etype == EntryType.CAPTURE:
            f["captured"] -= total
        elif etype == EntryType.INVOICE:
            f["invoice_debits"] -= total
        elif etype == EntryType.ADJUSTMENT and sg < 0:
            f["negative_adjustments"] -= total
        elif sg > 0:  # TOPUP, REFUND, ADJUSTMENT+ (çdo lloj ≠ GRANT me rritje neto)
            f["positive_local_credit"] += total
        elif sg < 0:  # çdo debit tjetër (nuk duhet të ekzistojë)
            f["other_debits"] -= total
    return f


def build_drafts(db: Session, *, now: datetime | None = None) -> BuildResult:
    """Lexim i pastër (thirret brenda `snapshot_session`). Nuk shkruan asgjë."""
    now = now or utcnow()
    out = BuildResult()
    mode = settings.money_authority
    wallets = list(db.scalars(select(Wallet).order_by(Wallet.id)))
    cursor = db.get(MoneyCursor, 1)
    for w in wallets:
        ent = db.scalar(select(Enterprise).where(Enterprise.owner_ref == w.owner_ref))
        if ent is None:
            out.skipped.append(f"wallet {w.id}: owner has no enterprise identity")
            continue
        try:
            product_id = ma.sms_product_for(db, ent.id)
        except ma.Unmapped as e:
            out.skipped.append(f"wallet {w.id}: {e}")
            continue
        last = db.scalar(
            select(LedgerEntry)
            .where(LedgerEntry.wallet_id == w.id)
            .order_by(LedgerEntry.id.desc())
            .limit(1)
        )
        avail, held = (last.available_after, last.held_after) if last else (Decimal(0), Decimal(0))
        if _after_balance_hook is not None:
            _after_balance_hook()
        base = db.scalar(
            select(MoneyBaseline).where(
                MoneyBaseline.wallet_id == w.id, MoneyBaseline.status == BASELINE_ACTIVE
            )
        )
        after_id = base.ledger_max_id if base is not None else 0
        f = _flows(db, w.id, after_id)
        sums = db.execute(
            select(func.coalesce(func.sum(LedgerEntry.available_delta), 0),
                   func.coalesce(func.sum(LedgerEntry.held_delta), 0), func.coalesce(func.max(LedgerEntry.id), 0))
            .where(LedgerEntry.wallet_id == w.id)
        ).one()  # fmt: skip
        holds = db.execute(
            select(func.coalesce(func.sum(Hold.amount), 0), func.count()).where(
                Hold.wallet_id == w.id, Hold.status == HoldStatus.ACTIVE)
        ).one()  # fmt: skip
        referenced = set(db.scalars(select(MoneyGrant.ledger_entry_id).where(MoneyGrant.wallet_id == w.id,
                                                                            MoneyGrant.ledger_entry_id.isnot(None))))  # fmt: skip
        referenced |= set(db.scalars(select(MoneyGrant.reversal_entry_id).where(MoneyGrant.wallet_id == w.id,
                                                                                MoneyGrant.reversal_entry_id.isnot(None))))  # fmt: skip
        orphan_credit = orphan_rev = Decimal(0)
        for e in db.scalars(select(LedgerEntry).where(LedgerEntry.wallet_id == w.id,
                                                      LedgerEntry.entry_type.in_((EntryType.GRANT, EntryType.GRANT_REVERSAL)))):  # fmt: skip
            if e.id in referenced:
                continue
            n = e.available_delta + e.held_delta
            if e.entry_type == EntryType.GRANT:
                orphan_credit += max(n, Decimal(0))
            else:
                orphan_rev += -min(n, Decimal(0))
        grants = []
        for g in db.scalars(
            select(MoneyGrant)
            .where(MoneyGrant.enterprise_id == ent.id)
            .order_by(MoneyGrant.issued_seq)
        ):
            detail = (
                g.detail[: uv.MAX_DETAIL]
                if g.status in (G_MISMATCH, G_UNMAPPED, G_RECON) and g.detail
                else None
            )
            grants.append({
                "grant_id": str(g.grant_id), "status": g.status, "amount": _s(g.amount), "currency": g.currency,
                "product_id": str(g.product_id), "purpose": g.purpose, "baseline_ref": g.baseline_ref,
                "issued_seq": g.issued_seq, "reversed_seq": g.reversed_seq,
                "updated_at": uv.format_ts(g.updated_at), "detail": detail,
            })  # fmt: skip
        doc = {
            "schema": uv.SCHEMA, "enterprise_id": str(ent.id), "product_id": str(product_id), "currency": w.currency,
            "authority_mode": mode, "ledger_max_id": int(sums[2]),
            "wallet": {"available": _s(avail), "held": _s(held), "gross": _s(avail + held),
                       "active_hold_total": _s(holds[0]), "active_hold_count": int(holds[1])},
            "baseline": None if base is None else {
                "baseline_ref": base.baseline_ref, "gross_at_cutover": _s(base.gross_at_cutover),
                "ledger_max_id": base.ledger_max_id, "status": base.status},
            "flows": {"baseline_gross": _s(base.gross_at_cutover if base is not None else 0),
                      **{k: _s(v) for k, v in f.items()}},
            "integrity": {"ledger_sum_available": _s(sums[0]), "ledger_sum_held": _s(sums[1]),
                          "orphan_grant_credit": _s(orphan_credit), "orphan_grant_reversal": _s(orphan_rev)},
            "cursor": {
                "epoch": str(cursor.epoch) if cursor is not None and cursor.epoch else None,
                "last_seq": int(cursor.last_seq) if cursor is not None else 0,
                "generation": cursor.authorization_generation if cursor is not None else None,
                "last_success_at": uv.format_ts(cursor.last_success_at) if cursor is not None and cursor.last_success_at else None,
                "has_error": bool(cursor is not None and cursor.last_error)},
            "grants": grants,
        }  # fmt: skip
        out.drafts.append(Draft(w.id, ent.id, product_id, w.currency, doc))
    return out


def _finalize(
    draft: Draft, report_id: uuid.UUID, seq: int, generated_at: datetime
) -> uv.UsageReportV1:
    doc = {
        **draft.doc,
        "report_id": str(report_id),
        "report_seq": seq,
        "generated_at": uv.format_ts(generated_at),
    }
    return uv.UsageReportV1.parse(doc)


def generate(
    engine: Engine, factory: Callable[[], Session], *, now: datetime | None = None
) -> list[uuid.UUID]:
    """Snapshot (një transaksion) → outbox (transaksion i veçantë). Kthen `report_id` e krijuara."""
    now = as_utc(now or utcnow())
    with snapshot_session(engine) as snap:
        built = build_drafts(snap, now=now)
    for msg in built.skipped:
        log.warning("usage report skipped: %s", msg)
    created: list[uuid.UUID] = []
    heartbeat = timedelta(seconds=settings.money_report_heartbeat_seconds)
    for d in built.drafts:
        for attempt in (
            1,
            2,
        ):  # një riprovim nëse një proces tjetër mori report_seq (kufizim UNIQUE)
            with factory() as db:
                last = db.scalar(
                    select(UsageReport).where(
                        UsageReport.enterprise_id == d.enterprise_id, UsageReport.product_id == d.product_id,
                        UsageReport.currency == d.currency).order_by(UsageReport.report_seq.desc()).limit(1)
                )  # fmt: skip
                rid = uuid.uuid4()
                seq = (last.report_seq if last else 0) + 1
                rep = _finalize(d, rid, seq, now)
                if (
                    last is not None
                    and last.content_hash == rep.content_hash()
                    and now - as_utc(last.generated_at) < heartbeat
                ):
                    break  # asnjë ndryshim financiar dhe heartbeat jo i detyruar
                db.add(UsageReport(
                    report_id=rid, enterprise_id=d.enterprise_id, product_id=d.product_id, currency=d.currency,
                    report_seq=seq, authority_mode=d.doc["authority_mode"], ledger_max_id=d.doc["ledger_max_id"],
                    generated_at=now, payload=rep.doc, payload_hash=rep.payload_hash(), content_hash=rep.content_hash(),
                    status=R_PENDING, next_attempt_at=now, created_at=now, updated_at=now,
                ))  # fmt: skip
                try:
                    db.commit()
                    created.append(rid)
                    break
                except IntegrityError:
                    db.rollback()
                    if attempt == 2:
                        raise
    return created


# --- dërgimi ---------------------------------------------------------------------------------------


@dataclass(slots=True)
class DeliveryOutcome:
    kind: str = "ok"  # ok | network_error | auth_error | forbidden | protocol_error
    sent: int = 0
    retry: int = 0
    failed: int = 0
    superseded: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.kind == "ok" and self.failed == 0


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** max(0, attempts - 1))))


def supersede_older(db: Session, now: datetime) -> int:
    """Raportet kumulative të vjetra në pritje s'dërgohen kur ka më të reja për të njëjtin çelës."""
    n = 0
    pending = list(db.scalars(select(UsageReport).where(UsageReport.status.in_((R_PENDING, R_RETRY)))
                              .order_by(UsageReport.report_seq.desc()).with_for_update()))  # fmt: skip
    newest: dict[tuple, int] = {}
    for r in pending:
        k = (r.enterprise_id, r.product_id, r.currency)
        if k not in newest:
            newest[k] = r.report_seq
        else:
            r.status, r.updated_at = R_SUPERSEDED, now
            n += 1
    db.flush()
    return n


def deliver(factory: Callable[[], Session], client: ControlPlaneClient, *, now: datetime | None = None,
            limit: int = 50) -> DeliveryOutcome:  # fmt: skip
    now = as_utc(now or utcnow())
    out = DeliveryOutcome()
    with factory() as db:
        out.superseded = supersede_older(db, now)
        rows = list(db.scalars(
            select(UsageReport).where(
                ((UsageReport.status.in_((R_PENDING, R_RETRY))) & (UsageReport.next_attempt_at <= now))
                | ((UsageReport.status == R_SENDING) & (UsageReport.leased_until < now)))
            .order_by(UsageReport.report_seq).limit(limit).with_for_update(skip_locked=True)
        ))  # fmt: skip
        claimed = []
        for r in rows:
            r.status, r.attempts, r.leased_until, r.updated_at = (
                R_SENDING,
                r.attempts + 1,
                now + timedelta(seconds=LEASE_S),
                now,
            )
            claimed.append((r.report_id, dict(r.payload), r.attempts))
        db.commit()
    for rid, payload, attempts in claimed:
        status, err, stop = R_SENT, None, False
        try:
            client.post_usage_report(payload)
        except CpReportRejected as e:  # 409/413/422: PERMANENT, vëmendje e operatorit
            status, err = R_FAILED, f"rejected by central: {e}"
            out.failed += 1
            log.error("ALERT usage report %s rejected permanently: %s", rid, e)
        except (CpTransportError, CpAuthError, CpForbidden, CpProtocolError, CpError) as e:
            status, err, stop = R_RETRY, f"{type(e).__name__}: {e}", True
            out.kind = {CpTransportError: "network_error", CpAuthError: "auth_error",
                        CpForbidden: "forbidden"}.get(type(e), "protocol_error")  # fmt: skip
            out.detail = err
            out.retry += 1
        else:
            out.sent += 1
        with factory() as db:
            r = db.get(UsageReport, rid, with_for_update=True)
            r.status, r.last_error, r.leased_until, r.updated_at = status, err, None, now
            if status == R_SENT:
                r.sent_at = now
            elif status == R_RETRY:
                r.next_attempt_at = now + _backoff(attempts)
            db.commit()
        if stop:  # Central s'është i arritshëm: mos godit pjesën tjetër; rishikohet pas backoff-it
            break
    return out


# --- gjendja për readiness/monitorim ---------------------------------------------------------------


def latest_by_key(db: Session) -> dict[tuple, UsageReport]:
    out: dict[tuple, UsageReport] = {}
    for r in db.scalars(select(UsageReport).order_by(UsageReport.report_seq.desc())):
        out.setdefault((r.enterprise_id, r.product_id, r.currency), r)
    return out


def last_sent_by_key(db: Session) -> dict[tuple, UsageReport]:
    out: dict[tuple, UsageReport] = {}
    for r in db.scalars(
        select(UsageReport)
        .where(UsageReport.status == R_SENT)
        .order_by(UsageReport.report_seq.desc())
    ):
        out.setdefault((r.enterprise_id, r.product_id, r.currency), r)
    return out


def run_once(engine: Engine, factory: Callable[[], Session], client: ControlPlaneClient,
             now: datetime | None = None) -> DeliveryOutcome:  # fmt: skip
    """Një cikël i reporter-it: gjenero (snapshot→outbox) pastaj dërgo. Dështimi i Central s'ndikon wallet-in."""
    generate(engine, factory, now=now)
    return deliver(factory, client, now=now)
