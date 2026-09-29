"""Campaigns SMS: audienca materializohet një herë, pastaj dërgohet me kufij."""

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.rates import PK
from app.models.wallet import MONEY, utcnow


class CampaignStatus(enum.StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    PREPARING = "preparing"  # audienca po materializohet në pjesë
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


ACTIVE = {CampaignStatus.SCHEDULED, CampaignStatus.PREPARING, CampaignStatus.RUNNING}


class Campaign(Base):
    __tablename__ = "sms_campaigns"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_ref: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(80))
    list_id: Mapped[int] = mapped_column(ForeignKey("sms_contact_lists.id"))
    channel: Mapped[str] = mapped_column(String(8), default="sms", server_default="sms")
    category: Mapped[str] = mapped_column(String(16), default="marketing")
    sender: Mapped[str] = mapped_column(String(16))  # SMS: sender ID; email: "email"
    text: Mapped[str | None] = mapped_column(Text)  # SMS ≤ 1600; email plain-text ≤ 100000
    # Vetëm për email:
    subject: Mapped[str | None] = mapped_column(String(200))
    html_body: Mapped[str | None] = mapped_column(Text)
    from_email: Mapped[str | None] = mapped_column(String(254))
    from_name: Mapped[str | None] = mapped_column(String(100))
    template_id: Mapped[int | None] = mapped_column(ForeignKey("sms_templates.id"))
    status: Mapped[CampaignStatus] = mapped_column(
        Enum(CampaignStatus, native_enum=False, length=16), default=CampaignStatus.DRAFT
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_cost: Mapped[Decimal | None] = mapped_column(MONEY)  # kufi i buxhetit; NULL = pa kufi
    rate_per_minute: Mapped[int] = mapped_column(Integer, default=300)
    # Dritare dërgimi (ora lokale = UTC + offset); NULL = çdo orë. Mbështet mesnatën (nis > fund).
    window_start_hour: Mapped[int | None] = mapped_column(Integer)
    window_end_hour: Mapped[int | None] = mapped_column(Integer)
    utc_offset_minutes: Mapped[int] = mapped_column(Integer, default=0)
    prep_cursor: Mapped[int] = mapped_column(BigInteger, default=0)
    pause_reason: Mapped[str | None] = mapped_column(String(32))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("owner_ref", "name", name="uq_sms_campaigns_owner_name"),
        CheckConstraint(
            "(text IS NOT NULL) <> (template_id IS NOT NULL)", name="text_xor_template"
        ),
        CheckConstraint("rate_per_minute BETWEEN 1 AND 10000", name="rate_range"),
        Index("ix_sms_campaigns_status", "status", "id"),
    )


class RecipientStatus(enum.StrEnum):
    PENDING = "pending"
    QUEUED = "queued"  # mesazhi u krijua (statusi i tij ndiqet te sms_messages)
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class CampaignRecipient(Base):
    __tablename__ = "sms_campaign_recipients"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("sms_campaigns.id"))
    contact_id: Mapped[int] = mapped_column(ForeignKey("sms_contacts.id"))
    address: Mapped[str | None] = mapped_column(String(254))  # hiqet kur kontakti fshihet (GDPR)
    status: Mapped[RecipientStatus] = mapped_column(
        Enum(RecipientStatus, native_enum=False, length=16), default=RecipientStatus.PENDING
    )
    reason: Mapped[str | None] = mapped_column(String(48))
    message_id: Mapped[int | None] = mapped_column(ForeignKey("sms_messages.id"))  # SMS
    email_id: Mapped[int | None] = mapped_column(ForeignKey("sms_emails.id"))  # email
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("campaign_id", "contact_id", name="uq_sms_camp_recipient"),
        Index("ix_sms_camp_recipients_status", "campaign_id", "status", "id"),
        Index("ix_sms_camp_recipients_message", "message_id"),
        Index("ix_sms_camp_recipients_email", "email_id"),
        Index("ix_sms_camp_recipients_contact", "contact_id"),
    )
