"""M9-c: aplikuesi i `cp.money.v1` në Enterprise. Pa HTTP/JWT (transport te `control_plane_client`), pa
thirrje drejt Central nga rruga e dërgimit: ky modul punon vetëm mbi DB-në lokale.

Rrjedha (një faqe = një transaksion): kyç kursorin → valido epokë/generation/rendin e seq → aplikon çdo
ngjarje IDEMPOTENT sipas `grant_id` (+ `event_id` + hash i payload-it) → kursori. Ngjarje e keqe/në konflikt
(ContractError, ripërsëritje me përmbajtje tjetër, reversal pa issuance) ⇒ kursori NUK kalon atë ngjarje:
aplikohet vetëm prefiksi i mirë, `last_error` regjistrohet (alarm/readiness), asgjë s'anashkalohet.

Kredia: `SMS_MONEY_AUTHORITY=central` ⇒ GRANT postohet në ledger (`grant:<uuid>`, UNIQUE sipas wallet+key).
`shadow` ⇒ grant-i normal REGJISTROHET `deferred_shadow` (s'kreditohet; aplikohet nga `drain_deferred` kur
authority bëhet central, sipas seq). Bootstrap që përputhet me baseline ⇒ `matched_to_existing_balance`,
rresht ledger me delta 0 (provenance, jo kredi). Mospërputhje ⇒ `baseline_mismatch`/`unmapped`, pa mutacion,
kursori përparon pas regjistrimit durabël, readiness dështon. Reversal konservativ: debit vetëm nëse
available ≥ shuma (holds aktive s'preken); përndryshe `reconciliation_required`, kurrë negativ.
"""

import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import as_utc, utcnow
from app.models.money_authority import (
    BASELINE_ACTIVE,
    G_APPLIED,
    G_DEFERRED,
    G_MATCHED,
    G_MISMATCH,
    G_RECON,
    G_REVERSED,
    G_UNMAPPED,
    G_VOIDED,
    MoneyCursor,
    MoneyGrant,
)
from app.models.wallet import EntryType
from app.services import money_authority as ma
from app.services import wallet as wallets
from packages.contracts.control_plane.money import v1

ALERT_AGE_S, SLO_AGE_S = 300, 900
APPLIED, NOOP = "applied", "noop"


class MoneySyncError(Exception):
    """Faqja s'mund të vazhdojë (konfigurim/rend/kursor). Kursori s'lëviz."""


