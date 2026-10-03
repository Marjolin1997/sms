import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from apps.central.api.deps import get_db, require_role
from apps.central.models.product import Channel, Product, ProductStatus
from apps.central.models.user import CentralUser, Role
from apps.central.services import audit, products

router = APIRouter(prefix="/admin/products")
READ = require_role(Role.ADMIN, Role.OPERATOR)
WRITE = require_role(Role.ADMIN)


class ProductIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(max_length=64)
    name: str = Field(max_length=200)
    channel: Channel
    description: str | None = Field(default=None, max_length=2000)
    status: ProductStatus = ProductStatus.ACTIVE


class ProductPatch(BaseModel):
    """`code` dhe `channel` nuk pranohen (të pandryshueshme): fushë e panjohur → 422."""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    status: ProductStatus | None = None


def out(p: Product) -> dict:
    return {
        "id": str(p.id), "code": p.code, "name": p.name, "description": p.description,
        "channel": p.channel, "status": p.status,
        "created_at": p.created_at, "updated_at": p.updated_at,
    }  # fmt: skip


@router.get("")
def list_products(
    status: ProductStatus | None = None,
    channel: Channel | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: CentralUser = Depends(READ),
):
    return [out(p) for p in products.list_products(db, status=status, channel=channel,
                                                   limit=limit, offset=offset)]  # fmt: skip


@router.post("", status_code=201)
def create_product(
    body: ProductIn, db: Session = Depends(get_db), actor: CentralUser = Depends(WRITE)
):
    p = products.create(db, body.code, body.name, body.channel, body.description, body.status)
    after = {"code": p.code, "channel": p.channel, **products.snapshot(p)}
    audit.record(db, actor, "product.create", "product", p.id, {"after": after})
    db.commit()
    return out(p)


@router.get("/{product_id}")
def get_product(
    product_id: uuid.UUID, db: Session = Depends(get_db), _: CentralUser = Depends(READ)
):
    return out(products.get(db, product_id))


@router.patch("/{product_id}")
def update_product(
    product_id: uuid.UUID,
    body: ProductPatch,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    fields = {k: getattr(body, k) for k in body.model_fields_set}
    if "name" in fields and fields["name"] is None:
        fields["name"] = ""  # null nuk pastron emrin: refuzohet nga validimi (422)
    p, changes = products.update(db, product_id, **fields)
    if changes:
        audit.record(db, actor, "product.update", "product", p.id, changes)
    db.commit()
    return out(p)
