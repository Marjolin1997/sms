"""Verifikimi i kontaktit të regjistrimit (M8-e). Vërteton qasjen te kutia postare e kontaktit;
NUK krijon CentralUser, përdorues Enterprise apo identitet autentikimi.

TOKENI nuk ruhet kurrë (as plaintext, as hash): `token = base64url(HMAC-SHA256(key, "regverify:v1:"
+ id + ":" + nonce))`, ku `key` = `CENTRAL_REGISTRATION_VERIFY_KEY` (vetëm në konfigurim). Në DB
jeton vetëm `verification_nonce` (i padobishëm pa çelësin) + `verification_expires_at`. Pasojat:
  * dërgimi i email-it pas commit-it mund ta rindërtojë tokenin (retry deterministik, pa sekret të
    ruajtur ose "sealed" në outbox);
  * rotacioni (resend) = nonce i ri ⇒ tokeni i vjetër dështon vetvetiu;
  * një përdorim: sukses ⇒ `verified_at` vendoset, nonce pastrohet ⇒ çdo token tjetër/replay dështon;
  * ndryshimi i çelësit invalidon tokenat e pa-konsumuar (afat i shkurtër: pranueshëm).
Çdo dështim (id/token i gabuar, i skaduar, i konsumuar, i panjohur) ⇒ i njëjti `VerificationFailed`.
Auditi i verifikimit: aktor sistemi `system:registration_verification` (përdoruesi publik nuk është staf).
"""

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.errors import CentralError, TooManyRequests
from apps.central.core.timeutil import utcnow
from apps.central.models.registration import (
    SUBMITTED,
    NotificationOutbox,
    RegistrationRequest,
)
from apps.central.services import audit
from apps.central.services import registrations as reg

KIND = "registration_verification"
LABEL = "system:registration_verification"
ACTION_VERIFY = "registration.verify"
MIN_KEY_LEN = 32
RESEND_MIN_INTERVAL_S = 60
RESEND_MAX_PER_24H = 5  # përfshirë dërgimin fillestar


class VerificationFailed(CentralError):
    """Gabim i vetëm, i pazbuluese, për çdo dështim verifikimi."""


class VerificationUnavailable(CentralError):
    """Verifikimi s'është i konfiguruar (çelës/mailer)."""


def key_configured() -> bool:
    return len(settings.registration_verify_key or "") >= MIN_KEY_LEN


def configured() -> bool:
    """Verifikimi është i mundur: çelës + mailer jo-disabled."""
    return key_configured() and settings.mailer != "disabled"


def derive_token(registration_id, nonce: str) -> str:
    if not key_configured():
        raise VerificationUnavailable("verification key is not configured")
    msg = f"regverify:v1:{registration_id}:{nonce}".encode()
    digest = hmac.new(settings.registration_verify_key.encode(), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=utcnow().tzinfo)


def issue(
    db: Session, row: RegistrationRequest, *, now: datetime | None = None
) -> NotificationOutbox:
    """Rrotullon nonce-in (tokeni i mëparshëm bëhet i pavlefshëm), vendos afatin dhe vë në outbox
    NJË mesazh `pending` (mesazhet e mëparshme jo-të-dërguara ⇒ `superseded`). Pa SMTP këtu, pa commit."""
    if not configured():
        raise VerificationUnavailable("contact verification is not configured")
    now = now or utcnow()
    nonce = secrets.token_urlsafe(16)[:22]
    row.verification_nonce = nonce
    row.verification_expires_at = now + timedelta(minutes=settings.registration_verify_ttl_minutes)
    row.updated_at = now
    db.execute(
        update(NotificationOutbox)
        .where(NotificationOutbox.registration_id == row.id, NotificationOutbox.kind == KIND,
               NotificationOutbox.state.in_(("pending", "sending", "failed")))
        .values(state="superseded", updated_at=now)
    )  # fmt: skip
    msg = NotificationOutbox(
        kind=KIND, registration_id=row.id, recipient=row.contact_email, payload={"nonce": nonce},
        state="pending", available_at=now, created_at=now, updated_at=now,
    )  # fmt: skip
    db.add(msg)
    db.flush()
    return msg


def resend(db: Session, request_id, *, now: datetime | None = None) -> bool:
    """Për pronarin e vërtetuar të kërkesës (thirrësi ka verifikuar access token). → True nëse u vu
    në radhë dërgim i ri; False nëse kontakti është tashmë i verifikuar (no-op). Kufij: ≥60s nga
    dërgimi i fundit dhe ≤5 në 24h (kundër mail-bombing); përndryshe `TooManyRequests`."""
    now = now or utcnow()
    row = reg.get(db, request_id, for_update=True)  # serializon resend-et paralele
    if row.verified_at is not None:
        return False
    last = db.scalar(
        select(func.max(NotificationOutbox.created_at)).where(
            NotificationOutbox.registration_id == row.id, NotificationOutbox.kind == KIND
        )
    )
    if last is not None and _aware(last) > now - timedelta(seconds=RESEND_MIN_INTERVAL_S):
        raise TooManyRequests("verification resend is too frequent")
    used = db.scalar(
        select(func.count()).select_from(NotificationOutbox).where(
            NotificationOutbox.registration_id == row.id, NotificationOutbox.kind == KIND,
            NotificationOutbox.created_at > now - timedelta(hours=24))
    )  # fmt: skip
    if used >= RESEND_MAX_PER_24H:
        raise TooManyRequests("verification resend limit reached")
    issue(db, row, now=now)
    return True


@dataclass(frozen=True, slots=True)
class VerifyResult:
    request: RegistrationRequest
    auto_approved: bool


def verify(db: Session, request_id, token, *, now: datetime | None = None) -> VerifyResult:
    """Konsumon tokenin: vendos `verified_at` një herë, pastron nonce-in dhe — nëse politikat janë
    të gjitha `automatic` — auto-miraton (aktor sistemi). Çdo dështim ⇒ `VerificationFailed`."""
    now = now or utcnow()
    try:
        row = reg.get(db, request_id, for_update=True)
    except CentralError:
        row = None
    nonce = row.verification_nonce if row is not None else None
    expected = (
        derive_token(row.id, nonce) if row is not None and nonce and key_configured() else "!" * 43
    )
    given = token if isinstance(token, str) and 0 < len(token) <= 256 else ""
    same = hmac.compare_digest(expected.encode(), given.encode())  # kohë e qëndrueshme
    if (
        not same or row is None or not nonce or row.verified_at is not None
        or row.verification_expires_at is None or _aware(row.verification_expires_at) <= now
    ):  # fmt: skip
        raise VerificationFailed("verification failed")
    row.verified_at, row.verification_nonce, row.verification_expires_at = now, None, None
    row.updated_at = now
    db.flush()
    audit.record_system(
        db, label=LABEL, action=ACTION_VERIFY, resource_type=reg.RESOURCE, resource_id=row.id,
        detail={"contact_verified": True}, now=now,
    )  # fmt: skip
    approved = row.status == SUBMITTED and reg.auto_approve_verified(db, row, now=now)
    return VerifyResult(row, approved)
