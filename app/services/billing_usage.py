"""M9-g2: prova e faturueshmërisë së email-it (Enterprise) + raportimi kumulativ drejt Central. Pa HTTP në gjenerim.

**Capture (rruga e vetme):** `record_first_billable` thirret nga `emails._move` — dera e vetme e tranzicioneve të email — kur një email
hyn NË SENT/DELIVERED/BOUNCED/COMPLAINED nga një gjendje jo-e-faturueshme. Një `INSERT … ON CONFLICT (email_id) DO NOTHING` i vetëm
(i kufizuar; pa COUNT mbi emailet) brenda të njëjtit transaksion me ndryshimin e statusit ⇒ ose të dyja ose asnjëra. SENT→DELIVERED,
callback-e dublikat dhe rikthimet s'shtojnë kurrë njësi të dytë (UNIQUE). UNKNOWN s'është i faturueshëm; një zgjidhje autoritative
(SENT/DELIVERED/…) krijon provën aty.

**Raporti:** cumulative_billable_count + watermark vijnë nga NJË snapshot REPEATABLE READ, nga një deklaratë e vetme. `generated_at` merret
PARA hapjes së snapshot-it (pretendon vetëm tranzicione të commit-uara para tij). Outbox at-least-once, pa rrjet brenda transaksionit DB,
dështimi i raportimit s'ndikon dërgimin e email-it."""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import Conflict
from app.core.timeutil import as_utc, utcnow
from app.models.billing_usage import (
    BILLABLE_STATUSES,
    R_FAILED,
    R_PENDING,
    R_RETRY,
    R_SENDING,
    R_SENT,
    R_SUPERSEDED,
    BillingUsageReport,
    EmailBillableEvent,
)
from app.models.control_plane import ENTITLEMENT_WITHDRAWN, Entitlement
from app.models.enterprise import Enterprise
from app.services.control_plane_client import (
    ControlPlaneClient,
    CpAuthError,
    CpError,
    CpForbidden,
    CpProtocolError,
    CpReportRejected,
    CpTransportError,
)
from app.services.money_usage import snapshot_session
from packages.contracts.control_plane.billing import usage_v1 as bv

log = logging.getLogger("sms.billing.usage")
LEASE_S = 120
BACKOFF_BASE_S, BACKOFF_CAP_S = 30, 900
LOCK_KEY = 0x534D534255  # "SMSBU"


# --- capture ----------------------------------------------------------------------------------------------------


def _enterprise_id(db: Session, e) -> uuid.UUID:
    if e.enterprise_id is not None:
        return e.enterprise_id
    ent = db.scalar(select(Enterprise.id).where(Enterprise.owner_ref == e.owner_ref))
    if ent is None:
        raise Conflict("billable email has no enterprise identity")
    return ent


def record_first_billable(db: Session, e, to_status, now: datetime | None = None) -> None:
    """Një INSERT me ON CONFLICT DO NOTHING; thirrësi garanton se `to_status` është i faturueshëm dhe se statusi i mëparshëm s'ishte."""
    value = getattr(to_status, "value", to_status)
    if value not in BILLABLE_STATUSES:
        return
    now = now or utcnow()
    insert = postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert
    stmt = insert(EmailBillableEvent.__table__).values(
        email_id=e.id, enterprise_id=_enterprise_id(db, e), first_status=value, billable_at=now, created_at=now
    ).on_conflict_do_nothing(index_elements=["email_id"])  # fmt: skip
    db.execute(stmt)


# --- snapshot dhe outbox -------------------------------------------------------------------------------------------


def email_product_for(db: Session, enterprise_id: uuid.UUID) -> uuid.UUID | None:
    """Produkti i vetëm EMAIL i enterprise-it (entitlements cp.v1); asnjë ose shumë ⇒ None (nuk raportohet, i dukshëm te readiness)."""
    ids = set(db.scalars(select(Entitlement.product_id).where(
        Entitlement.enterprise_id == enterprise_id, Entitlement.channel == "email",
        Entitlement.status != ENTITLEMENT_WITHDRAWN)))  # fmt: skip
    return next(iter(ids)) if len(ids) == 1 else None


