"""2FA me TOTP (RFC 6238, SHA-1, 6 shifra, 30 s) dhe kode rikuperimi. Vetëm stdlib."""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import UTC, datetime
from urllib.parse import quote

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.config import settings
from app.core.security import hash_secret, mfa_required_for
from app.models.users import User, UserRecoveryCode, UserToken
from app.services import auth
from app.services.wallet import Conflict, WalletError

STEP = 30
RECOVERY_COUNT = 10
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # pa shkronja të ngjashme (i l o 0 1)


class InvalidCode(WalletError):
    code = "invalid_code"


class MfaExpired(WalletError):
    code = "mfa_expired"


# --- TOTP -----------------------------------------------------------------------------


def totp_at(secret: bytes, step: int, digits: int = 6) -> str:
    mac = hmac.new(secret, struct.pack(">Q", step), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    n = (struct.unpack(">I", mac[off : off + 4])[0] & 0x7FFFFFFF) % 10**digits
    return f"{n:0{digits}d}"


def verify_totp(secret: bytes, code: str, last_step: int, now: float | None = None) -> int | None:
    """Hapi që u përputh, ose None. Pranon ±1 hap (ndryshim ore) dhe kurrë hap ≤ last_step
    (një kod nuk përdoret dy herë). Krahasohen të gjitha hapat, pa dalje të hershme."""
    cur = int((time.time() if now is None else now) // STEP)
    hit = None
    for step in (cur - 1, cur, cur + 1):
        if hmac.compare_digest(totp_at(secret, step), code) and step > last_step:
            hit = step
    return hit


def _secret(u: User) -> bytes:
    return crypto.decrypt(u.totp_secret_enc)


def otpauth_uri(email: str, secret_b32: str) -> str:
    issuer = settings.system_from_name
    return (
        f"otpauth://totp/{quote(issuer)}:{quote(email)}?secret={secret_b32}"
        f"&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period={STEP}"
    )


# --- Kodet e rikuperimit ---------------------------------------------------------------


def _norm_recovery(code: str) -> str:
    return "".join(c for c in code.lower() if c.isalnum())


def _new_recovery(db: Session, u: User) -> list[str]:
    db.execute(delete(UserRecoveryCode).where(UserRecoveryCode.user_id == u.id))
    codes = []
    for _ in range(RECOVERY_COUNT):
        raw = "".join(secrets.choice(_ALPHABET) for _ in range(16))
        codes.append("-".join(raw[i : i + 4] for i in range(0, 16, 4)))
        db.add(UserRecoveryCode(user_id=u.id, code_hash=hash_secret(_norm_recovery(codes[-1]))))
    db.flush()
    return codes


def recovery_left(db: Session, user_id: int) -> int:
    return int(
        db.scalar(
            select(func.count(UserRecoveryCode.id)).where(
                UserRecoveryCode.user_id == user_id, UserRecoveryCode.used_at.is_(None)
            )
        )
        or 0
    )


# --- Verifikimi i faktorit të dytë -------------------------------------------------------


def check_second_factor(db: Session, u: User, code: str) -> str | None:
    """'totp' | 'recovery' kur kodi vlen, ndryshe None. Dështimet numërohen te bllokimi i
    përbashkët me fjalëkalimin; thirrësi bën commit edhe pas None."""
    now = datetime.now(UTC)
    if auth.is_locked(u):
        return None
    c = code.strip().replace(" ", "")
    method = None
    if c.isdigit() and len(c) == 6:
        step = verify_totp(_secret(u), c, u.totp_last_step)
        if step is not None:
            u.totp_last_step, method = step, "totp"
    else:
        rc = db.scalar(
            select(UserRecoveryCode)
            .where(
                UserRecoveryCode.user_id == u.id,
                UserRecoveryCode.code_hash == hash_secret(_norm_recovery(c)),
                UserRecoveryCode.used_at.is_(None),
            )
            .with_for_update()
        )
        if rc is not None and len(_norm_recovery(c)) == 16:
            rc.used_at, method = now, "recovery"
    if method is None:
        auth.register_failure(u, now)
    return method


def complete_login(
    db: Session, mfa_token: str, code: str, user_agent: str | None = None
) -> tuple[auth.LoginResult, str] | None:
    """Hapi i dytë i hyrjes. None = kod i gabuar (mund të riprovohet brenda 5 minutave);
    MfaExpired = filloje nga fjalëkalimi."""
    try:
        t = auth._valid_token(db, mfa_token, auth.MFA_KINDS)
    except auth.InvalidToken as e:
        raise MfaExpired("Your sign-in took too long. Please start again.") from e
    u = auth.get_user(db, t.user_id)
    if u.status != auth.UserStatus.ACTIVE or u.totp_enabled_at is None:
        raise MfaExpired("Your sign-in took too long. Please start again.")
    method = check_second_factor(db, u, code)
    if method is None:
        return None
    t.used_at = datetime.now(UTC)
    u.failed_logins, u.locked_until = 0, None
    res = auth.start_session(db, u, t.kind == "mfa_r", user_agent, mfa_done=True)
    return res, method


# --- Aktivizim / çaktivizim --------------------------------------------------------------


def status(db: Session, u: User) -> dict:
    return {
        "enabled": u.totp_enabled_at is not None,
        "required": mfa_required_for(u.role),
        "recovery_left": recovery_left(db, u.id) if u.totp_enabled_at else 0,
    }


def begin_setup(db: Session, user_id: int, password: str) -> dict:
    u = auth.get_user(db, user_id)
    if not auth.verify_password(password, u.password_hash):
        raise auth.InvalidLogin("Your password is not correct.")
    if u.totp_enabled_at is not None:
        raise Conflict("Two-factor is already on. Turn it off first to set it up again.")
    raw = secrets.token_bytes(20)
    u.totp_secret_enc = crypto.encrypt(raw)
    u.totp_last_step = 0
    db.flush()
    b32 = base64.b32encode(raw).decode().rstrip("=")
    return {"secret": b32, "uri": otpauth_uri(u.email, b32)}


def enable(db: Session, user_id: int, code: str, keep_session: int | None) -> list[str]:
    u = auth.get_user(db, user_id)
    if u.totp_enabled_at is not None:
        raise Conflict("Two-factor is already on.")
    if not u.totp_secret_enc:
        raise Conflict("Start the setup first.")
    step = verify_totp(_secret(u), code.strip().replace(" ", ""), 0)
    if step is None:
        raise InvalidCode(
            "That code isn't right. Check the time on your phone and try the next one."
        )
    u.totp_enabled_at, u.totp_last_step = datetime.now(UTC), step
    auth.revoke_all(db, u.id, except_id=keep_session)  # pajisjet e tjera duhet të hyjnë me kod
    return _new_recovery(db, u)


def _confirm(db: Session, u: User, password: str, code: str) -> None:
    if not auth.verify_password(password, u.password_hash):
        raise auth.InvalidLogin("Your password is not correct.")
    if check_second_factor(db, u, code) is None:
        db.commit()  # numëruesi i dështimeve ruhet
        raise InvalidCode("That code isn't right.")


def disable(db: Session, user_id: int, password: str, code: str, keep_session: int | None) -> None:
    u = auth.get_user(db, user_id)
    if u.totp_enabled_at is None:
        raise Conflict("Two-factor is not on.")
    if mfa_required_for(u.role):
        raise Conflict("Two-factor is required for your role, so it can't be turned off.")
    _confirm(db, u, password, code)
    _clear(db, u)
    auth.revoke_all(db, u.id, except_id=keep_session)


def regenerate_recovery(db: Session, user_id: int, password: str, code: str) -> list[str]:
    u = auth.get_user(db, user_id)
    if u.totp_enabled_at is None:
        raise Conflict("Two-factor is not on.")
    _confirm(db, u, password, code)
    return _new_recovery(db, u)


def _clear(db: Session, u: User) -> None:
    u.totp_secret_enc = u.totp_enabled_at = None
    u.totp_last_step = 0
    db.execute(delete(UserRecoveryCode).where(UserRecoveryCode.user_id == u.id))
    db.execute(
        UserToken.__table__.update()
        .where(UserToken.user_id == u.id, UserToken.kind.in_(auth.MFA_KINDS))
        .values(used_at=datetime.now(UTC))
    )


def admin_reset(db: Session, user_id: int) -> User:
    """Pajisje e humbur pa kode rikuperimi: stafi e heq 2FA dhe mbyll të gjitha sesionet."""
    u = auth.get_user(db, user_id)
    _clear(db, u)
    auth.revoke_all(db, u.id)
    return u
