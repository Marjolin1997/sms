"""M9-d: outbox i ngushtë dhe i qëndrueshëm për raportet e përdorimit financiar drejt Central.

Çdo rresht = një raport kumulativ i NGRIRË (payload kanonik, `report_id` stabil, `report_seq` monoton lokal per
(enterprise, product, currency)). Dërgimi është at-least-once: Central e bën idempotent sipas `report_id`.
Vetëm fushat e dërgimit (status, attempts, ...) ndryshojnë; përmbajtja e raportit është e pandryshueshme (guard
ORM + trigger PG). Kjo NUK është magjistral e përgjithshme eventesh."""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
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

R_PENDING, R_SENDING, R_SENT, R_RETRY, R_FAILED, R_SUPERSEDED = (
    "pending", "sending", "sent", "retry", "failed", "superseded",
)  # fmt: skip
REPORT_STATUSES = (R_PENDING, R_SENDING, R_SENT, R_RETRY, R_FAILED, R_SUPERSEDED)


class UsageReport(Base):
    __tablename__ = "sms_usage_reports"

    report_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    currency: Mapped[str] = mapped_column(String(3))
    report_seq: Mapped[int] = mapped_column(BigInteger)
    authority_mode: Mapped[str] = mapped_column(String(8))
    ledger_max_id: Mapped[int] = mapped_column(BigInteger)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    # --- dërgimi (i vetmi pjesë e ndryshueshme) ---
    status: Mapped[str] = mapped_column(String(12), default=R_PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("enterprise_id", "product_id", "currency", "report_seq", name="uq_sms_usage_reports_seq"),
        Index("ix_sms_usage_reports_status_next", "status", "next_attempt_at"),
        CheckConstraint("report_seq > 0", name="seq_positive"),
        CheckConstraint(
            "status in ('" + "', '".join(REPORT_STATUSES) + "')", name="status"
        ),
        CheckConstraint("authority_mode in ('local', 'shadow', 'central')", name="authority_mode"),
    )  # fmt: skip


REPORT_FROZEN = ("report_id", "enterprise_id", "product_id", "currency", "report_seq", "authority_mode",
                 "ledger_max_id", "generated_at", "payload", "payload_hash", "content_hash", "created_at")  # fmt: skip


class UsageReportImmutableError(Exception):
    pass


@event.listens_for(UsageReport, "before_delete")
def _no_delete(*_) -> None:
    raise UsageReportImmutableError("usage reports are never deleted")


@event.listens_for(UsageReport, "before_update")
def _frozen(_m, _c, target) -> None:
    attrs = inspect(target).attrs
    changed = [f for f in REPORT_FROZEN if getattr(attrs, f).history.has_changes()]
    if changed:
        raise UsageReportImmutableError(f"usage report content is immutable: {changed}")
