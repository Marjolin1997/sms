"""M10-S3: transporti i kërkesave të sender-ave Enterprise → Central (`sender.request.v1`). VETËM transport: nuk ndryshon `SenderId`, historinë e vendimeve lokale, as projeksionin e sinkronizuar.

**Shkrimi atomik:** `enqueue` shton NJË INSERT në transaksionin e thirrësit (`sender_ids.request`), pra kërkesa/ridërgimi lokal dhe veprimi i outbox-it commit-ohen ose rikthehen BASHKË. Asnjë rrjet në transaksionin e klientit.
**Veprimi logjik:** `operation_id` UUID i ri për çdo kërkesë/ridërgim; riprovimi i transportit e ridërgon të NJËJTIN payload/`operation_id` ⇒ Central e dedupe-on (efekt biznesi saktësisht një herë mbi transport at-least-once).
**Dorëzimi:** claim i kufizuar (`FOR UPDATE SKIP LOCKED` + lease) → rrjeti JASHTË transaksionit DB → rezultati. Radhë për sender: një veprim nuk dërgohet derisa të gjithë paraardhësit e të njëjtit sender të jenë `sent`.
- 200/201 ⇒ `sent` (rezultati i biznesit — miratim/refuzim automatik — është ACK, jo dështim; s'e prek gjendjen lokale).
- transport/5xx/429/auth/scope/protokoll ⇒ `retry` me backoff (kushte kalimtare/operacionale); lease i skaduar ⇒ rikuperim.
- 404/409/413/422 ose `enterprise_not_authorized` ⇒ `failed` (PERMANENT, shfaqet te readiness; asnjë rishkrim automatik i identitetit lokal)."""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.engine import Engine  # noqa: F401
from sqlalchemy.orm import Session, aliased

from app.core.timeutil import as_utc, utcnow
from app.models.messaging import SenderId
from app.models.sender_request import (
    Q_FAILED,
    Q_PENDING,
    Q_RETRY,
    Q_SENDING,
    Q_SENT,
    SenderRequestOutbox,
)
from app.services.control_plane_client import (
    ControlPlaneClient,
    CpAuthError,
    CpError,
    CpForbidden,
    CpProtocolError,
    CpReportRejected,
    CpTransportError,
)
from packages.contracts.control_plane.sender import request_v1 as rv

log = logging.getLogger("sms.sender.request")
LEASE_S = 120
BACKOFF_BASE_S, BACKOFF_CAP_S = 30, 900
LOCK_KEY = 0x534D53534F  # "SMSSO"
OPEN_STATES = (Q_PENDING, Q_RETRY, Q_SENDING)


def enqueue(db: Session, sender: SenderId, request_type: str) -> SenderRequestOutbox | None:
    """NJË INSERT në transaksionin e thirrësit. `request_type` ∈ requested|resubmitted. Pa `enterprise_id` (rresht legacy) nuk ka identitet drejt Central: log + `None` (kurrë heshtur)."""
    if sender.enterprise_id is None:
        log.warning("sender request not queued: sender %s has no enterprise identity", sender.id)
        return None
    req = rv.SenderRequestV1.build(
        operation_id=str(uuid.uuid4()),
        operation="request" if request_type == "requested" else "resubmit",
        enterprise_id=str(sender.enterprise_id),
        external_ref=rv.external_ref_for(sender.id),
        country=sender.country,
        sender_kind=sender.kind.value,
        display_value=sender.value,
        evidence_ref=None,
    )
    now = utcnow()
    row = SenderRequestOutbox(
        operation_id=uuid.UUID(req.operation_id), enterprise_id=sender.enterprise_id, sender_id=sender.id,
        external_ref=req.external_ref, request_type=request_type, schema_version=rv.SCHEMA,
        payload=req.to_dict(), request_hash=req.request_hash(), state=Q_PENDING, attempts=0,
        next_attempt_at=now, created_at=now, updated_at=now,
    )  # fmt: skip
    db.add(row)
    db.flush()
    return row


@dataclass(slots=True)
class DeliveryOutcome:
    kind: str = "ok"
    sent: int = 0
    retry: int = 0
    failed: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.kind == "ok" and self.failed == 0


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** max(0, attempts - 1))))


def _claim(db: Session, now: datetime, limit: int) -> list[SenderRequestOutbox]:
    earlier = aliased(SenderRequestOutbox)
    head = ~exists().where(
        earlier.sender_id == SenderRequestOutbox.sender_id,
        earlier.id < SenderRequestOutbox.id,
        earlier.state != Q_SENT,
    )
    due = (
        (SenderRequestOutbox.state.in_((Q_PENDING, Q_RETRY)))
        & (SenderRequestOutbox.next_attempt_at <= now)
    ) | ((SenderRequestOutbox.state == Q_SENDING) & (SenderRequestOutbox.leased_until < now))
    return list(
        db.scalars(
            select(SenderRequestOutbox).where(due, head).order_by(SenderRequestOutbox.id).limit(limit).with_for_update(skip_locked=True)
        )
    )  # fmt: skip


