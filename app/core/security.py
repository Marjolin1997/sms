"""Autentikim (API keys + bootstrap) dhe RBAC."""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.timeutil import as_utc
from app.models.admin import ApiKey, KeyStatus

ROLE_PERMS: dict[str, set[str]] = {
    "superadmin": {"*"},
    "finance": {
        "wallet:read", "wallet:write", "wallet:adjust", "topup:write", "topup:confirm",
        "audit:read", "monitor:read",
    },
    "pricing": {"rates:read", "rates:write", "routes:write", "plans:write", "monitor:read"},
    "approver": {"sender:review", "template:review", "monitor:read"},
    "support": {
        "wallet:read", "messages:read", "monitor:read", "audit:read", "switch:write",
        "contacts:read", "campaigns:read",
    },
    "client": {
        "wallet:read", "messages:send", "messages:read", "sender:request",
        "template:write", "template:render", "contacts:read", "contacts:write", "consent:write",
        "campaigns:read", "campaigns:write",
    },
}  # fmt: skip
STAFF_ROLES = set(ROLE_PERMS) - {"client"}
KEY_PREFIX = "sms"


@dataclass(frozen=True)
class Principal:
    actor: str
    role: str
    owner_ref: str | None = None  # i vendosur vetëm për role=client
    key_id: int | None = None

    def has(self, perm: str) -> bool:
        perms = ROLE_PERMS.get(self.role, set())
        return "*" in perms or perm in perms

    def owns(self, owner_ref: str) -> bool:
        return self.owner_ref is None or self.owner_ref == owner_ref

    def check_owner(self, owner_ref: str) -> None:
        # 404 e jo 403, që të mos zbulohet ekzistenca e burimeve të të tjerëve
        if not self.owns(owner_ref):
            raise HTTPException(404, {"code": "not_found", "message": "resource not found"})


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def generate_key() -> tuple[str, str, str]:
    """→ (çelësi i plotë, prefix, hash). Çelësi i plotë shfaqet vetëm një herë."""
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    return f"{KEY_PREFIX}_{prefix}_{secret}", prefix, hash_secret(secret)


def _unauthorized() -> HTTPException:
    return HTTPException(401, {"code": "unauthorized", "message": "invalid credentials"})


def _from_key(db: Session, token: str) -> Principal:
    parts = token.split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_PREFIX:
        raise _unauthorized()
    key = db.scalar(select(ApiKey).where(ApiKey.prefix == parts[1]))
    # krahasim me hash edhe kur çelësi mungon → kohë e njëtrajtshme
    expected = key.key_hash if key else hash_secret("")
    ok = hmac.compare_digest(hash_secret(parts[2]), expected)
    now = datetime.now(UTC)
    if not (key and ok and key.status == KeyStatus.ACTIVE):
        raise _unauthorized()
    if key.expires_at is not None and as_utc(key.expires_at) <= now:
        raise _unauthorized()
    last = as_utc(key.last_used_at) if key.last_used_at else None
    if last is None or now - last > timedelta(minutes=5):
        key.last_used_at = now
        db.commit()
    return Principal(f"key:{key.prefix}", key.role, key.owner_ref, key.id)


def current_principal(
    authorization: str = Header(default=""),
    x_admin_key: str = Header(default=""),
    db: Session = Depends(get_db),
) -> Principal:
    if authorization.lower().startswith("bearer "):
        return _from_key(db, authorization[7:].strip())
    # Bootstrap: vetëm për të krijuar çelësat e parë; hiqe SMS_ADMIN_API_KEY në prodhim.
    if x_admin_key and settings.admin_api_key:
        if hmac.compare_digest(x_admin_key, settings.admin_api_key):
            return Principal("bootstrap", "superadmin")
    raise _unauthorized()


def require(perm: str):
    def dep(p: Principal = Depends(current_principal)) -> Principal:
        if not p.has(perm):
            raise HTTPException(403, {"code": "forbidden", "message": f"missing {perm}"})
        return p

    return dep
