"""Email: domene dërguese (SPF/DKIM), mesazhe dhe historik statusesh."""

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.rates import PK
from app.models.tenant import TenantOwned
from app.models.wallet import utcnow


class DomainStatus(enum.StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"


class EmailDomain(TenantOwned, Base):
    __tablename__ = "sms_email_domains"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_ref: Mapped[str] = mapped_column(String(64))
    domain: Mapped[str] = mapped_column(String(190))
    dkim_selector: Mapped[str] = mapped_column(String(32))
    dkim_public_key: Mapped[str] = mapped_column(Text)  # base64 DER (SubjectPublicKeyInfo)
    dkim_private_key_enc: Mapped[str] = mapped_column(Text)  # Fernet(PEM); kurrë në tekst të hapur
    status: Mapped[DomainStatus] = mapped_column(
        Enum(DomainStatus, native_enum=False, length=16), default=DomainStatus.PENDING
    )
    # I plotësuar vetëm kur verifikohet; UNIQUE => një domen s'mund të verifikohet nga dy klientë.
    verified_key: Mapped[str | None] = mapped_column(String(190), unique=True)
    spf_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    dkim_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    dmarc_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("owner_ref", "domain", name="uq_sms_email_domains_owner"),)


class EmailStatus(enum.StrEnum):
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"  # pranuar nga provider-i
    DELIVERED = "delivered"
    BOUNCED = "bounced"
    COMPLAINED = "complained"
    FAILED = "failed"


EMAIL_TRANSITIONS = {
    EmailStatus.QUEUED: {EmailStatus.SENDING, EmailStatus.FAILED},
    EmailStatus.SENDING: {EmailStatus.SENT, EmailStatus.QUEUED, EmailStatus.FAILED},
    EmailStatus.SENT: {
        EmailStatus.DELIVERED,
        EmailStatus.BOUNCED,
        EmailStatus.COMPLAINED,
        EmailStatus.FAILED,
    },  # fmt: skip
    EmailStatus.DELIVERED: {EmailStatus.COMPLAINED, EmailStatus.BOUNCED},
    EmailStatus.BOUNCED: set(),
    EmailStatus.COMPLAINED: set(),
    EmailStatus.FAILED: set(),
}


class Email(TenantOwned, Base):
    __tablename__ = "sms_emails"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    owner_ref: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(16), default="transactional")
    domain_id: Mapped[int] = mapped_column(ForeignKey("sms_email_domains.id"))
    from_email: Mapped[str] = mapped_column(String(254))
    from_name: Mapped[str | None] = mapped_column(String(100))
    to_email: Mapped[str] = mapped_column(String(254))
    subject: Mapped[str] = mapped_column(String(200))
    text_body: Mapped[str] = mapped_column(Text)
    html_body: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(32))
    provider_message_id: Mapped[str | None] = mapped_column(String(190))
    status: Mapped[EmailStatus] = mapped_column(
        Enum(EmailStatus, native_enum=False, length=16), default=EmailStatus.QUEUED
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("owner_ref", "idempotency_key", name="uq_sms_emails_idem"),
        Index("ix_sms_emails_queue", "status", "next_attempt_at"),
        Index("ix_sms_emails_provider_msg", "provider", "provider_message_id"),
        Index("ix_sms_emails_owner_created", "owner_ref", "created_at"),
    )


class EmailEvent(Base):
    __tablename__ = "sms_email_events"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("sms_emails.id"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EmailEventImmutableError(RuntimeError):
    pass


@event.listens_for(EmailEvent, "before_update")
@event.listens_for(EmailEvent, "before_delete")
def _email_events_append_only(*_):
    raise EmailEventImmutableError("email events are append-only")
