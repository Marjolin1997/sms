"""Assignment-i Enterprise <-> Product (control plane): konkret, pa commit, pa fshirje.

Matrica e statuseve:
- assign i ri: kërkon Enterprise `active` DHE Product `active` (përndryshe Conflict).
- suspend: gjithmonë i lejuar (veprim i sigurt).
- activate (suspended → active): kërkon Enterprise `active` DHE Product `active`.
- statusi i njëjtë → no-op (pa kontroll, pa ndryshim).
- Suspendimi i Enterprise dhe `retired` i Product NUK prekin assignment-et ekzistuese.
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.central.core.errors import Conflict, Invalid, NotFound
from apps.central.core.timeutil import utcnow
from apps.central.models.enterprise import EnterpriseStatus
from apps.central.models.enterprise_product import AssignmentStatus, EnterpriseProduct
from apps.central.models.product import Channel, Product, ProductStatus
from apps.central.services import enterprises as enterprise_svc


def _uuid(value: uuid.UUID | str, field: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError as e:
        raise Invalid(f"invalid {field}") from e


def _status(value) -> AssignmentStatus:
    try:
        return AssignmentStatus(value)
    except ValueError as e:
        raise Invalid("invalid status") from e


def assign_product(
    db: Session,
    enterprise_id: uuid.UUID | str,
    product_id: uuid.UUID | str,
    *,
    now: datetime | None = None,
) -> tuple[EnterpriseProduct, Product]:
    """Krijon assignment `active`. Çifti ekziston (çdo status) → Conflict, pa e ndryshuar."""
    enterprise = enterprise_svc.get(db, enterprise_id)
    product = db.get(Product, _uuid(product_id, "product id"))
    if product is None:
        raise NotFound("product not found")
    if enterprise.status != EnterpriseStatus.ACTIVE.value:
        raise Conflict("enterprise is suspended; new assignments are not allowed")
    if product.status != ProductStatus.ACTIVE.value:
        raise Conflict("product is retired; new assignments are not allowed")
    existing = db.scalar(
        select(EnterpriseProduct).where(
            EnterpriseProduct.enterprise_id == enterprise.id,
            EnterpriseProduct.product_id == product.id,
        )
    )
    if existing is not None:
        raise Conflict(f"assignment already exists (id={existing.id}, status={existing.status})")
    now = now or utcnow()
    row = EnterpriseProduct(
        enterprise_id=enterprise.id, product_id=product.id,
        status=AssignmentStatus.ACTIVE.value, created_at=now, updated_at=now,
    )  # fmt: skip
    db.add(row)
    try:
        db.flush()
    except (
        IntegrityError
    ) as e:  # garë: unique(enterprise_id, product_id) është burimi i së vërtetës
        raise Conflict("assignment already exists") from e
    return row, product


def get_assignment(
    db: Session, enterprise_id: uuid.UUID | str, assignment_id: uuid.UUID | str
) -> tuple[EnterpriseProduct, Product]:
    """Assignment i këtij Enterprise; i një Enterprise tjetër → NotFound."""
    eid = _uuid(enterprise_id, "enterprise id")
    row = db.scalar(
        select(EnterpriseProduct).where(
            EnterpriseProduct.id == _uuid(assignment_id, "assignment id"),
            EnterpriseProduct.enterprise_id == eid,
        )
    )
    if row is None:
        raise NotFound("assignment not found")
    return row, db.get(Product, row.product_id)


def list_enterprise_products(
    db: Session,
    enterprise_id: uuid.UUID | str,
    *,
    status: AssignmentStatus | None = None,
    channel: Channel | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[tuple[EnterpriseProduct, Product]]:
    """Join me produktin (identiteti i produktit nuk denormalizohet te assignment)."""
    enterprise = enterprise_svc.get(db, enterprise_id)
    q = (
        select(EnterpriseProduct, Product)
        .join(Product, Product.id == EnterpriseProduct.product_id)
        .where(EnterpriseProduct.enterprise_id == enterprise.id)
    )
    if status is not None:
        q = q.where(EnterpriseProduct.status == _status(status).value)
    if channel is not None:
        q = q.where(Product.channel == Channel(channel).value)
    q = q.order_by(EnterpriseProduct.created_at, EnterpriseProduct.id)
    q = q.limit(max(1, min(limit, 500))).offset(max(0, offset))
    return [(ep, p) for ep, p in db.execute(q)]


def _set_status(db, enterprise_id, assignment_id, target: AssignmentStatus, now):
    row, product = get_assignment(db, enterprise_id, assignment_id)
    if row.status == target.value:
        return row, product, {}
    if target is AssignmentStatus.ACTIVE:
        enterprise = enterprise_svc.get(db, row.enterprise_id)
        if enterprise.status != EnterpriseStatus.ACTIVE.value:
            raise Conflict("enterprise is suspended; assignment cannot be activated")
        if product.status != ProductStatus.ACTIVE.value:
            raise Conflict("product is retired; assignment cannot be activated")
    before = row.status
    row.status, row.updated_at = target.value, now or utcnow()
    db.flush()
    return row, product, {"before": {"status": before}, "after": {"status": target.value}}


def suspend_assignment(db, enterprise_id, assignment_id, *, now: datetime | None = None):
    """→ (assignment, product, changes); changes == {} = no-op."""
    return _set_status(db, enterprise_id, assignment_id, AssignmentStatus.SUSPENDED, now)


def activate_assignment(db, enterprise_id, assignment_id, *, now: datetime | None = None):
    return _set_status(db, enterprise_id, assignment_id, AssignmentStatus.ACTIVE, now)


def set_status(db, enterprise_id, assignment_id, status, *, now: datetime | None = None):
    target = _status(status)
    return _set_status(db, enterprise_id, assignment_id, target, now)
