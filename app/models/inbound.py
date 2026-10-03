"""SMS hyrës (MO): inbox i klientit dhe fjalët kyçe me përgjigje automatike."""

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.timeutil import utcnow
from app.models.rates import PK
from app.models.tenant import TenantOwned


class InboundMessage(TenantOwned, Base):
    """Mesazh i marrë në një numër të klientit. `text` dhe `from_number` janë PII: fshihen
    (zëvendësohen) kur kontakti fshihet sipas GDPR."""

    __tablename__ = "sms_inbound_messages"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    owner_ref: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    provider_message_id: Mapped[str | None] = mapped_column(String(128))
    from_number: Mapped[str] = mapped_column(String(20))
    to_number: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    action: Mapped[str | None] = mapped_column(String(16))  # opt_out | opt_in
    keyword: Mapped[str | None] = mapped_column(String(32))  # fjala kyçe e klientit që u përputh
    reply_status: Mapped[str | None] = mapped_column(String(64))  # queued | failed:<kodi>
    reply_message_id: Mapped[str | None] = mapped_column(String(36))
    contact_id: Mapped[int | None] = mapped_column(PK)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("provider", "provider_message_id", name="uq_sms_inbound_provider_msg"),
        Index("ix_sms_inbound_owner", "owner_ref", "id"),
        Index("ix_sms_inbound_from", "owner_ref", "from_number"),
    )


class Keyword(TenantOwned, Base):
    """Fjalë kyçe e klientit (fjala e parë e mesazhit hyrës), me përgjigje automatike opsionale."""

    __tablename__ = "sms_keywords"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_ref: Mapped[str] = mapped_column(String(64))
    keyword: Mapped[str] = mapped_column(String(32))  # e vogël, vetëm shkronja/shifra
    reply_text: Mapped[str | None] = mapped_column(String(480))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("owner_ref", "keyword", name="uq_sms_keywords_owner_kw"),)
