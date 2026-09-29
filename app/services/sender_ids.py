import re

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.messaging import ApprovalStatus, SenderId, SenderKind
from app.services import approvals
from app.services.wallet import Conflict, NotFound, WalletError

ALNUM = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9 ]{3,11}$")
NUMERIC = re.compile(r"^\+?[1-9]\d{2,14}$")
COUNTRY = re.compile(r"^[A-Za-z]{2}$")


class SenderNotAllowed(WalletError):
    code = "sender_not_allowed"


class InvalidSender(WalletError):
    code = "invalid_sender"


def classify(value: str) -> tuple[str, SenderKind]:
    if NUMERIC.match(value):
        return value.lstrip("+"), SenderKind.NUMERIC
    if ALNUM.match(value) and value == value.strip():
        return value, SenderKind.ALPHANUMERIC
    raise InvalidSender("sender must be 3-11 alphanumerics (with a letter) or a phone number")


def _key(country: str, value: str) -> str:
    return f"{country}:{value.lower()}"


def request(db: Session, owner_ref: str, country: str, value: str) -> SenderId:
    if not COUNTRY.match(country):
        raise InvalidSender("country must be ISO alpha-2")
    country = country.upper()
    norm, kind = classify(value)
    existing = db.scalar(
        select(SenderId).where(
            SenderId.owner_ref == owner_ref, SenderId.country == country, SenderId.value == norm
        )
    )
    if existing:
        if existing.status in (ApprovalStatus.REJECTED, ApprovalStatus.REVOKED):
            approvals.transition(existing, "resubmit", owner_ref)
            db.flush()
        return existing
    s = SenderId(owner_ref=owner_ref, country=country, value=norm, kind=kind)
    db.add(s)
    db.flush()
    return s


def _get(db: Session, sender_id: int) -> SenderId:
    s = db.get(SenderId, sender_id, with_for_update=True)
    if s is None:
        raise NotFound("sender id not found")
    return s


def approve(db: Session, sender_id: int, actor: str) -> SenderId:
    s = _get(db, sender_id)
    approvals.transition(s, "approve", actor)
    s.approved_key = _key(s.country, s.value)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise Conflict("sender id already approved for another account") from e
    return s


def reject(db: Session, sender_id: int, actor: str, reason: str) -> SenderId:
    s = _get(db, sender_id)
    approvals.transition(s, "reject", actor, reason)
    db.flush()
    return s


def revoke(db: Session, sender_id: int, actor: str, reason: str) -> SenderId:
    s = _get(db, sender_id)
    approvals.transition(s, "revoke", actor, reason)
    s.approved_key = None
    db.flush()
    return s


def assert_usable(db: Session, owner_ref: str, country: str, value: str) -> SenderId:
    """Thirret nga pipeline para dërgimit: sender i miratuar për këtë klient dhe shtet."""
    norm = value.lstrip("+") if NUMERIC.match(value) else value
    s = db.scalar(
        select(SenderId).where(
            SenderId.owner_ref == owner_ref,
            SenderId.country == country.upper(),
            SenderId.value == norm,
            SenderId.status == ApprovalStatus.APPROVED,
        )
    )
    if s is None:
        raise SenderNotAllowed("sender id is not approved for this account and country")
    return s
