"""Login me email + fjalëkalim: scrypt, bllokim pas tentativave të dështuara, sesione, ftesa."""

import base64
import hashlib
import hmac
import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.security import ROLE_PERMS, STAFF_ROLES, hash_secret
from app.core.timeutil import as_utc
from app.models.users import User, UserSession, UserStatus, UserToken
from app.services.wallet import Conflict, NotFound, WalletError

SESSION_PREFIX = "sess"
SESSION_TTL = timedelta(hours=12)
REMEMBER_TTL = timedelta(days=30)
TOKEN_TTL = timedelta(hours=72)
MAX_FAILED = 5
LOCK_FOR = timedelta(minutes=15)
MAX_SESSIONS = 20  # më të vjetrat revokohen, që një pajisje e harruar të mos grumbullohet
_N, _R, _P = 2**14, 8, 1
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
_COMMON = {
    "password123", "1234567890", "qwertyuiop", "password12", "letmein123", "iloveyou12",
    "administrator", "welcome123", "abc1234567", "passw0rd123",
}  # fmt: skip


class InvalidLogin(WalletError):
    code = "invalid_credentials"


class WeakPassword(WalletError):
    code = "weak_password"


class InvalidUser(WalletError):
    code = "invalid_user"


class InvalidToken(WalletError):
    code = "invalid_token"


# --- Fjalëkalimet ---------------------------------------------------------------------


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b = base64.b64encode
    return f"scrypt${_N}${_R}${_P}${b(salt).decode()}${b(h).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Kohë e njëtrajtshme edhe kur nuk ka hash (përdorues i panjohur ose ftesë e pranuar)."""
    if not stored:
        stored = _DUMMY
        ok = False
    else:
        ok = True
    try:
        _, n, r, p, salt, want = stored.split("$")
        got = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return ok and hmac.compare_digest(got, base64.b64decode(want))
    except (ValueError, TypeError):
        return False


_DUMMY = hash_password("dummy-password-for-constant-time")


def check_password_policy(password: str, email: str = "") -> None:
    if len(password) < 10:
        raise WeakPassword("Use at least 10 characters.")
    if len(password) > 128:
        raise WeakPassword("Use at most 128 characters.")
    low = password.lower()
    local = email.split("@")[0].lower()
    if low in _COMMON or len(set(password)) < 4 or (local and len(local) >= 3 and local in low):
        raise WeakPassword("That password is too easy to guess. Try a longer phrase.")


# --- Përdoruesit ----------------------------------------------------------------------


def normalize_email(email: str) -> str:
    e = email.strip().lower()
    if len(e) > 254 or not _EMAIL.match(e):
        raise InvalidUser("Enter a valid email address.")
    return e


def _token(db: Session, user: User, kind: str) -> str:
    """Anulon tokenat e pa-përdorur të mëparshëm dhe krijon një të ri (kthehet vetëm një herë)."""
    now = datetime.now(UTC)
    db.execute(
        update(UserToken)
        .where(UserToken.user_id == user.id, UserToken.used_at.is_(None))
        .values(used_at=now)
    )
    raw = secrets.token_urlsafe(32)
    db.add(
        UserToken(
            user_id=user.id, kind=kind, token_hash=hash_secret(raw), expires_at=now + TOKEN_TTL
        )
    )
    db.flush()
    return raw


def create_user(
    db: Session, email: str, role: str, owner_ref: str | None, created_by: str
) -> tuple[User, str]:
    """→ (përdoruesi, token ftese). Përdoruesi vendos vetë fjalëkalimin."""
    email = normalize_email(email)
    if role not in ROLE_PERMS:
        raise InvalidUser(f"Unknown role '{role}'.")
    if role == "client" and not owner_ref:
        raise InvalidUser("Client users must belong to an account.")
    if role in STAFF_ROLES and owner_ref:
        raise InvalidUser("Staff users cannot belong to a customer account.")
    if db.scalar(select(User.id).where(User.email == email)):
        raise Conflict("A user with this email already exists.")
    u = User(email=email, role=role, owner_ref=owner_ref, created_by=created_by)
    db.add(u)
    db.flush()
    return u, _token(db, u, "invite")


def get_user(db: Session, user_id: int) -> User:
    u = db.get(User, user_id, with_for_update=True)
    if u is None:
        raise NotFound("user not found")
    return u


def reset_token(db: Session, user_id: int) -> str:
    u = get_user(db, user_id)
    if u.status != UserStatus.ACTIVE:
        raise Conflict("The user is disabled. Enable them first.")
    return _token(db, u, "reset" if u.password_hash else "invite")


def set_status(db: Session, user_id: int, enabled: bool) -> User:
    u = get_user(db, user_id)
    u.status = UserStatus.ACTIVE if enabled else UserStatus.DISABLED
    if not enabled:
        revoke_all(db, u.id)
    db.flush()
    return u


def list_users(db: Session) -> list[User]:
    return list(db.scalars(select(User).order_by(User.id)))


# --- Sesionet -------------------------------------------------------------------------


