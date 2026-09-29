"""Pipeline i dërgimit: plani i llogarisë, routes, mesazhet dhe historiku i statuseve."""

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.rates import PK
from app.models.wallet import MONEY, utcnow


class AccountPlan(Base):
    """Lidh një klient me rate card-in e tij; `enabled` është kill switch për llogarinë."""

    __tablename__ = "sms_account_plans"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_ref: Mapped[str] = mapped_column(String(64), unique=True)
    rate_card_id: Mapped[int] = mapped_column(ForeignKey("sms_rate_cards.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Route(Base):
    __tablename__ = "sms_routes"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    prefix: Mapped[str] = mapped_column(String(16))
    country: Mapped[str] = mapped_column(String(2))
    provider: Mapped[str] = mapped_column(String(32))
    priority: Mapped[int] = mapped_column(Integer, default=100)  # më e madhe = më e preferuar
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (UniqueConstraint("prefix", "provider"),)


class MessageStatus(enum.StrEnum):
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"  # pranuar nga provider-i, pret DLR
    DELIVERED = "delivered"
    FAILED = "failed"


TERMINAL = {MessageStatus.DELIVERED, MessageStatus.FAILED}
TRANSITIONS = {
    MessageStatus.QUEUED: {MessageStatus.SENDING, MessageStatus.FAILED},
    MessageStatus.SENDING: {MessageStatus.SENT, MessageStatus.QUEUED, MessageStatus.FAILED},
    MessageStatus.SENT: {MessageStatus.DELIVERED, MessageStatus.FAILED},
    MessageStatus.DELIVERED: set(),
    MessageStatus.FAILED: set(),
}


class Message(Base):
    __tablename__ = "sms_messages"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    owner_ref: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    wallet_id: Mapped[int] = mapped_column(ForeignKey("sms_wallets.id"))
    hold_id: Mapped[int] = mapped_column(ForeignKey("sms_holds.id"))
    sender: Mapped[str] = mapped_column(String(16))
    destination: Mapped[str] = mapped_column(String(16))
    country: Mapped[str] = mapped_column(String(2))
    text: Mapped[str] = mapped_column(Text)
    template_version_id: Mapped[int | None] = mapped_column(ForeignKey("sms_template_versions.id"))
    encoding: Mapped[str] = mapped_column(String(8))
    segments: Mapped[int] = mapped_column(Integer)
    # Çmimi ngrihet këtu në momentin e pranimit; tarifat e reja nuk e prekin.
    currency: Mapped[str] = mapped_column(String(3))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    total_price: Mapped[Decimal] = mapped_column(MONEY)
    rate_version_id: Mapped[int] = mapped_column(ForeignKey("sms_rate_card_versions.id"))
    rate_id: Mapped[int] = mapped_column(ForeignKey("sms_rates.id"))
    provider: Mapped[str] = mapped_column(String(32))
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[MessageStatus] = mapped_column(
        Enum(MessageStatus, native_enum=False, length=16), default=MessageStatus.QUEUED
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("owner_ref", "idempotency_key"),
        Index("ix_sms_messages_queue", "status", "next_attempt_at"),
        Index("ix_sms_messages_provider_msg", "provider", "provider_message_id"),
    )


class MessageEvent(Base):
    """Historik i pandryshueshëm i çdo ndryshimi statusi."""

    __tablename__ = "sms_message_events"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("sms_messages.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EventImmutableError(RuntimeError):
    pass


@event.listens_for(MessageEvent, "before_update")
@event.listens_for(MessageEvent, "before_delete")
def _events_append_only(*_):
    raise EventImmutableError("message events are append-only")


class DlrReceipt(Base):
    """Çdo DLR i pranuar, edhe i refuzuari (p.sh. 'delivered' pas dlr_timeout), për audit."""

    __tablename__ = "sms_dlr_receipts"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32))
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str | None] = mapped_column(String(16))
    outcome: Mapped[str] = mapped_column(String(24))  # applied | unknown_message | conflict
    raw_body: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_sms_dlr_receipts_pmid", "provider", "provider_message_id"),)