@dataclass(slots=True)
class Draft:
    enterprise_id: uuid.UUID
    product_id: uuid.UUID
    watermark: int
    count: int


@dataclass(slots=True)
class BuildResult:
    drafts: list[Draft] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def build_drafts(db: Session) -> BuildResult:
    """Lexon NJË snapshot: (watermark, count) per enterprise nga e njëjta deklaratë; enterprise pa prova ⇒ (0, 0)."""
    agg = {r[0]: (int(r[1]), int(r[2])) for r in db.execute(
        select(EmailBillableEvent.enterprise_id, func.max(EmailBillableEvent.id), func.count())
        .group_by(EmailBillableEvent.enterprise_id))}  # fmt: skip
    out = BuildResult()
    ents = set(
        db.scalars(
            select(Entitlement.enterprise_id).where(
                Entitlement.channel == "email", Entitlement.status != ENTITLEMENT_WITHDRAWN
            )
        )
    ) | set(agg)
    for eid in sorted(ents, key=str):
        product = email_product_for(db, eid)
        if product is None:
            out.skipped.append(f"enterprise {eid}: no single email product entitlement")
            continue
        wm, cnt = agg.get(eid, (0, 0))
        out.drafts.append(Draft(eid, product, wm, cnt))
    return out


def _finalize(
    d: Draft, report_id: uuid.UUID, seq: int, generated_at: datetime
) -> bv.BillingUsageReportV1:
    return bv.BillingUsageReportV1.parse({
        "schema": bv.SCHEMA, "report_id": str(report_id), "report_seq": seq, "enterprise_id": str(d.enterprise_id),
        "product_id": str(d.product_id), "generated_at": bv.format_ts(generated_at), "watermark": d.watermark,
        "cumulative_billable_count": d.count,
    })  # fmt: skip


def generate(
    engine: Engine, factory: Callable[[], Session], *, now: datetime | None = None
) -> list[uuid.UUID]:
    """Snapshot (një transaksion, `generated_at` para tij) → outbox (transaksion i veçantë). Heartbeat: raport i ri edhe pa ndryshim."""
    now = as_utc(now or utcnow())
    with snapshot_session(engine) as snap:
        built = build_drafts(snap)
    for msg in built.skipped:
        log.warning("billing usage report skipped: %s", msg)
    heartbeat = timedelta(seconds=settings.billing_usage_heartbeat_seconds)
    created: list[uuid.UUID] = []
    for d in built.drafts:
        for attempt in (1, 2):
            with factory() as db:
                last = db.scalar(select(BillingUsageReport).where(
                    BillingUsageReport.enterprise_id == d.enterprise_id, BillingUsageReport.product_id == d.product_id)
                    .order_by(BillingUsageReport.report_seq.desc()).limit(1))  # fmt: skip
                rid = uuid.uuid4()
                seq = (last.report_seq if last else 0) + 1
                rep = _finalize(d, rid, seq, now)
                if (
                    last is not None
                    and last.content_hash == rep.content_hash()
                    and now - as_utc(last.generated_at) < heartbeat
                ):
                    break
                db.add(BillingUsageReport(
                    report_id=rid, enterprise_id=d.enterprise_id, product_id=d.product_id, report_seq=seq, watermark=d.watermark,
                    cumulative_billable_count=d.count, generated_at=now, payload=rep.doc, payload_hash=rep.payload_hash(),
                    content_hash=rep.content_hash(), status=R_PENDING, next_attempt_at=now, created_at=now, updated_at=now))  # fmt: skip
                try:
                    db.commit()
                    created.append(rid)
                    break
                except IntegrityError:
                    db.rollback()
                    if attempt == 2:
                        raise
    return created