def _new_session(
    db: Session, user: User, ttl: timedelta, user_agent: str | None
) -> tuple[UserSession, str]:
    now = datetime.now(UTC)
    live = db.scalars(
        select(UserSession)
        .where(
            UserSession.user_id == user.id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        )
        .order_by(UserSession.id.desc())
    ).all()
    for old in live[MAX_SESSIONS - 1 :]:
        old.revoked_at = now
    prefix, secret = secrets.token_hex(4), secrets.token_urlsafe(32)
    s = UserSession(
        user_id=user.id, prefix=prefix, token_hash=hash_secret(secret),
        user_agent=(user_agent or "")[:200] or None, expires_at=now + ttl,
    )  # fmt: skip
    db.add(s)
    db.flush()
    return s, f"{SESSION_PREFIX}_{prefix}_{secret}"


def login(
    db: Session, email: str, password: str, remember: bool = False, user_agent: str | None = None
) -> tuple[User, UserSession, str] | None:
    """None kur kredencialet s'vlejnë (mesazh i njëjtë për email të panjohur, fjalëkalim të gabuar
    ose llogari të bllokuar, që të mos zbulohet cilat email ekzistojnë). Thirrësi bën commit
    edhe pas None, që numëruesi i dështimeve të ruhet."""
    email = email.strip().lower()
    u = db.scalar(select(User).where(User.email == email).with_for_update())
    ok = verify_password(password, u.password_hash if u else None)
    now = datetime.now(UTC)
    if u is None:
        return None
    locked = u.locked_until is not None and as_utc(u.locked_until) > now
    if not ok or locked or u.status != UserStatus.ACTIVE:
        if not locked and u.status == UserStatus.ACTIVE and u.password_hash:
            u.failed_logins += 1
            if u.failed_logins >= MAX_FAILED:
                u.failed_logins, u.locked_until = 0, now + LOCK_FOR
        return None
    u.failed_logins, u.locked_until, u.last_login_at = 0, None, now
    s, token = _new_session(db, u, REMEMBER_TTL if remember else SESSION_TTL, user_agent)
    return u, s, token


def purge_expired(db: Session, days: int = 30) -> int:
    """Sesione dhe tokena të skaduar/revokuar/përdorur prej më shumë se `days` ditësh."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    n = db.execute(
        delete(UserSession).where(
            (UserSession.expires_at < cutoff) | (UserSession.revoked_at < cutoff)
        )
    ).rowcount
    n += db.execute(
        delete(UserToken).where((UserToken.expires_at < cutoff) | (UserToken.used_at < cutoff))
    ).rowcount
    return n


def is_locked(u: User) -> bool:
    return u.locked_until is not None and as_utc(u.locked_until) > datetime.now(UTC)


def revoke_all(db: Session, user_id: int, except_id: int | None = None) -> None:
    q = update(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
    if except_id is not None:
        q = q.where(UserSession.id != except_id)
    db.execute(q.values(revoked_at=datetime.now(UTC)))


def revoke_session(db: Session, user_id: int, session_id: int) -> None:
    s = db.get(UserSession, session_id)
    if s is None or s.user_id != user_id:
        raise NotFound("session not found")
    if s.revoked_at is None:
        s.revoked_at = datetime.now(UTC)


def active_sessions(db: Session, user_id: int) -> list[UserSession]:
    return list(
        db.scalars(
            select(UserSession)
            .where(
                UserSession.user_id == user_id,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > datetime.now(UTC),
            )
            .order_by(UserSession.last_seen_at.desc())
        )
    )


def change_password(db: Session, user_id: int, current: str, new: str, keep_session: int | None):
    u = get_user(db, user_id)
    if not verify_password(current, u.password_hash):
        raise InvalidLogin("Your current password is not correct.")
    check_password_policy(new, u.email)
    if verify_password(new, u.password_hash):
        raise WeakPassword("Choose a password you haven't used just now.")
    u.password_hash = hash_password(new)
    revoke_all(db, u.id, except_id=keep_session)  # pajisjet e tjera dalin


# --- Ftesa / rivendosje ---------------------------------------------------------------


def _valid_token(db: Session, raw: str) -> UserToken:
    t = db.scalar(
        select(UserToken).where(UserToken.token_hash == hash_secret(raw)).with_for_update()
    )
    if t is None or t.used_at is not None or as_utc(t.expires_at) <= datetime.now(UTC):
        raise InvalidToken("This link has expired or was already used. Ask for a new one.")
    return t


def token_info(db: Session, raw: str) -> dict:
    t = _valid_token(db, raw)
    u = db.get(User, t.user_id)
    if u.status != UserStatus.ACTIVE:
        raise InvalidToken("This link is no longer valid.")
    return {"email": u.email, "kind": t.kind, "expires_at": t.expires_at}


def accept_token(
    db: Session, raw: str, password: str, user_agent: str | None = None
) -> tuple[User, UserSession, str]:
    t = _valid_token(db, raw)
    u = get_user(db, t.user_id)
    if u.status != UserStatus.ACTIVE:
        raise InvalidToken("This link is no longer valid.")
    check_password_policy(password, u.email)
    u.password_hash = hash_password(password)
    u.failed_logins, u.locked_until = 0, None
    t.used_at = datetime.now(UTC)
    revoke_all(db, u.id)  # rivendosja mbyll çdo sesion të vjetër
    u.last_login_at = datetime.now(UTC)
    s, token = _new_session(db, u, SESSION_TTL, user_agent)
    return u, s, token