class CursorMismatch(MoneySyncError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason  # no_cursor | epoch_mismatch | generation_mismatch


class EventConflict(MoneySyncError):
    """Ngjarje që kundërshton gjendjen e regjistruar (përsëritje me përmbajtje tjetër, reversal pa
    issuance, identitet i ndryshuar). Fail-closed: kursori ndalet këtu."""


def parse_events(raw: Sequence[Any]) -> list[v1.MoneyEventV1]:
    return [v1.MoneyEventV1.from_dict(r) for r in raw]


# --- kursori -------------------------------------------------------------------------------------


def _insert_cursor_if_missing(db: Session) -> None:
    ins = (postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert)(
        MoneyCursor.__table__
    )
    db.execute(ins.values(id=1, last_seq=0).on_conflict_do_nothing())


def _cursor_q(lock: bool):
    q = select(MoneyCursor).where(MoneyCursor.id == 1).execution_options(populate_existing=True)
    return q.with_for_update() if lock else q


def get_cursor(db: Session, *, lock: bool = False) -> MoneyCursor:
    row = db.scalar(_cursor_q(lock))
    if row is None:
        _insert_cursor_if_missing(db)
        row = db.scalar(_cursor_q(lock))
    assert row is not None
    return row


def init_cursor(db: Session, epoch: uuid.UUID, generation: int) -> None:
    """Fillimi i parë: (epoch, generation) nga `/internal/money/state`, kursor 0 ⇒ riprodhim i plotë
    (idempotent: grant-et e panjohura aplikohen, të njohurat janë no-op)."""
    cur = get_cursor(db, lock=True)
    if cur.epoch is not None:
        return
    cur.epoch, cur.authorization_generation, cur.last_seq = epoch, generation, 0


def rebase_generation(db: Session, epoch: uuid.UUID, generation: int) -> None:
    """Ndryshim autorizimi (bashkësia e enterprise-eve): riprodho nga 0 me generation-in e ri. Epoka
    duhet të jetë e njëjtë; epokë tjetër = veprim operatori (`reset_epoch`)."""
    cur = get_cursor(db, lock=True)
    if cur.epoch != epoch:
        raise CursorMismatch("epoch_mismatch")
    cur.authorization_generation, cur.last_seq = generation, 0


def reset_epoch(db: Session, epoch: uuid.UUID, generation: int) -> None:
    """VETËM operator (skripti `money_authority reset-cursor --ack`): epokë e re e Central ⇒ riprodhim
    nga 0. Grant-et e aplikuar mbeten; ato që Central s'i ka më janë çështje rakordimi (M9-d)."""
    cur = get_cursor(db, lock=True)
    cur.epoch, cur.authorization_generation, cur.last_seq = epoch, generation, 0
    cur.last_error = cur.last_error_at = None


def sync_age_seconds(cur: MoneyCursor, now: datetime | None = None) -> float | None:
    if cur.last_success_at is None:
        return None
    return (as_utc(now or utcnow()) - as_utc(cur.last_success_at)).total_seconds()


# --- aplikimi i një ngjarje ----------------------------------------------------------------------


def payload_hash(ev: v1.MoneyEventV1) -> str:
    doc = {"event_type": ev.event_type, "enterprise_id": ev.enterprise_id,
           "grant_id": ev.grant_id, "data": ev.data.to_dict()}  # fmt: skip
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _identity(g: MoneyGrant, ev: v1.MoneyEventV1) -> bool:
    d = ev.data
    return (
        str(g.enterprise_id) == ev.enterprise_id and str(g.account_id) == d.account_id
        and str(g.product_id) == d.product_id and g.currency == d.currency
        and g.amount == d.amount and g.purpose == d.purpose and g.baseline_ref == d.baseline_ref
    )  # fmt: skip


def _new_row(ev: v1.MoneyEventV1, status: str, detail: str | None, wallet_id, now) -> MoneyGrant:
    d = ev.data
    return MoneyGrant(
        grant_id=uuid.UUID(ev.grant_id), enterprise_id=uuid.UUID(ev.enterprise_id),
        account_id=uuid.UUID(d.account_id), product_id=uuid.UUID(d.product_id),
        currency=d.currency, amount=d.amount, purpose=d.purpose, baseline_ref=d.baseline_ref,
        wallet_id=wallet_id, status=status, detail=detail, issued_seq=ev.seq,
        issued_event_id=uuid.UUID(ev.event_id), issued_payload_hash=payload_hash(ev),
        created_at=now, updated_at=now,
    )  # fmt: skip


def _gkey(grant_id) -> str:
    return f"grant:{grant_id}"


def _post_credit(db: Session, w_id: int, g: MoneyGrant) -> int:
    e = wallets._post(
        db, w_id, EntryType.GRANT, g.amount, wallets.ZERO, _gkey(g.grant_id), "grant",
        str(g.grant_id), "central grant", authoritative=True,
    )  # fmt: skip
    return e.id


def _bootstrap_problem(db: Session, ev: v1.MoneyEventV1) -> tuple[str | None, int | None]:
    """→ (arsyeja e mospërputhjes | None, wallet_id). Asnjë përputhje e pjesshme."""
    d = ev.data
    b = ma.get_baseline_by_ref(db, d.baseline_ref)  # type: ignore[arg-type]
    if b is None:
        return "baseline_ref_unknown", None
    if b.status != BASELINE_ACTIVE:
        return "baseline_not_active", None
    if not ma.baseline_valid(b):
        return "baseline_hash_invalid", None
    if str(b.enterprise_id) != ev.enterprise_id:
        return "enterprise_mismatch", None
    if str(b.product_id) != d.product_id:
        return "product_mismatch", None
    if b.currency != d.currency:
        return "currency_mismatch", None
    if b.gross_at_cutover != d.amount:
        return f"amount_mismatch: baseline gross {b.gross_at_cutover} != grant {d.amount}", None
    try:
        w = ma.resolve_wallet(db, b.enterprise_id, b.product_id, b.currency, create=False)
    except ma.Unmapped as e:
        return f"mapping: {e}", None
    if w.id != b.wallet_id:
        return "wallet_mismatch", None
    if db.scalar(select(MoneyGrant.grant_id).where(MoneyGrant.baseline_ref == b.baseline_ref,
                                                   MoneyGrant.status == G_MATCHED)):  # fmt: skip
        return "duplicate_bootstrap_for_baseline", None
    if ma.positive_mints_after(db, w.id, b.ledger_max_id):
        return "positive_local_mint_after_baseline", None
    return None, w.id


def _apply_issued(db: Session, ev: v1.MoneyEventV1, now: datetime) -> str:
    d = ev.data
    existing = db.get(MoneyGrant, uuid.UUID(ev.grant_id), populate_existing=True)
    if existing is not None:
        if (str(existing.issued_event_id) == ev.event_id and existing.issued_seq == ev.seq
                and existing.issued_payload_hash == payload_hash(ev)):  # fmt: skip
            return NOOP
        raise EventConflict(f"grant {ev.grant_id} was already recorded from a different event")
    ent_id = uuid.UUID(ev.enterprise_id)
    if d.purpose == v1.PURPOSE_BOOTSTRAP:
        reason, wallet_id = _bootstrap_problem(db, ev)
        if reason is not None:
            db.add(_new_row(ev, G_MISMATCH, reason, None, now))
            db.flush()
            return APPLIED
        row = _new_row(ev, G_MATCHED, "matched_to_existing_balance", wallet_id, now)
        db.add(row)
        db.flush()
        wallets.lock_wallet(db, wallet_id)
        e = wallets._post(  # delta 0: provenance, jo kredi
            db, wallet_id, EntryType.GRANT, wallets.ZERO, wallets.ZERO, _gkey(row.grant_id),
            "grant", str(row.grant_id), "matched_to_existing_balance", authoritative=True,
        )  # fmt: skip
        row.ledger_entry_id = e.id
        return APPLIED
    row = _new_row(ev, G_DEFERRED, "received in shadow: not credited", None, now)
    db.add(row)
    db.flush()
    try:
        ma.check_product(db, ent_id, uuid.UUID(d.product_id))
    except ma.Unmapped as e:
        row.status, row.detail = G_UNMAPPED, str(e)[:500]
        return APPLIED
    if settings.money_authority == "central":
        _credit_row(db, row, now)
    return APPLIED


def _credit_row(db: Session, row: MoneyGrant, now: datetime) -> None:
    """Zgjidh wallet-in (krijon bosh nëse mungon) dhe poston GRANT; mapim i pavlefshëm ⇒ `unmapped`."""
    try:
        w = ma.resolve_wallet(db, row.enterprise_id, row.product_id, row.currency, create=True)
    except ma.Unmapped as e:
        row.status, row.detail, row.updated_at = G_UNMAPPED, str(e)[:500], now
        return
    row.wallet_id = w.id
    wallets.lock_wallet(db, row.wallet_id)
    row.ledger_entry_id = _post_credit(db, row.wallet_id, row)
    row.status, row.detail, row.updated_at = G_APPLIED, None, now


def _apply_reversed(db: Session, ev: v1.MoneyEventV1, now: datetime) -> str:
    g = db.get(MoneyGrant, uuid.UUID(ev.grant_id), populate_existing=True)
    if g is None:
        raise EventConflict(f"reversal for unknown grant {ev.grant_id}")
    if g.reversed_event_id is not None:
        if str(g.reversed_event_id) == ev.event_id and g.reversed_seq == ev.seq:
            return NOOP
        raise EventConflict(f"grant {ev.grant_id} already carries a different reversal")
    if not _identity(g, ev):
        raise EventConflict(f"reversal identity differs from the recorded grant {ev.grant_id}")
    g.reversed_seq, g.reversed_event_id, g.updated_at = ev.seq, uuid.UUID(ev.event_id), now
    if g.status in (G_DEFERRED, G_MISMATCH, G_UNMAPPED):
        g.status, g.detail = G_VOIDED, f"reversed before any credit existed (was {g.status})"
        return APPLIED
    if g.status not in (G_APPLIED, G_MATCHED):
        raise EventConflict(f"reversal for grant {g.grant_id} in status {g.status}")
    wallets.lock_wallet(db, g.wallet_id)
    avail, _held = wallets.balances(db, g.wallet_id)
    if avail < g.amount:  # holds aktive s'preken; kurrë negativ
        g.status = G_RECON
        g.detail = f"insufficient available funds for reversal: available={avail} amount={g.amount}"
        return APPLIED
    e = wallets._post(
        db, g.wallet_id, EntryType.GRANT_REVERSAL, -g.amount, wallets.ZERO,
        f"grant_reversal:{g.grant_id}", "grant", str(g.grant_id), "central grant reversal",
        authoritative=True,
    )  # fmt: skip
    g.reversal_entry_id, g.status, g.detail = e.id, G_REVERSED, None
    return APPLIED


def apply_event(db: Session, ev: v1.MoneyEventV1, now: datetime) -> str:
    if ev.event_type == v1.EVENT_GRANT_ISSUED:
        return _apply_issued(db, ev, now)
    return _apply_reversed(db, ev, now)


def drain_deferred(db: Session, now: datetime | None = None) -> int:
    """Rivlerëson grant-et standard të regjistruara (`deferred_shadow`, `unmapped`), sipas `issued_seq`.
    shadow: unmapped→deferred kur mapimi bëhet i vlefshëm (asnjë kredi). central: kreditoi (GRANT).
    Reversal-et e tyre u shënuan `voided_before_apply` kur erdhën. Idempotent. Kthen numrin e kredituar."""
    mode = settings.money_authority
    if mode == "local":
        return 0
    now = now or utcnow()
    n = 0
    for g in db.scalars(
        select(MoneyGrant)
        .where(MoneyGrant.status.in_((G_DEFERRED, G_UNMAPPED)), MoneyGrant.purpose == "standard",
               MoneyGrant.reversed_event_id.is_(None))
        .order_by(MoneyGrant.issued_seq)
        .with_for_update()
    ):  # fmt: skip
        try:
            ma.check_product(db, g.enterprise_id, g.product_id)
        except ma.Unmapped as e:
            g.status, g.detail, g.updated_at = G_UNMAPPED, str(e)[:500], now
            continue
        if g.status == G_UNMAPPED:
            g.status, g.detail, g.updated_at = G_DEFERRED, "mapping valid: not credited", now
        if mode == "central":
            _credit_row(db, g, now)
            n += g.status == G_APPLIED
    db.flush()
    return n


# --- faqja ---------------------------------------------------------------------------------------


@dataclass(slots=True)
class BatchResult:
    applied: int = 0
    noop: int = 0
    last_seq: int = 0
    error: str | None = None
    unresolved: int = 0
    outcomes: list[tuple[int, str]] = field(default_factory=list)


def apply_batch(
    db: Session,
    *,
    epoch: uuid.UUID,
    authorization_generation: int,
    events: Sequence[v1.MoneyEventV1],
    next_seq: int,
    now: datetime | None = None,
) -> BatchResult:
    """Një faqe, atomikisht (thirrësi bën commit). `EventConflict` brenda faqes NUK hedhet: prefiksi i mirë
    aplikohet, kursori ndalet para ngjarjes problematike dhe `last_error` regjistrohet."""
    now = now or utcnow()
    res = BatchResult()
    if settings.money_authority == "local":
        raise MoneySyncError("SMS_MONEY_AUTHORITY=local: the money consumer must not apply events")
    cur = get_cursor(db, lock=True)
    if cur.epoch is None or cur.authorization_generation is None:
        raise CursorMismatch("no_cursor")
    if cur.epoch != epoch:
        raise CursorMismatch("epoch_mismatch")
    if cur.authorization_generation != authorization_generation:
        raise CursorMismatch("generation_mismatch")
    if isinstance(next_seq, bool) or not isinstance(next_seq, int) or next_seq < cur.last_seq:
        raise MoneySyncError(f"next_seq {next_seq!r} is behind the local cursor {cur.last_seq}")
    prev = 0
    for ev in events:  # rendi/kufiri vlerësohen mbi gjithë faqen
        if ev.seq <= prev or ev.seq > next_seq:
            raise MoneySyncError(
                f"event seq {ev.seq} out of order/range (after {prev}, next {next_seq})"
            )
        prev = ev.seq
    drain_deferred(db, now)
    last_ok = cur.last_seq
    for ev in events:
        if (
            ev.seq <= cur.last_seq
        ):  # tashmë i konsumuar (p.sh. konsumator tjetër e çoi kursorin): no-op
            res.outcomes.append((ev.seq, NOOP))
            res.noop += 1
            continue
        try:
            with db.begin_nested():
                out = apply_event(db, ev, now)
        except EventConflict as e:
            res.error = f"seq {ev.seq} {ev.event_type}: {e}"
            break
        res.outcomes.append((ev.seq, out))
        res.applied += out == APPLIED
        res.noop += out == NOOP
        last_ok = ev.seq
    if res.error is None:
        cur.last_seq, cur.last_success_at, cur.last_error, cur.last_error_at = (
            next_seq, now, None, None,
        )  # fmt: skip
        res.last_seq = next_seq
    else:
        cur.last_seq, cur.last_error, cur.last_error_at = last_ok, res.error[:2000], now
        res.last_seq = last_ok
    return res


def record_error(db: Session, message: str, now: datetime | None = None) -> None:
    """Gabim i vëzhguar nga poller-i (ngjarje e palexueshme etj.): kursori s'lëviz, operatori e sheh."""
    cur = get_cursor(db, lock=True)
    cur.last_error, cur.last_error_at = message[:2000], now or utcnow()


def unresolved_count(db: Session) -> int:
    from app.models.money_authority import UNRESOLVED

    return len(
        list(db.scalars(select(MoneyGrant.grant_id).where(MoneyGrant.status.in_(UNRESOLVED))))
    )
