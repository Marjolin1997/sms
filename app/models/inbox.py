"""SMS hyrës (përgjigje nga marrësit), të lidhur me llogarinë që zotëron numrin marrës."""

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.rates import PK
from app.models.wallet import utcnow


class InboundMessage(Base):
    __tablename__ = "sms_inbound_messages"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True)
    owner_ref: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    provider_message_id: Mapped[str | None] = mapped_column(String(128))  # për idempotencë
    to_number: Mapped[str] = mapped_column(String(20))  # numri ynë (pa "+")
    from_number: Mapped[str] = mapped_column(String(20))  # dërguesi (pa "+")
    text: Mapped[str] = mapped_column(Text)
    keyword_action: Mapped[str | None] = mapped_column(String(16))  # opt_out | opt_in
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("provider", "provider_message_id", name="uq_sms_inbound_provider_msg"),
        Index("ix_sms_inbound_thread", "owner_ref", "from_number", "id"),
        Index("ix_sms_inbound_received", "received_at"),
    )
