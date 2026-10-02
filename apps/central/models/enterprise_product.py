"""Assignment-i Enterprise <-> Product (control plane). Vetëm identitet + status.

Nuk është gjendje faturimi (`Subscription`), plan operacional (`AccountPlan`), çmim, wallet, rrugë
provider ose snapshot konfigurimi: ato janë M5-c/M5-d/M9. `enterprise_id` dhe `product_id` janë të
pandryshueshme; nuk ka fshirje fizike; FK `RESTRICT`.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from apps.central.core.db import Base
from apps.central.core.timeutil import utcnow
from apps.central.models.product import ImmutableError
from apps.central.models.sync import guard_revision


class AssignmentStatus(enum.StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class EnterpriseProduct(Base):
    __tablename__ = "enterprise_products"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("enterprises.id", ondelete="RESTRICT")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("products.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(
        String(16), default=AssignmentStatus.ACTIVE.value, server_default="active"
    )
    revision: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "enterprise_id", "product_id", name="uq_enterprise_products_enterprise_id_product_id"
        ),
        CheckConstraint("status in ('active', 'suspended')", name="status"),
        CheckConstraint("revision >= 1", name="revision_positive"),
    )


@event.listens_for(EnterpriseProduct, "before_update")
def _immutable_pair(_mapper, _conn, target: EnterpriseProduct) -> None:
    state = inspect(target)
    for attr in ("enterprise_id", "product_id"):
        if state.attrs[attr].history.has_changes():
            raise ImmutableError(f"enterprise_product.{attr} is immutable")


@event.listens_for(EnterpriseProduct, "before_delete")
def _no_hard_delete(*_) -> None:
    raise ImmutableError("assignments are never deleted; set status=suspended")


guard_revision(EnterpriseProduct, ("status",))
