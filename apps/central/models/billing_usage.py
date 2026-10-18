"""M9-g2: raportet kumulative të email-eve të faturueshme të marra nga Enterprise (`cp.billing.usage.v1`). Immutable, append-only.

Çdo raport ruhet si u mor. `cumulative_billable_count` është e vetmja sasi që faturimi përdor (delta = cumulative_to − cumulative_from);
`watermark` është informativ dhe monoton. Raportet NUK fshihen kurrë (periudhat e faturimit i referojnë me FK RESTRICT: prova e deltës).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from apps.central.core.db import Base
from apps.central.core.timeutil import utcnow
from apps.central.models.billing import BillingImmutableError


class BillingUsageReport(Base):
    __tablename__ = "billing_usage_reports"

    report_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("enterprises.id", ondelete="RESTRICT")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("products.id", ondelete="RESTRICT")
    )
    report_seq: Mapped[int] = mapped_column(BigInteger)
    watermark: Mapped[int] = mapped_column(BigInteger)
    cumulative_billable_count: Mapped[int] = mapped_column(BigInteger)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    payload: Mapped[dict] = mapped_column(JSON)
    payload_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(32))

    __table_args__ = (
        UniqueConstraint(
            "enterprise_id", "product_id", "report_seq", name="uq_billing_usage_reports_seq"
        ),
        Index("ix_billing_usage_reports_generated", "enterprise_id", "product_id", "generated_at"),
        CheckConstraint("report_seq > 0", name="seq_positive"),
        CheckConstraint(
            "watermark >= 0 AND cumulative_billable_count >= 0 AND cumulative_billable_count <= watermark",
            name="counts",
        ),
    )


@event.listens_for(BillingUsageReport, "before_update")
@event.listens_for(BillingUsageReport, "before_delete")
def _append_only(*_) -> None:
    raise BillingImmutableError("billing usage reports are append-only")
