import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from apps.central.api.deps import get_db, require_role
from apps.central.models.enterprise_product import AssignmentStatus, EnterpriseProduct
from apps.central.models.product import Channel, Product
from apps.central.models.user import CentralUser, Role
from apps.central.services import audit
from apps.central.services import enterprise_products as svc

router = APIRouter(prefix="/admin/enterprises/{enterprise_id}/products")
READ = require_role(Role.ADMIN, Role.OPERATOR)
WRITE = require_role(Role.ADMIN)
RESOURCE = "enterprise_product"


class AssignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: uuid.UUID


class AssignmentPatch(BaseModel):
    """Vetëm `status`; `enterprise_id`/`product_id` (të pandryshueshme) → 422."""

    model_config = ConfigDict(extra="forbid")
    status: AssignmentStatus


def out(ep: EnterpriseProduct, p: Product) -> dict:
    return {
        "id": str(ep.id), "enterprise_id": str(ep.enterprise_id), "product_id": str(ep.product_id),
        "product_code": p.code, "product_name": p.name, "product_channel": p.channel,
        "product_status": p.status, "status": ep.status,
        "created_at": ep.created_at, "updated_at": ep.updated_at,
    }  # fmt: skip


@router.get("")
def list_assignments(
    enterprise_id: uuid.UUID,
    status: AssignmentStatus | None = None,
    channel: Channel | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: CentralUser = Depends(READ),
):
    rows = svc.list_enterprise_products(
        db, enterprise_id, status=status, channel=channel, limit=limit, offset=offset
    )
    return [out(ep, p) for ep, p in rows]


@router.post("", status_code=201)
def assign(
    enterprise_id: uuid.UUID,
    body: AssignIn,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    ep, p = svc.assign_product(db, enterprise_id, body.product_id)
    detail = {"after": {"enterprise_id": str(ep.enterprise_id), "product_id": str(ep.product_id),
                        "status": ep.status}}  # fmt: skip
    audit.record(db, actor, "enterprise_product.assign", RESOURCE, ep.id, detail)
    db.commit()
    return out(ep, p)


@router.get("/{assignment_id}")
def get_assignment(
    enterprise_id: uuid.UUID,
    assignment_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: CentralUser = Depends(READ),
):
    return out(*svc.get_assignment(db, enterprise_id, assignment_id))


@router.patch("/{assignment_id}")
def update_assignment(
    enterprise_id: uuid.UUID,
    assignment_id: uuid.UUID,
    body: AssignmentPatch,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    ep, p, changes = svc.set_status(db, enterprise_id, assignment_id, body.status)
    if changes:
        detail = {
            "enterprise_id": str(ep.enterprise_id),
            "product_id": str(ep.product_id),
            **changes,
        }
        audit.record(db, actor, "enterprise_product.update", RESOURCE, ep.id, detail)
    db.commit()
    return out(ep, p)
