"""Dispatcher i outbox-it të njoftimeve (M8-e). Dërgimi SMTP bëhet JASHTË çdo transaksioni DB.

Rrjedha për një rresht: (Tx A) claim → `sending`, attempts+1, commit · (pa transaksion) rindërto
tokenin nga çelësi+nonce dhe thirr mailer-in · (Tx B) `sent` | `pending` me backoff | `failed`
(terminal pas MAX_ATTEMPTS) | `superseded`. Crash mes A dhe B: rreshti mbetet `sending` dhe rimerret
pas STALE_SENDING_S (dërgim "të paktën një herë"; tokeni është deterministik, pra dublikati është i
padëmshëm). Rreshtat jo më aktualë (nonce i ndryshuar/verifikuar/skaduar) ⇒ `superseded`, s'dërgohen.
Pa retry të pafund: max 5 përpjekje, backoff 1m·4^n. Retry i dorës = `resend` (rrotullon nonce-in).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from apps.central.core.timeutil import utcnow
from apps.central.models.registration import NotificationOutbox, RegistrationRequest
from apps.central.services import contact_verification as cv
from apps.central.services import registration_metrics as metrics
from apps.central.services.mailer import MailerError, RegistrationMailer

MAX_ATTEMPTS = 5
STALE_SENDING_S = 600
BATCH = 20


@dataclass(slots=True)
class DispatchReport:
    sent: int = 0
    retried: int = 0
    failed: int = 0
    superseded: int = 0


def _aware(dt):
    return cv._aware(dt)


def _claim(db: Session, now: datetime) -> NotificationOutbox | None:
    stale = now - timedelta(seconds=STALE_SENDING_S)
    q = (
        select(NotificationOutbox)
        .where(NotificationOutbox.kind == cv.KIND)
        .where(
            ((NotificationOutbox.state == "pending") & (NotificationOutbox.available_at <= now))
            | ((NotificationOutbox.state == "sending") & (NotificationOutbox.updated_at <= stale))
        )
        .order_by(NotificationOutbox.available_at, NotificationOutbox.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    row = db.scalar(q)
    if row is None:
        return None
    row.state, row.attempts, row.updated_at = "sending", row.attempts + 1, now
    db.commit()
    return row


def _backoff(attempts: int) -> timedelta:
    return timedelta(minutes=4 ** max(0, attempts - 1))


def dispatch_due(
    factory: sessionmaker,
    mailer: RegistrationMailer,
    *,
    now: datetime | None = None,
    limit: int = BATCH,
) -> DispatchReport:
    rep = DispatchReport()
    for _ in range(limit):
        now_ = now or utcnow()
        with factory() as db:
            msg = _claim(db, now_)
            if msg is None:
                break
            mid, rid, to, nonce, attempts = (
                msg.id, msg.registration_id, msg.recipient, msg.payload.get("nonce"), msg.attempts,
            )  # fmt: skip
            reg = db.get(RegistrationRequest, rid)
            current = (
                reg is not None and reg.verified_at is None and reg.verification_nonce == nonce
                and reg.verification_expires_at is not None
                and _aware(reg.verification_expires_at) > now_
            )  # fmt: skip
            expires = reg.verification_expires_at if reg is not None else None
        if not current:
            _finish(factory, mid, "superseded", now_, error=None)
            rep.superseded += 1
            continue
        try:  # JASHTË transaksionit
            token = cv.derive_token(rid, nonce)
            mailer.send_verification(
                to=to, registration_id=str(rid), token=token, expires_at=_aware(expires)
            )
        except MailerError as e:
            _fail(factory, mid, attempts, now_, e.code, e.temporary, rep)
            continue
        except Exception:  # noqa: BLE001  (kod i qëndrueshëm; asnjë tekst përjashtimi)
            _fail(factory, mid, attempts, now_, "mailer_error", True, rep)
            continue
        _finish(factory, mid, "sent", now_, error=None)
        metrics.inc("registration_verification_email_sent_total")
        metrics.event("verification_email", registration_id=rid, result="sent", attempt=attempts)
        rep.sent += 1
    return rep


def _fail(factory, mid, attempts, now, code, temporary, rep: DispatchReport) -> None:
    metrics.inc("registration_verification_email_failed_total")
    if temporary and attempts < MAX_ATTEMPTS:
        _finish(factory, mid, "pending", now, error=code, available_at=now + _backoff(attempts))
        rep.retried += 1
    else:
        _finish(factory, mid, "failed", now, error=code)
        rep.failed += 1
    metrics.event("verification_email", result="failed", error_code=code, attempt=attempts)


def _finish(factory, mid, state, now, *, error, available_at=None) -> None:
    with factory() as db:
        row = db.get(NotificationOutbox, mid, populate_existing=True)
        if row is None or row.state != "sending":
            return  # u zëvendësua gjatë dërgimit (supersede nga resend)
        row.state, row.last_error_code, row.updated_at = state, error, now
        if state == "sent":
            row.sent_at = now
        if available_at is not None:
            row.available_at = available_at
        db.commit()
