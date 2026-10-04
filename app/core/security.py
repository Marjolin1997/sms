"""Autentikim (API keys + bootstrap) dhe RBAC."""

import hashlib
import hmac
import ipaddress
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.timeutil import as_utc
from app.models.admin import ApiKey, AuthFailure, KeyStatus

ROLE_PERMS: dict[str, set[str]] = {
    "superadmin": {"*"},
    "finance": {
        "wallet:read", "wallet:write", "wallet:adjust", "topup:write", "topup:confirm",
        "audit:read", "monitor:read", "billing:read", "billing:admin",
        "reports:read", "wallet:alert",
    },
    "pricing": {"rates:read", "rates:write", "routes:write", "plans:write", "monitor:read"},
    "approver": {"sender:review", "template:review", "monitor:read"},
    "support": {
        "wallet:read", "messages:read", "monitor:read", "audit:read", "switch:write",
        "contacts:read", "campaigns:read", "email:read",
        "webhooks:read", "events:read", "portal:read", "billing:read", "reports:read", "inbox:read",
    },
    "client": {
        "wallet:read", "messages:send", "messages:read", "sender:request",
        "template:write", "template:render", "contacts:read", "contacts:write", "consent:write",
        "campaigns:read", "campaigns:write", "email:send", "email:read", "email:write",
        "webhooks:read", "webhooks:write", "events:read", "keys:self", "portal:read",
        "inbox:read", "inbox:write",
        "billing:read", "billing:pay", "billing:profile", "reports:read", "wallet:alert",
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
    enterprise_id: uuid.UUID | None = None  # M1c: identiteti canonical i tenant-it (nga çelësi)

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


def client_ip(request: Request) -> str:
    """IP e klientit. X-Forwarded-For besohet vetëm sa proxy të besuar deklarohen."""
    hops = settings.trusted_proxy_hops
    if hops > 0:
        chain = [
            x.strip() for x in request.headers.get("x-forwarded-for", "").split(",") if x.strip()
        ]
        if len(chain) >= hops:
            return chain[-hops][:45]
    return (request.client.host if request.client else "unknown")[:45]


def ip_allowed(ip: str, allowed_cidrs: str | None) -> bool:
    if not allowed_cidrs:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in ipaddress.ip_network(c, strict=False) for c in json.loads(allowed_cidrs))


def _too_many_failures(db: Session, ip: str) -> bool:
    since = datetime.now(UTC) - timedelta(seconds=settings.auth_fail_window_s)
    n = db.scalar(
        select(func.count())
        .select_from(AuthFailure)
        .where(AuthFailure.ip == ip, AuthFailure.created_at > since)
    )
    return n >= settings.auth_max_failures


def _record_failure(db: Session, ip: str, prefix: str | None) -> None:
    """Në sesion të veçantë: duhet të mbetet edhe kur kërkesa kthen 401 dhe bën rollback."""
    with Session(bind=db.get_bind()) as s:
        s.add(AuthFailure(ip=ip, prefix=prefix))
        cutoff = datetime.now(UTC) - timedelta(seconds=settings.auth_fail_window_s * 6)
        s.execute(delete(AuthFailure).where(AuthFailure.created_at < cutoff))
        s.commit()


def _from_key(db: Session, token: str, ip: str) -> Principal:
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
    if not ip_allowed(ip, key.allowed_cidrs):
        raise HTTPException(
            403,
            {"code": "ip_not_allowed", "message": "this key can't be used from your IP address"},
        )
    last = as_utc(key.last_used_at) if key.last_used_at else None
    if last is None or now - last > timedelta(minutes=5):
        key.last_used_at = now
        db.commit()
    return Principal(f"key:{key.prefix}", key.role, key.owner_ref, key.id, key.enterprise_id)


def current_principal(
    request: Request,
    authorization: str = Header(default=""),
    x_admin_key: str = Header(default=""),
    db: Session = Depends(get_db),
) -> Principal:
    ip = client_ip(request)
    bearer = authorization.lower().startswith("bearer ")
    if (bearer or x_admin_key) and _too_many_failures(db, ip):
        raise HTTPException(
            429,
            {"code": "too_many_attempts", "message": "too many failed attempts, try again later"},
            headers={"Retry-After": str(settings.auth_fail_window_s)},
        )
    token = authorization[7:].strip() if bearer else ""
    try:
        if bearer:
            return _from_key(db, token, ip)
        # Bootstrap: vetëm për të krijuar çelësat e parë; hiqe SMS_ADMIN_API_KEY në prodhim.
        if x_admin_key and settings.admin_api_key:
            if hmac.compare_digest(x_admin_key, settings.admin_api_key):
                return Principal("bootstrap", "superadmin")
        raise _unauthorized()
    except HTTPException as e:
        if e.status_code == 401 and (bearer or x_admin_key):
            parts = token.split("_", 2)
            _record_failure(db, ip, parts[1][:12] if len(parts) == 3 else None)
        raise


# Veprime që lëvizin para, ndryshojnë çmime/rrugë, çelësa ose ndalojnë platformën.
SENSITIVE_PERMS = {
    "wallet:adjust", "topup:confirm", "keys:manage", "switch:write", "queue:resolve",
    "plans:write", "rates:write", "routes:write", "billing:admin",
}  # fmt: skip


def _step_up(db: Session, request: Request, p: Principal, perms: tuple[str, ...], code: str):
    """Hapi i dytë (TOTP) për çelësat e stafit te ndryshimet e ndjeshme (jo leximet). Bootstrap
    dhe klientët përjashtohen; kodi i përdorur një herë nuk pranohet dy herë."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return  # leximi nuk kërkon hap të dytë; vetëm ndryshimet
    if p.key_id is None or p.owner_ref is not None or not (set(perms) & SENSITIVE_PERMS):
        return
    key = db.get(ApiKey, p.key_id, with_for_update=True)
    if key is None or not key.totp_enabled:
        if settings.require_staff_2fa:
            raise HTTPException(
                403,
                {"code": "totp_enrollment_required",
                 "message": "enable two-factor authentication for this key first"},
            )  # fmt: skip
        return
    ip = client_ip(request)
    if _too_many_failures(db, ip):
        raise HTTPException(
            429,
            {"code": "too_many_attempts", "message": "too many failed attempts, try again later"},
        )
    if not code:
        raise HTTPException(
            403,
            {"code": "totp_required", "message": "a two-factor code is required (X-TOTP header)"},
        )
    from app.core import crypto, totp

    step = totp.verify(crypto.decrypt(key.totp_secret_enc).decode(), code, key.totp_last_step)
    if step is None:
        db.rollback()
        _record_failure(db, ip, key.prefix)
        raise HTTPException(403, {"code": "totp_invalid", "message": "invalid two-factor code"})
    key.totp_last_step = step
    db.commit()


def require(perm: str):
    def dep(
        request: Request,
        p: Principal = Depends(current_principal),
        x_totp: str = Header(default=""),
        db: Session = Depends(get_db),
    ) -> Principal:
        if not p.has(perm):
            raise HTTPException(403, {"code": "forbidden", "message": f"missing {perm}"})
        _step_up(db, request, p, (perm,), x_totp)
        return p

    dep.perms = (perm,)  # për testin e matricës së autorizimit
    return dep


def require_any(*perms: str):
    """Mjafton një nga lejet (p.sh. kush kërkon ose kush miraton sender ID)."""

    def dep(
        request: Request,
        p: Principal = Depends(current_principal),
        x_totp: str = Header(default=""),
        db: Session = Depends(get_db),
    ) -> Principal:
        if not any(p.has(x) for x in perms):
            raise HTTPException(
                403, {"code": "forbidden", "message": f"missing one of: {', '.join(perms)}"}
            )
        _step_up(db, request, p, perms, x_totp)
        return p

    dep.perms = perms
    return dep
