"""Event log (pull + push) dhe webhook-et e klientëve."""

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.rates import PK
from app.models.wallet import utcnow


class Event(Base):
    """Fakt i ndodhur për një klient. Data përmban vetëm id dhe statuse, jo PII
    (përjashtim: consent.* mban adresën, që klienti të sinkronizojë CRM-në). Ruhet
    `event_retention_days`, pastaj pastrohet."""

    __tablename__ = "sms_events"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_ref: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(48))
    resource_type: Mapped[str] = mapped_column(String(24))
    resource_id: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_sms_events_owner_id", "owner_ref", "id"),
        Index("ix_sms_events_created", "created_at"),
    )


class EndpointStatus(enum.StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class WebhookEndpoint(Base):
    __tablename__ = "sms_webhook_endpoints"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_ref: Mapped[str] = mapped_column(String(64), index=True)
    url: Mapped[str] = mapped_column(String(2000))
    secret_enc: Mapped[str] = mapped_column(Text)  # Fernet; shfaqet vetëm një herë
    event_types: Mapped[list] = mapped_column(JSON)  # ["*"], ["message.*"], ["email.bounced"]
    status: Mapped[EndpointStatus] = mapped_column(
        Enum(EndpointStatus, native_enum=False, length=16), default=EndpointStatus.ACTIVE
    )
    disabled_reason: Mapped[str | None] = mapped_column(String(64))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeliveryStatus(enum.StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # retry-t u shterën


class WebhookDelivery(Base):
    __tablename__ = "sms_webhook_deliveries"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    endpoint_id: Mapped[int] = mapped_column(ForeignKey("sms_webhook_endpoints.id"))
    event_id: Mapped[int] = mapped_column(ForeignKey("sms_events.id"))
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, native_enum=False, length=16), default=DeliveryStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(120))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_sms_wh_deliveries_queue", "status", "next_attempt_at"),
        Index("ix_sms_wh_deliveries_endpoint", "endpoint_id", "id"),
        Index("ix_sms_wh_deliveries_event", "event_id"),
    )
