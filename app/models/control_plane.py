"""M7-d: gjendja lokale e aplikuar nga Control Plane (Central). Vetëm skema; logjika te
`app.services.control_plane_sync`. Asnjë `owner_ref` këtu: identiteti është `enterprise_id`.

`sms_entitlements` = një rresht për çdo assignment Enterprise↔Product të Central (SMS dhe email
të pavarur). `enabled` NUK ruhet: llogaritet (`entitlement_enabled`) nga statusi i enterprise-it
dhe i assignment-it, që të mos ketë cache të derivuar që mund të dalë jashtë sinkronit.
`sms_cp_cursor` = rreshti singleton i kursorit të sinkronizimit (pa sekrete, pa çelësa)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.timeutil import utcnow

ENTITLEMENT_ACTIVE = "active"
ENTITLEMENT_SUSPENDED = "suspended"
ENTITLEMENT_WITHDRAWN = "withdrawn"  # vetëm lokale: snapshot-i i autorizuar nuk e përmban më


class Entitlement(Base):
    __tablename__ = "sms_entitlements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sms_enterprises.id"), index=True)
    assignment_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)  # id e Central
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    product_code: Mapped[str] = mapped_column(String(32))
    channel: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("enterprise_id", "product_code"),
        CheckConstraint("status IN ('active','suspended','withdrawn')", name="status"),
    )


class CpCursor(Base):
    __tablename__ = "sms_cp_cursor"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    epoch: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # NULL = s'ka snapshot ende
    authorization_generation: Mapped[int | None] = mapped_column(BigInteger)
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    last_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)


def entitlement_enabled(enterprise_status: str, entitlement_status: str) -> bool:
    """Derivim i vetëm: enterprise aktiv DHE assignment aktiv (kurrë i ruajtur)."""
    return enterprise_status == "active" and entitlement_status == ENTITLEMENT_ACTIVE
