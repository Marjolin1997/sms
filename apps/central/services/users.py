"""Stafi i Central: krijim, autentikim, çaktivizim. Pa self-registration; pa commit këtu."""

import re
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.central.core import passwords
from apps.central.core.errors import AuthenticationFailed, Conflict, Invalid, NotFound
from apps.central.core.timeutil import utcnow
from apps.central.models.user import CentralUser, Role, UserStatus

EMAIL_MAX = 254
_EMAIL = re.compile(r"^[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+\.[^@\s\x00-\x1f\x7f]+$")


def normalize_email(value: str) -> str:
    """strip + lowercase; formë minimale `local@domain.tld`, max 254. Pa verifikim email-i."""
    if not isinstance(value, str):
        raise Invalid("email must be a string")
    email = value.strip().lower()
    if len(email) > EMAIL_MAX or not _EMAIL.match(email):
        raise Invalid("invalid email")
    return email


def create_user(
    db: Session, email: str, password: str, role: Role | str, *, now: datetime | None = None
) -> CentralUser:
    email = normalize_email(email)
    try:
        role = Role(role).value
    except ValueError as e:
        raise Invalid("invalid role") from e
    if db.scalar(select(CentralUser.id).where(CentralUser.email == email)) is not None:
        raise Conflict("user already exists")
    now = now or utcnow()
    user = CentralUser(
        email=email, password_hash=passwords.hash_password(password), role=role,
        status=UserStatus.ACTIVE.value, created_at=now, updated_at=now,
    )  # fmt: skip
    db.add(user)
    try:
        db.flush()
    except IntegrityError as e:  # garë me krijim paralel të të njëjtit email
        raise Conflict("user already exists") from e
    return user


def get(db: Session, user_id: uuid.UUID) -> CentralUser:
    user = db.get(CentralUser, user_id)
    if user is None:
        raise NotFound("user not found")
    return user


def get_by_email(db: Session, email: str) -> CentralUser | None:
    try:
        return db.scalar(select(CentralUser).where(CentralUser.email == normalize_email(email)))
    except Invalid:
        return None


def authenticate(db: Session, email: str, password: str) -> CentralUser:
    """→ përdoruesi aktiv ose AuthenticationFailed(reason). Kosto e njëjtë kur email s'ekziston."""
    user = get_by_email(db, email)
    if user is None:
        passwords.burn_verification(password)
        raise AuthenticationFailed("unknown_user")
    if not passwords.verify_password(user.password_hash, password):
        raise AuthenticationFailed("bad_password")
    if user.status != UserStatus.ACTIVE.value:
        raise AuthenticationFailed("disabled")
    if passwords.needs_rehash(user.password_hash):  # kosto e re e algoritmit → hash i ri
        user.password_hash = passwords.hash_password(password)
        db.flush()
    return user


def _set_status(db: Session, user_id: uuid.UUID, status: UserStatus, now) -> CentralUser:
    user = get(db, user_id)
    if user.status != status.value:
        user.status, user.updated_at = status.value, now or utcnow()
        db.flush()
    return user


def disable(db: Session, user_id: uuid.UUID, *, now: datetime | None = None) -> CentralUser:
    return _set_status(db, user_id, UserStatus.DISABLED, now)


def enable(db: Session, user_id: uuid.UUID, *, now: datetime | None = None) -> CentralUser:
    return _set_status(db, user_id, UserStatus.ACTIVE, now)