# --- dërgimi -----------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class DeliveryOutcome:
    kind: str = "ok"
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
    n = 0
    pending = list(db.scalars(select(BillingUsageReport).where(BillingUsageReport.status.in_((R_PENDING, R_RETRY)))
                              .order_by(BillingUsageReport.report_seq.desc()).with_for_update()))  # fmt: skip
    newest: set = set()
    for r in pending:
        k = (r.enterprise_id, r.product_id)
        if k not in newest:
            newest.add(k)
        else:
            r.status, r.updated_at = R_SUPERSEDED, now
            n += 1
    db.flush()
    return n


def deliver(
    factory: Callable[[], Session],
    client: ControlPlaneClient,
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> DeliveryOutcome:
    now = as_utc(now or utcnow())
    out = DeliveryOutcome()
    with factory() as db:
        out.superseded = supersede_older(db, now)
        rows = list(db.scalars(
            select(BillingUsageReport).where(
                ((BillingUsageReport.status.in_((R_PENDING, R_RETRY))) & (BillingUsageReport.next_attempt_at <= now))
                | ((BillingUsageReport.status == R_SENDING) & (BillingUsageReport.leased_until < now)))
            .order_by(BillingUsageReport.report_seq).limit(limit).with_for_update(skip_locked=True)))  # fmt: skip
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
            client.post_billing_usage(payload)
        except CpReportRejected as e:
            status, err = R_FAILED, f"rejected by central: {e}"
            out.failed += 1
            log.error("ALERT billing usage report %s rejected permanently: %s", rid, e)
        except (CpTransportError, CpAuthError, CpForbidden, CpProtocolError, CpError) as e:
            status, err, stop = R_RETRY, f"{type(e).__name__}: {e}", True
            out.kind = {CpTransportError: "network_error", CpAuthError: "auth_error", CpForbidden: "forbidden"}.get(type(e), "protocol_error")  # fmt: skip
            out.detail = err
            out.retry += 1
        else:
            out.sent += 1
        with factory() as db:
            r = db.get(BillingUsageReport, rid, with_for_update=True)
            r.status, r.last_error, r.leased_until, r.updated_at = status, err, None, now
            if status == R_SENT:
                r.sent_at = now
            elif status == R_RETRY:
                r.next_attempt_at = now + _backoff(attempts)
            db.commit()
        if stop:
            break
    return out


def run_once(
    engine: Engine,
    factory: Callable[[], Session],
    client: ControlPlaneClient,
    now: datetime | None = None,
) -> DeliveryOutcome:
    generate(engine, factory, now=now)
    return deliver(factory, client, now=now)


def stats(db: Session, now: datetime | None = None) -> dict:
    """Numra të sigurt (pa PII) për observability/readiness: watermark-u më i fundit, numri kumulativ, outbox, mosha e raportit."""
    now = as_utc(now or utcnow())
    wm = db.scalar(select(func.max(EmailBillableEvent.id))) or 0
    cnt = db.scalar(select(func.count()).select_from(EmailBillableEvent)) or 0
    by = {
        s: n
        for s, n in db.execute(
            select(BillingUsageReport.status, func.count()).group_by(BillingUsageReport.status)
        )
    }
    open_states = (R_PENDING, R_RETRY, R_SENDING, R_FAILED)
    oldest = db.scalar(
        select(func.min(BillingUsageReport.created_at)).where(
            BillingUsageReport.status.in_(open_states)
        )
    )
    last_sent = db.scalar(select(func.max(BillingUsageReport.sent_at)))
    age = lambda t: None if t is None else max(0, int((now - as_utc(t)).total_seconds()))  # noqa: E731
    return {"latest_watermark": int(wm), "cumulative_billable_count": int(cnt), "outbox": by,
            "oldest_unsent_age_seconds": age(oldest), "last_sent_age_seconds": age(last_sent)}  # fmt: skip