def deliver(
    factory: Callable[[], Session],
    client: ControlPlaneClient,
    *,
    now: datetime | None = None,
    limit: int = 25,
) -> DeliveryOutcome:
    now = as_utc(now or utcnow())
    out = DeliveryOutcome()
    with factory() as db:
        claimed = []
        for r in _claim(db, now, limit):
            r.state, r.attempts, r.leased_until, r.updated_at = (
                Q_SENDING, r.attempts + 1, now + timedelta(seconds=LEASE_S), now,
            )  # fmt: skip
            claimed.append((r.id, dict(r.payload), r.attempts))
        db.commit()
    for rid, payload, attempts in claimed:
        state, code, ack, stop = Q_SENT, None, None, False
        try:
            ack = client.post_sender_request(payload)
        except CpReportRejected as e:
            state, code = Q_FAILED, (e.code or f"http_{e.status}")[:32]
            out.failed += 1
            log.error("ALERT sender request %s rejected permanently: %s", rid, code)
        except (CpTransportError, CpAuthError, CpForbidden, CpProtocolError, CpError) as e:
            state, stop = Q_RETRY, True
            code = {CpTransportError: "transport", CpAuthError: "auth", CpForbidden: "forbidden"}.get(type(e), "protocol")  # fmt: skip
            out.kind, out.detail = code + "_error", code
            out.retry += 1
        else:
            out.sent += 1
        with factory() as db:
            r = db.get(SenderRequestOutbox, rid, with_for_update=True)
            r.state, r.last_error_code, r.leased_until, r.updated_at = state, code, None, now
            if state == Q_SENT:
                r.sent_at, r.last_error_code = now, None
                r.ack_outcome, r.ack_registry_ref = ack["outcome"], uuid.UUID(ack["registry_ref"])
            elif state == Q_RETRY:
                r.next_attempt_at = now + _backoff(attempts)
            db.commit()
        if stop:
            break
    return out


def run_once(
    factory: Callable[[], Session], client: ControlPlaneClient, now: datetime | None = None
) -> DeliveryOutcome:
    return deliver(factory, client, now=now)


def _age(now: datetime, t) -> int | None:
    return None if t is None else max(0, int((now - as_utc(t)).total_seconds()))


def stats(db: Session, now: datetime | None = None) -> dict:
    """Numra të sigurt (pa vlera sender, pa PII): numërues sipas gjendjes, mosha, përpjekje, dështime sipas kategorisë."""
    now = as_utc(now or utcnow())
    by = dict(
        db.execute(
            select(SenderRequestOutbox.state, func.count()).group_by(SenderRequestOutbox.state)
        ).all()
    )
    oldest = db.scalar(
        select(func.min(SenderRequestOutbox.created_at)).where(
            SenderRequestOutbox.state.in_(OPEN_STATES)
        )
    )
    last_sent = db.scalar(select(func.max(SenderRequestOutbox.sent_at)))
    stuck = db.scalar(
        select(func.count()).select_from(SenderRequestOutbox).where(
            SenderRequestOutbox.state == Q_SENDING, SenderRequestOutbox.leased_until < now
        )
    ) or 0  # fmt: skip
    failures = dict(
        db.execute(
            select(SenderRequestOutbox.last_error_code, func.count())
            .where(SenderRequestOutbox.state == Q_FAILED)
            .group_by(SenderRequestOutbox.last_error_code)
        ).all()
    )
    attempts = db.scalar(select(func.coalesce(func.sum(SenderRequestOutbox.attempts), 0))) or 0
    max_attempts = db.scalar(select(func.coalesce(func.max(SenderRequestOutbox.attempts), 0))) or 0
    last_err = db.scalar(
        select(SenderRequestOutbox.last_error_code)
        .where(SenderRequestOutbox.state == Q_RETRY)
        .order_by(SenderRequestOutbox.updated_at.desc())
        .limit(1)
    )
    return {
        "pending": by.get(Q_PENDING, 0), "retry": by.get(Q_RETRY, 0), "sending": by.get(Q_SENDING, 0),
        "failed": by.get(Q_FAILED, 0), "sent": by.get(Q_SENT, 0), "stuck_sending": int(stuck),
        "oldest_open_age_seconds": _age(now, oldest), "last_sent_age_seconds": _age(now, last_sent),
        "attempts_total": int(attempts), "attempts_max": int(max_attempts),
        "failed_by_category": {str(k): v for k, v in failures.items()}, "last_retry_error": last_err,
    }  # fmt: skip
