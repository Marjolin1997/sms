"""Operacionet e Enterprise në Central (konkrete, pa framework repository/UoW).

Çdo funksion punon në transaksionin e thirrësit (asnjë commit këtu). Pa fshirje fizike: Enterprise
do të ketë produkte, pagesa, audit dhe histori përdorimi; çaktivizimi është `suspend`.
Pa actor/audit ende (s'ka auth në Central); operacionet janë të pastra: auditi shtohet më vonë.
"""

import re
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.central.core.errors import Conflict, Invalid, NotFound
from apps.central.core.timeutil import utcnow
from apps.central.models.enterprise import Enterprise, EnterpriseStatus

NAME_MAX = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def normalize_name(value: str) -> str:
    """Vetëm `strip`; pa normalizim tjetër. Bosh, >200 ose me karaktere kontrolli → Invalid."""
    if not isinstance(value, str):
        raise Invalid("name must be a string")
    name = value.strip()
    if not name or len(name) > NAME_MAX or _CONTROL.search(name):
        raise Invalid(f"name must be 1..{NAME_MAX} characters without control characters")
    return name


def _uuid(value: uuid.UUID | str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError as e:
        raise Invalid("invalid enterprise id") from e


def create(
    db: Session, name: str, *, enterprise_id: uuid.UUID | None = None, now: datetime | None = None
) -> Enterprise:
    """Krijon Enterprise (status `active`). `enterprise_id` eksplicit vetëm për migrimin e ardhshëm
    të UUID-ve ekzistuese; përndryshe Central e gjeneron (UUIDv4). ID e dyfishtë → Conflict."""
    name = normalize_name(name)
    eid = _uuid(enterprise_id) if enterprise_id is not None else uuid.uuid4()
    if db.get(Enterprise, eid) is not None:
        raise Conflict("enterprise id already exists")
    now = now or utcnow()
    ent = Enterprise(id=eid, name=name, status=EnterpriseStatus.ACTIVE.value,
                     created_at=now, updated_at=now)  # fmt: skip
    db.add(ent)
    try:
        db.flush()
    except IntegrityError as e:  # garë me një krijim paralel të të njëjtit id
        raise Conflict("enterprise id already exists") from e
    return ent


def get(db: Session, enterprise_id: uuid.UUID | str) -> Enterprise:
    ent = db.get(Enterprise, _uuid(enterprise_id))
    if ent is None:
        raise NotFound("enterprise not found")
    return ent


def list_enterprises(
    db: Session, *, status: EnterpriseStatus | None = None, limit: int = 100, offset: int = 0
) -> list[Enterprise]:
    """Renditje e qëndrueshme: `created_at`, pastaj `id`."""
    q = select(Enterprise)
    if status is not None:
        q = q.where(Enterprise.status == EnterpriseStatus(status).value)
    q = q.order_by(Enterprise.created_at, Enterprise.id).limit(max(1, min(limit, 500)))
    return list(db.scalars(q.offset(max(0, offset))))


def rename(
    db: Session, enterprise_id: uuid.UUID | str, name: str, *, now: datetime | None = None
) -> Enterprise:
    ent = get(db, enterprise_id)
    name = normalize_name(name)
    if name != ent.name:
        ent.name, ent.updated_at = name, now or utcnow()
        db.flush()
    return ent


def _set_status(db, enterprise_id, status: EnterpriseStatus, now) -> Enterprise:
    ent = get(db, enterprise_id)
    if ent.status != status.value:  # idempotent: pa ndryshim → pa updated_at
        ent.status, ent.updated_at = status.value, now or utcnow()
        db.flush()
    return ent


def suspend(
    db: Session, enterprise_id: uuid.UUID | str, *, now: datetime | None = None
) -> Enterprise:
    return _set_status(db, enterprise_id, EnterpriseStatus.SUSPENDED, now)


def activate(
    db: Session, enterprise_id: uuid.UUID | str, *, now: datetime | None = None
) -> Enterprise:
    return _set_status(db, enterprise_id, EnterpriseStatus.ACTIVE, now)
