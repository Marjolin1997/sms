import ipaddress
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.scope import Owner, belongs, owned, ref
from app.core.security import ROLE_PERMS, STAFF_ROLES, generate_key
from app.models.admin import ApiKey, KeyStatus
from app.services.wallet import Conflict, NotFound, WalletError


class InvalidKey(WalletError):
    code = "invalid_key"


MAX_CIDRS = 20


def normalize_cidrs(cidrs: list[str] | None) -> str | None:
    """Lista e lejuar IP/CIDR → JSON kanonik; bosh/None = pa kufizim."""
    if not cidrs:
        return None
    if len(cidrs) > MAX_CIDRS:
        raise InvalidKey(f"at most {MAX_CIDRS} allowed networks")
    out = []
    for c in cidrs:
        try:
            out.append(str(ipaddress.ip_network(c.strip(), strict=False)))
        except ValueError as e:
            raise InvalidKey(f"'{c}' is not a valid IP address or network") from e
    return json.dumps(sorted(set(out)))


def cidrs_of(key: ApiKey) -> list[str]:
    return json.loads(key.allowed_cidrs) if key.allowed_cidrs else []


def create_key(
    db: Session,
    name: str,
    role: str,
    owner_ref: str | None,
    created_by: str,
    expires_at: datetime | None = None,
    allowed_cidrs: list[str] | None = None,
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
        allowed_cidrs=normalize_cidrs(allowed_cidrs),
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


ROTATE_MAX_GRACE_MIN = 24 * 60


def rotate_key(db: Session, key_id: int, created_by: str, grace_minutes: int = 60):
    """Çelës i ri me të njëjtat rol/llogari/kufizime IP; i vjetri skadon pas `grace_minutes`
    (0 = revokohet menjëherë), që sistemi i klientit të përditësohet pa ndërprerje."""
    if not 0 <= grace_minutes <= ROTATE_MAX_GRACE_MIN:
        raise InvalidKey(f"grace period must be 0-{ROTATE_MAX_GRACE_MIN} minutes")
    old = db.get(ApiKey, key_id, with_for_update=True)
    if old is None:
        raise NotFound("api key not found")
    now = datetime.now(UTC)
    if old.status != KeyStatus.ACTIVE or (old.expires_at and old.expires_at.astimezone(UTC) <= now):
        raise Conflict("only an active key can be rotated")
    new, full = create_key(
        db, old.name, old.role, old.owner_ref, created_by, allowed_cidrs=cidrs_of(old)
    )
    if grace_minutes == 0:
        old.status = KeyStatus.REVOKED
    else:
        end = now + timedelta(minutes=grace_minutes)
        if old.expires_at is None or old.expires_at.astimezone(UTC) > end:
            old.expires_at = end
    db.flush()
    return old, new, full


def rotate_own_key(db: Session, owner: Owner, key_id: int, created_by: str, grace: int = 60):
    key = db.get(ApiKey, key_id)
    if key is None or not belongs(key, owner):
        raise NotFound("api key not found")
    return rotate_key(db, key_id, created_by, grace)


def list_keys(db: Session) -> list[ApiKey]:
    return list(db.scalars(select(ApiKey).order_by(ApiKey.id)))


MAX_SELF_SERVICE_KEYS = 20


def create_own_key(
    db: Session,
    owner: Owner,
    name: str,
    created_by: str,
    expires_at: datetime | None = None,
    allowed_cidrs: list[str] | None = None,
) -> tuple[ApiKey, str]:
    """Çelës vetë-shërbyes: gjithmonë role=client i lidhur me llogarinë e krijuesit
    (asnjë ngritje privilegjesh) dhe me kufi numri."""
    active = db.scalar(
        select(func.count())
        .select_from(ApiKey)
        .where(
            owned(ApiKey, owner),
            ApiKey.role == "client",
            ApiKey.status == KeyStatus.ACTIVE,
        )
    )
    if active >= MAX_SELF_SERVICE_KEYS:
        raise Conflict(f"at most {MAX_SELF_SERVICE_KEYS} active keys per account")
    return create_key(db, name, "client", ref(owner), created_by, expires_at, allowed_cidrs)


def list_own_keys(db: Session, owner: Owner) -> list[ApiKey]:
    return list(db.scalars(select(ApiKey).where(owned(ApiKey, owner)).order_by(ApiKey.id)))


def revoke_own_key(db: Session, owner: Owner, key_id: int) -> ApiKey:
    key = db.get(ApiKey, key_id)
    if key is None or not belongs(key, owner):
        raise NotFound("api key not found")
    return revoke_key(db, key_id)
