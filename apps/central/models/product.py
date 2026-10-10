"""Katalogu global i produkteve (Central). Jo specifik për tenant; assignment-i te Enterprise është
entitet i veçantë (`EnterpriseProduct`, M5-b).

`code` dhe `channel` janë të pandryshueshme pas krijimit (assignments varen prej tyre).
Nuk ka fshirje fizike: produkti çaktivizohet (`retired`).
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, Uuid, event, inspect
from sqlalchemy.orm import Mapped, mapped_column

from apps.central.core.db import Base
from apps.central.core.timeutil import utcnow


class Channel(enum.StrEnum):
    """Kanalet e vërteta sot (SMS, email); një kanal i ri kërkon migrim të qëllimshëm."""

    SMS = "sms"
    EMAIL = "email"


class ProductStatus(enum.StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"


class ImmutableError(RuntimeError):
    pass


class Product(Base):
    __tablename__ = "products"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(String(1000))
    channel: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(
        String(16), default=ProductStatus.ACTIVE.value, server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint("code = lower(trim(code)) and length(code) >= 2", name="code_canonical"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        CheckConstraint("channel in ('sms', 'email')", name="channel"),
        CheckConstraint("status in ('active', 'retired')", name="status"),
    )


@event.listens_for(Product, "before_update")
def _immutable_identity(_mapper, _conn, target: Product) -> None:
    state = inspect(target)
    for attr in ("code", "channel"):
        if state.attrs[attr].history.has_changes():
            raise ImmutableError(f"product.{attr} is immutable")


@event.listens_for(Product, "before_delete")
def _no_hard_delete(*_) -> None:
    raise ImmutableError("products are never deleted; set status=retired")
