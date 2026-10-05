"""M9-d: raportet e përdorimit financiar të marra nga Enterprise (immutable, append-only).

Çdo raport ruhet si u mor (payload kanonik + kolona të nxjerra për indekse). Raportet e reja NUK rishkruajnë
të vjetrit: pamja "aktuale" = rreshti me `report_seq` më të madh per (enterprise, product, currency), pa
gjendje të ndryshueshme (një raport i vjetër që vonohet ruhet në histori por s'bëhet kurrë aktual).
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from apps.central.core.db import Base
from apps.central.core.timeutil import utcnow
from apps.central.models.money import MoneyImmutableError

MONEY = Numeric(20, 6)


class UsageReport(Base):
    __tablename__ = "usage_reports"

    report_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("enterprises.id", ondelete="RESTRICT")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("products.id", ondelete="RESTRICT")
    )
    currency: Mapped[str] = mapped_column(String(3))
    report_seq: Mapped[int] = mapped_column(BigInteger)
    authority_mode: Mapped[str] = mapped_column(String(8))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ledger_max_id: Mapped[int] = mapped_column(BigInteger)
    money_cursor_seq: Mapped[int] = mapped_column(BigInteger)
    money_cursor_epoch: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    available: Mapped[Decimal] = mapped_column(MONEY)
    held: Mapped[Decimal] = mapped_column(MONEY)
    gross: Mapped[Decimal] = mapped_column(MONEY)
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(32))

    __table_args__ = (
        UniqueConstraint(
            "enterprise_id", "product_id", "currency", "report_seq", name="uq_usage_reports_seq"
        ),
        Index("ix_usage_reports_enterprise_generated", "enterprise_id", "generated_at"),
        Index(
            "ix_usage_reports_key_ledger",
            "enterprise_id",
            "product_id",
            "currency",
            "ledger_max_id",
        ),
        CheckConstraint("report_seq > 0", name="seq_positive"),
        CheckConstraint("length(currency) = 3 AND currency = upper(currency)", name="currency"),
        CheckConstraint("authority_mode in ('local', 'shadow', 'central')", name="authority_mode"),
        CheckConstraint("abs(gross - available - held) < 0.0000005", name="gross"),
    )


@event.listens_for(UsageReport, "before_update")
@event.listens_for(UsageReport, "before_delete")
def _append_only(*_) -> None:
    raise MoneyImmutableError("usage reports are append-only")
