"""Politika e regjistrimit për produkt (M8-b): a kërkohet nga vetë-regjistrimi dhe si miratohet.

1:1 me Product (PK = product_id). Mungesa e rreshtit = vetë-regjistrimi i çaktivizuar (fail-closed).
Vetëm dy fusha me përdorim real; pa pagesë/SID/shtete/provisioning_mode/çmim (M9/M10). Pa fshirje
fizike: çaktivizohet me `self_registration_enabled=false`; një produkt retired e ruan politikën.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Uuid,
    event,
    false,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from apps.central.core.db import Base
from apps.central.core.timeutil import utcnow
from apps.central.models.product import ImmutableError

MANUAL, AUTOMATIC = "manual", "automatic"
APPROVAL_MODES = (MANUAL, AUTOMATIC)


class ProductRegistrationPolicy(Base):
    __tablename__ = "product_registration_policy"

    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("products.id", ondelete="RESTRICT"), primary_key=True
    )
    self_registration_enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    approval_mode: Mapped[str] = mapped_column(String(16), default=MANUAL, server_default=MANUAL)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint("approval_mode in ('manual', 'automatic')", name="approval_mode"),
    )


@event.listens_for(ProductRegistrationPolicy, "before_delete")
def _no_hard_delete(*_) -> None:
    raise ImmutableError("registration policies are never deleted; disable self-registration")


@event.listens_for(ProductRegistrationPolicy, "before_update")
def _immutable_product(_mapper, _conn, target: ProductRegistrationPolicy) -> None:
    if inspect(target).attrs.product_id.history.has_changes():
        raise ImmutableError("product_registration_policy.product_id is immutable")
