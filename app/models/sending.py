"""Pipeline i dërgimit: plani i llogarisë, routes, mesazhet dhe historiku i statuseve."""

import enum
import uuid
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
    Uuid,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.timeutil import utcnow
from app.models.rates import PK
from app.models.tenant import TenantOwned
from app.models.wallet import MONEY


class AccountPlan(TenantOwned, Base):
    """Lidh një klient me rate card-in e tij; `enabled` është kill switch për llogarinë."""

    __tablename__ = "sms_account_plans"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_ref: Mapped[str] = mapped_column(String(64), unique=True)
    rate_card_id: Mapped[int] = mapped_column(ForeignKey("sms_rate_cards.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Mesazhe të pranuara për minutë; NULL = kufiri i paracaktuar (DEFAULT_RATE_LIMIT).
    rate_limit_per_min: Mapped[int | None] = mapped_column(Integer)
    email_rate_limit_per_min: Mapped[int | None] = mapped_column(Integer)  # NULL = 600


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
    # M9-a: rezultati i dërgimit është i PANJOHUR dhe i pasigurt për retry (jo sukses i provider-it,
    # jo dështim). Terminal për automatizim: asnjë ridërgim, release apo capture automatik; hold-i
    # mbetet i pandryshuar. Dalja: DLR autoritativ (delivered/failed) ose zgjidhje e stafit.
    UNKNOWN = "unknown"
    DELIVERED = "delivered"
    FAILED = "failed"


TERMINAL = {MessageStatus.DELIVERED, MessageStatus.FAILED}
TRANSITIONS = {
    MessageStatus.QUEUED: {MessageStatus.SENDING, MessageStatus.FAILED},
    MessageStatus.SENDING: {
        MessageStatus.SENT,
        MessageStatus.QUEUED,
        MessageStatus.FAILED,
        MessageStatus.UNKNOWN,
    },
    MessageStatus.SENT: {MessageStatus.DELIVERED, MessageStatus.FAILED},
    MessageStatus.UNKNOWN: {MessageStatus.DELIVERED, MessageStatus.FAILED},
    MessageStatus.DELIVERED: set(),
    MessageStatus.FAILED: set(),
}


class Message(TenantOwned, Base):
    __tablename__ = "sms_messages"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    owner_ref: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    wallet_id: Mapped[int] = mapped_column(ForeignKey("sms_wallets.id"))
    hold_id: Mapped[int] = mapped_column(ForeignKey("sms_holds.id"))
    category: Mapped[str] = mapped_column(
        String(16), default="transactional", server_default="transactional"
    )
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
    # Referencat ligjëruese (NULL kur çmimi vjen nga snapshot-i Central: price_source='central').
    rate_version_id: Mapped[int | None] = mapped_column(ForeignKey("sms_rate_card_versions.id"))
    rate_id: Mapped[int | None] = mapped_column(ForeignKey("sms_rates.id"))
    # M9-e: burimi i çmimit dhe identitetet e snapshot-it Central (të ngrira me mesazhin; nuk kërkohet kërkim i ri për ta shpjeguar).
    price_source: Mapped[str | None] = mapped_column(
        String(8)
    )  # legacy | central (NULL = para M9-e = legacy)
    pricing_book_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    pricing_version_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    pricing_rule_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    provider: Mapped[str] = mapped_column(String(32))
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[MessageStatus] = mapped_column(
        Enum(MessageStatus, native_enum=False, length=16), default=MessageStatus.QUEUED
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # M9-a: vendoset (COMMIT i veçantë) PARA thirrjes së provider-it; NULL pas një SENDING të
    # ngecur = provider-i definitivisht NUK u thirr (i sigurt për riradhitje). Pastrohet te QUEUED.
    dispatch_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("owner_ref", "idempotency_key"),
        Index("ix_sms_messages_queue", "status", "next_attempt_at"),
        Index("ix_sms_messages_provider_msg", "provider", "provider_message_id"),
        Index("ix_sms_messages_owner_created", "owner_ref", "created_at"),
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


MESSAGE_PRICE_FROZEN = ("currency", "unit_price", "total_price", "segments", "encoding", "rate_version_id", "rate_id",
                        "price_source", "pricing_book_ref", "pricing_version_ref", "pricing_rule_ref")  # fmt: skip


class MessagePriceFrozenError(RuntimeError):
    pass


@event.listens_for(Message, "before_update")
def _message_price_frozen(_m, _c, target) -> None:
    """M9-e: snapshot-i i çmimit të mesazhit s'ndryshon kurrë (rezervimi/capture/DLR përdorin vlerën e ngrirë)."""
    from sqlalchemy import inspect

    attrs = inspect(target).attrs
    changed = [f for f in MESSAGE_PRICE_FROZEN if getattr(attrs, f).history.has_changes()]
    if changed:
        raise MessagePriceFrozenError(f"message price snapshot is immutable: {changed}")
