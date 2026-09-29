from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import ROLE_PERMS, STAFF_ROLES, generate_key
from app.models.admin import ApiKey, KeyStatus
from app.services.wallet import Conflict, NotFound, WalletError


class InvalidKey(WalletError):
    code = "invalid_key"


def create_key(
    db: Session,
    name: str,
    role: str,
    owner_ref: str | None,
    created_by: str,
    expires_at: datetime | None = None,
) -> tuple[ApiKey, str]:
    """→ (rreshti, çelësi i plotë). Çelësi i plotë kthehet vetëm këtu, kurrë më vonë."""
    if role not in ROLE_PERMS:
        raise InvalidKey(f"unknown role '{role}'")
    if role == "client" and not owner_ref:
        raise InvalidKey("client keys must be bound to an owner_ref")
    if role in STAFF_ROLES and owner_ref:
        raise InvalidKey("staff keys cannot be bound to an owner_ref")
    if expires_at is not None and expires_at.astimezone(UTC) <= datetime.now(UTC):
        raise InvalidKey("expires_at must be in the future")
    full, prefix, digest = generate_key()
    key = ApiKey(
        prefix=prefix, key_hash=digest, name=name, role=role, owner_ref=owner_ref,
        expires_at=expires_at, created_by=created_by,
    )  # fmt: skip
    db.add(key)
    db.flush()
    return key, full


def revoke_key(db: Session, key_id: int) -> ApiKey:
    key = db.get(ApiKey, key_id, with_for_update=True)
    if key is None:
        raise NotFound("api key not found")
    if key.status == KeyStatus.REVOKED:
        raise Conflict("api key already revoked")
    key.status = KeyStatus.REVOKED
    db.flush()
    return key


def list_keys(db: Session) -> list[ApiKey]:
    return list(db.scalars(select(ApiKey).order_by(ApiKey.id)))
