"""M10-S3: outbox-i i kërkesave të sender-ave Enterprise → Central (`sender.request.v1`). Një rresht = NJË veprim logjik lokal (kërkesë ose ridërgim), i shkruar në të njëjtin transaksion me ndryshimin e `SenderId`.

Identiteti: `operation_id` (UUID i veprimit; UNIQUE) ≠ `id` i rreshtit ≠ `external_ref` i sender-it (`sms-sender-<id lokal>`, i lidhur me CHECK me `sender_id`, pra i pandryshueshëm).
Përmbajtja (payload, hash, identitetet) është e ngrirë (ORM + trigger PG); vetëm fushat e dërgimit ndryshojnë. NJË `requested` për sender (indeks i pjesshëm UNIQUE).
`ack_*` janë vetëm për operim/debug: NUK janë gjendja e sender-it (ajo vjen nga `cp.sender.v1`)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Uuid,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.timeutil import utcnow
from app.models.rates import PK

Q_PENDING, Q_SENDING, Q_SENT, Q_RETRY, Q_FAILED = "pending", "sending", "sent", "retry", "failed"
REQUEST_STATES = (Q_PENDING, Q_SENDING, Q_SENT, Q_RETRY, Q_FAILED)
REQUEST_TYPES = ("requested", "resubmitted")
FROZEN = (
    "operation_id", "enterprise_id", "sender_id", "external_ref", "request_type", "schema_version",
    "payload", "request_hash", "created_at",
)  # fmt: skip


class SenderRequestImmutableError(RuntimeError):
    pass


class SenderRequestOutbox(Base):
    __tablename__ = "sms_sender_request_outbox"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("sms_sender_ids.id"))
    external_ref: Mapped[str] = mapped_column(String(64))
    request_type: Mapped[str] = mapped_column(String(12))
    schema_version: Mapped[str] = mapped_column(String(24))
    payload: Mapped[dict] = mapped_column(JSON)
    request_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(10), default=Q_PENDING, server_default=Q_PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(32))
    ack_outcome: Mapped[str | None] = mapped_column(String(24))
    ack_registry_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_sms_sender_request_outbox_state_next", "state", "next_attempt_at"),
        Index("ix_sms_sender_request_outbox_sender", "sender_id", "id"),
        Index(
            "uq_sms_sender_request_outbox_one_request",
            "sender_id",
            unique=True,
            sqlite_where=text("request_type = 'requested'"),
            postgresql_where=text("request_type = 'requested'"),
        ),
        CheckConstraint("state in ('" + "', '".join(REQUEST_STATES) + "')", name="state"),
        CheckConstraint("request_type in ('requested', 'resubmitted')", name="request_type"),
        CheckConstraint("external_ref = 'sms-sender-' || CAST(sender_id AS VARCHAR)", name="external_ref"),
        CheckConstraint("attempts >= 0", name="attempts"),
    )  # fmt: skip


@event.listens_for(SenderRequestOutbox, "before_delete")
def _no_delete(*_) -> None:
    raise SenderRequestImmutableError("sender request outbox rows are never deleted")


@event.listens_for(SenderRequestOutbox, "before_update")
def _frozen(_m, _c, target) -> None:
    attrs = inspect(target).attrs
    changed = [f for f in FROZEN if getattr(attrs, f).history.has_changes()]
    if changed:
        raise SenderRequestImmutableError(f"sender request content is immutable: {changed}")
