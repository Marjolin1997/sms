"""M9-g2: prova e pandryshueshme e faturueshmërisë së email-it + outbox i raporteve kumulative drejt Central.

`EmailBillableEvent`: një rresht = "ky email u bë i faturueshëm PËR HERË TË PARË" (hyrja e parë në SENT/DELIVERED/BOUNCED/COMPLAINED),
JO "është i faturueshëm tani". `UNIQUE(email_id)` + `INSERT … ON CONFLICT DO NOTHING` e bëjnë të pamundur një njësi të dytë (riprovime,
callback-e dublikat, gara). Append-only (ORM + trigger PG). `id` monoton është watermark-u; kurrë `updated_at` i email-it.

`BillingUsageReport`: outbox i ngushtë si `sms_usage_reports` (M9-d): përmbajtja e raportit e ngrirë, vetëm fushat e dërgimit ndryshojnë."""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.timeutil import utcnow
from app.models.rates import PK

R_PENDING, R_SENDING, R_SENT, R_RETRY, R_FAILED, R_SUPERSEDED = (
    "pending", "sending", "sent", "retry", "failed", "superseded",
)  # fmt: skip
REPORT_STATUSES = (R_PENDING, R_SENDING, R_SENT, R_RETRY, R_FAILED, R_SUPERSEDED)
BILLABLE_STATUSES = ("sent", "delivered", "bounced", "complained")


class BillingEvidenceImmutableError(RuntimeError):
    pass


class EmailBillableEvent(Base):
    __tablename__ = "sms_email_billable_events"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    email_id: Mapped[int] = mapped_column(ForeignKey("sms_emails.id"), unique=True)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    first_status: Mapped[str] = mapped_column(String(16))
    billable_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint("first_status in ('sent', 'delivered', 'bounced', 'complained')", name="first_status"),
    )  # fmt: skip


class BillingUsageReport(Base):
    __tablename__ = "sms_billing_usage_reports"

    report_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    report_seq: Mapped[int] = mapped_column(BigInteger)
    watermark: Mapped[int] = mapped_column(BigInteger)
    cumulative_billable_count: Mapped[int] = mapped_column(BigInteger)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(12), default=R_PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("enterprise_id", "product_id", "report_seq", name="uq_sms_billing_usage_reports_seq"),
        Index("ix_sms_billing_usage_reports_status_next", "status", "next_attempt_at"),
        CheckConstraint("report_seq > 0", name="seq_positive"),
        CheckConstraint("watermark >= 0 AND cumulative_billable_count >= 0 AND cumulative_billable_count <= watermark", name="counts"),
        CheckConstraint("status in ('" + "', '".join(REPORT_STATUSES) + "')", name="status"),
    )  # fmt: skip


REPORT_FROZEN = ("report_id", "enterprise_id", "product_id", "report_seq", "watermark", "cumulative_billable_count",
                 "generated_at", "payload", "payload_hash", "content_hash", "created_at")  # fmt: skip


@event.listens_for(EmailBillableEvent, "before_update")
@event.listens_for(EmailBillableEvent, "before_delete")
@event.listens_for(BillingUsageReport, "before_delete")
def _append_only(*_) -> None:
    raise BillingEvidenceImmutableError(
        "billable-email evidence and usage reports are never changed or deleted"
    )


@event.listens_for(BillingUsageReport, "before_update")
def _frozen(_m, _c, target) -> None:
    attrs = inspect(target).attrs
    changed = [f for f in REPORT_FROZEN if getattr(attrs, f).history.has_changes()]
    if changed:
        raise BillingEvidenceImmutableError(f"billing usage report content is immutable: {changed}")
