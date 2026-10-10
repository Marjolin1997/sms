"""Katalogu i produkteve (Central): konkret, pa framework; asnjë commit këtu; pa fshirje."""

import re
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.central.core.errors import Conflict, Invalid, NotFound
from apps.central.core.timeutil import utcnow
from apps.central.models.product import Channel, Product, ProductStatus

CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")  # lowercase, 2..32, pa hapësira, pa vizë
NAME_MAX, DESCRIPTION_MAX = 120, 1000
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_CONTROL_TEXT = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # përshkrimi lejon \n dhe \t
UNSET = object()


def normalize_code(value: str) -> str:
    """strip + lowercase, pastaj `[a-z][a-z0-9_]{1,31}`. Hapësira të brendshme/simbole → Invalid."""
    if not isinstance(value, str):
        raise Invalid("code must be a string")
    code = value.strip().lower()
    if not CODE_RE.match(code):
        raise Invalid("code must match [a-z][a-z0-9_]{1,31} (lowercase, no spaces)")
    return code


def normalize_name(value: str) -> str:
    if not isinstance(value, str):
        raise Invalid("name must be a string")
    name = value.strip()
    if not name or len(name) > NAME_MAX or _CONTROL.search(name):
        raise Invalid(f"name must be 1..{NAME_MAX} characters without control characters")
    return name


def normalize_description(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise Invalid("description must be a string")
    text = value.strip()
    if len(text) > DESCRIPTION_MAX or _CONTROL_TEXT.search(text):
        raise Invalid(f"description must be at most {DESCRIPTION_MAX} characters, no control chars")
    return text or None


def _enum(cls, value, field: str) -> str:
    try:
        return cls(value).value
    except ValueError as e:
        raise Invalid(f"invalid {field}") from e


def snapshot(p: Product) -> dict:
    return {"name": p.name, "description": p.description, "status": p.status}


def create(
    db: Session,
    code: str,
    name: str,
    channel: Channel | str,
    description: str | None = None,
    status: ProductStatus | str = ProductStatus.ACTIVE,
    *,
    now: datetime | None = None,
) -> Product:
    code, name = normalize_code(code), normalize_name(name)
    channel_v, status_v = _enum(Channel, channel, "channel"), _enum(ProductStatus, status, "status")
    description = normalize_description(description)
    if db.scalar(select(Product.id).where(Product.code == code)) is not None:
        raise Conflict("product code already exists")
    now = now or utcnow()
    p = Product(code=code, name=name, description=description, channel=channel_v, status=status_v,
                created_at=now, updated_at=now)  # fmt: skip
    db.add(p)
    try:
        db.flush()
    except IntegrityError as e:  # garë me krijim paralel të të njëjtit code
        raise Conflict("product code already exists") from e
    return p


def get(db: Session, product_id: uuid.UUID) -> Product:
    p = db.get(Product, product_id)
    if p is None:
        raise NotFound("product not found")
    return p


def get_by_code(db: Session, code: str) -> Product:
    p = db.scalar(select(Product).where(Product.code == normalize_code(code)))
    if p is None:
        raise NotFound("product not found")
    return p


def list_products(
    db: Session,
    *,
    status: ProductStatus | None = None,
    channel: Channel | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Product]:
    q = select(Product)
    if status is not None:
        q = q.where(Product.status == ProductStatus(status).value)
    if channel is not None:
        q = q.where(Product.channel == Channel(channel).value)
    q = q.order_by(Product.created_at, Product.id).limit(max(1, min(limit, 500)))
    return list(db.scalars(q.offset(max(0, offset))))


def update(
    db: Session,
    product_id: uuid.UUID,
    *,
    name=UNSET,
    description=UNSET,
    status=UNSET,
    now: datetime | None = None,
) -> tuple[Product, dict]:
    """Ndryshon vetëm `name`/`description`/`status` (`code`, `channel` janë të pandryshueshëm).
    → (produkti, {"before": {...}, "after": {...}} vetëm fushat e ndryshuara; {} = pa ndryshim)."""
    p = get(db, product_id)
    new = {}
    if name is not UNSET:
        new["name"] = normalize_name(name)
    if description is not UNSET:
        new["description"] = normalize_description(description)
    if status is not UNSET:
        new["status"] = _enum(ProductStatus, status, "status")
    before = snapshot(p)
    changed = {k: v for k, v in new.items() if before[k] != v}
    if not changed:
        return p, {}
    for k, v in changed.items():
        setattr(p, k, v)
    p.updated_at = now or utcnow()
    db.flush()
    return p, {"before": {k: before[k] for k in changed}, "after": changed}
