"""Regjistrimi dhe rivendosja e TOTP për çelësat e stafit."""

from sqlalchemy.orm import Session

from app.core import crypto, totp
from app.models.admin import ApiKey
from app.services.wallet import Conflict, NotFound, WalletError


class InvalidCode(WalletError):
    code = "totp_invalid"


def _key(db: Session, key_id: int) -> ApiKey:
    k = db.get(ApiKey, key_id, with_for_update=True)
    if k is None:
        raise NotFound("api key not found")
    return k


def enroll(db: Session, key_id: int) -> tuple[str, str]:
    """→ (sekreti base32, otpauth URI). Sekreti shfaqet vetëm tani; aktivizohet pas konfirmimit."""
    k = _key(db, key_id)
    if k.totp_enabled:
        raise Conflict("two-factor authentication is already enabled for this key")
    secret = totp.new_secret()
    k.totp_secret_enc = crypto.encrypt(secret.encode())
    k.totp_last_step = None
    db.flush()
    return secret, totp.provisioning_uri(secret, f"{k.name} ({k.prefix})")


def confirm(db: Session, key_id: int, code: str) -> None:
    k = _key(db, key_id)
    if k.totp_enabled:
        raise Conflict("two-factor authentication is already enabled for this key")
    if not k.totp_secret_enc:
        raise Conflict("start enrollment first")
    step = totp.verify(crypto.decrypt(k.totp_secret_enc).decode(), code)
    if step is None:
        raise InvalidCode("invalid two-factor code")
    k.totp_enabled = True
    k.totp_last_step = step
    db.flush()


def reset(db: Session, key_id: int) -> ApiKey:
    """Rivendos 2FA-në e një çelësi (humbi pajisjen). Vetëm me leje keys:manage + hapin e dytë."""
    k = _key(db, key_id)
    k.totp_secret_enc = None
    k.totp_enabled = False
    k.totp_last_step = None
    db.flush()
    return k
