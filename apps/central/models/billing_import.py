"""M9-g4: importi i faturimit legacy, gjendja e autoritetit, baseline-i i përdorimit dhe krahasimet shadow (Central).

- `BillingImportBatch`: një artifact i aplikuar (UNIQUE `export_id`; `content_hash` i ngrirë). Rirunimi i të njëjtit artifact = no-op; i njëjti `export_id` me hash tjetër = refuzim.
- `BillingImportItem`: dëshmia e origjinës për ÇDO objekt të importuar (source_system/table/id, `source_hash` (pjesa e ngurtë) + `state_hash` (pjesa që ndryshon), batch, koha). Nuk ndotet modeli financiar.
- `BillingImportIssue`: rresht i bllokuar (conflict/invalid/unsupported/requires_manual_review) për batch; zgjidhje manuale e regjistruar (arsye + aktor), kurrë ndryshim i heshtur.
- `BillingUsageBaseline`: gjendja e hapjes së numëruesit kumulativ të email-it në kufirin e periudhës së parë të faturuar nga Central (e pandryshueshme).
- `BillingAuthorityState`: singleton (local|shadow|central) me ACK dhe aktorin; `BillingShadowComparison`: krahasime të përhershme (append-only), pa efekt autoritar."""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    event,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from apps.central.core.db import Base
from apps.central.core.timeutil import utcnow
from apps.central.models.billing import BillingImmutableError

MODES = ("local", "shadow", "central")
CLASSIFICATIONS = (
    "exact",
    "importable",
    "already_imported",
    "conflict",
    "invalid",
    "unsupported",
    "requires_manual_review",
)
BLOCKING = ("conflict", "invalid", "unsupported", "requires_manual_review")
SHADOW_CATEGORIES = ("exact", "amount_mismatch", "usage_mismatch", "period_mismatch", "plan_mismatch", "pricing_mismatch", "currency_mismatch",
                     "tax_mismatch", "legacy_only", "central_only", "insufficient_usage")  # fmt: skip


class BillingImportBatch(Base):
    __tablename__ = "billing_import_batches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    export_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attestation: Mapped[dict] = mapped_column(
        JSON
    )  # authority.mode + due_unbilled_periods të Enterprise në çastin e eksportit
    summary: Mapped[dict] = mapped_column(
        JSON
    )  # numërues sipas klasifikimit/tabelës, seed-et, baseline-et
    applied_by_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BillingImportItem(Base):
    __tablename__ = "billing_import_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_system: Mapped[str] = mapped_column(String(16))
    source_table: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(64))
    source_hash: Mapped[str] = mapped_column(
        String(64)
    )  # pjesa e ngurtë (nuk ndryshon pas importit)
    state_hash: Mapped[str] = mapped_column(
        String(64)
    )  # pjesa e gjendjes (open→paid|void, plan/status i abonimit)
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("billing_import_batches.id", ondelete="RESTRICT")
    )
    last_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("billing_import_batches.id", ondelete="RESTRICT")
    )
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    detail: Mapped[dict] = mapped_column(
        JSON
    )  # prejardhja e fushave (p.sh. issuer=unknown, voided_at=import_time), pa PII

    __table_args__ = (
        UniqueConstraint(
            "source_system", "source_table", "source_id", name="uq_billing_import_items_source"
        ),
        Index("ix_billing_import_items_target", "target_type", "target_id"),
        CheckConstraint("source_system = 'enterprise'", name="source_system"),
    )


class BillingImportIssue(Base):
    __tablename__ = "billing_import_issues"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("billing_import_batches.id", ondelete="RESTRICT")
    )
    source_table: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(String(64))
    classification: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    resolution: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        UniqueConstraint(
            "batch_id", "source_table", "source_id", name="uq_billing_import_issues_row"
        ),
        Index("ix_billing_import_issues_open", "resolved_at"),
        CheckConstraint(
            "classification in ('conflict', 'invalid', 'unsupported', 'requires_manual_review')",
            name="classification",
        ),
        CheckConstraint(
            "(resolved_at IS NULL AND resolved_by_id IS NULL AND resolution IS NULL) OR "
            "(resolved_at IS NOT NULL AND resolved_by_id IS NOT NULL AND resolution IS NOT NULL AND length(trim(resolution)) > 0)",
            name="resolution_consistency",
        ),
    )


class BillingUsageBaseline(Base):
    __tablename__ = "billing_usage_baselines"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("enterprises.id", ondelete="RESTRICT")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("products.id", ondelete="RESTRICT")
    )
    boundary: Mapped[datetime] = mapped_column(
        DateTime(timezone=True)
    )  # fillimi i periudhës së parë të faturuar nga Central
    cumulative_count: Mapped[int] = mapped_column(BigInteger)
    watermark: Mapped[int] = mapped_column(BigInteger)
    capture_active_since: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("billing_import_batches.id", ondelete="RESTRICT")
    )
    source_hash: Mapped[str] = mapped_column(String(64))
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "enterprise_id", "product_id", "boundary", name="uq_billing_usage_baselines_key"
        ),
        CheckConstraint("cumulative_count >= 0 AND watermark >= cumulative_count", name="counts"),
        CheckConstraint("capture_active_since <= boundary", name="capture_covers_boundary"),
    )


class BillingAuthorityState(Base):
    __tablename__ = "billing_authority_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    mode: Mapped[str] = mapped_column(String(8), default="local", server_default="local")
    ack: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    changed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("mode in ('local', 'shadow', 'central')", name="mode"),
    )


class BillingShadowComparison(Base):
    __tablename__ = "billing_shadow_comparisons"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("billing_subscriptions.id", ondelete="RESTRICT")
    )
    enterprise_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("enterprises.id", ondelete="RESTRICT")
    )
    period_index: Mapped[int] = mapped_column(Integer)
    legacy_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("invoices.id", ondelete="RESTRICT")
    )
    category: Mapped[str] = mapped_column(String(24))
    categories: Mapped[list] = mapped_column(JSON)
    central: Mapped[dict] = mapped_column(JSON)
    legacy: Mapped[dict] = mapped_column(JSON)
    comparison_hash: Mapped[str] = mapped_column(String(64))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_billing_shadow_sub_period", "subscription_id", "period_index", "computed_at"),
        CheckConstraint(
            "category in ('exact', 'amount_mismatch', 'usage_mismatch', 'period_mismatch', 'plan_mismatch', 'pricing_mismatch', "
            "'currency_mismatch', 'tax_mismatch', 'legacy_only', 'central_only', 'insufficient_usage')",
            name="category",
        ),
    )


@event.listens_for(BillingImportBatch, "before_update")
@event.listens_for(BillingUsageBaseline, "before_update")
@event.listens_for(BillingShadowComparison, "before_update")
def _immutable(*_) -> None:
    raise BillingImmutableError(
        "import batches, usage baselines and shadow comparisons are immutable"
    )


@event.listens_for(BillingImportBatch, "before_delete")
@event.listens_for(BillingImportItem, "before_delete")
@event.listens_for(BillingImportIssue, "before_delete")
@event.listens_for(BillingUsageBaseline, "before_delete")
@event.listens_for(BillingAuthorityState, "before_delete")
@event.listens_for(BillingShadowComparison, "before_delete")
def _never_deleted(*_) -> None:
    raise BillingImmutableError(
        "import evidence, authority state and comparisons are never deleted"
    )


@event.listens_for(BillingImportItem, "before_update")
def _item_guard(_m, _c, t) -> None:
    from sqlalchemy import inspect

    attrs = inspect(t).attrs
    changed = [f for f in ("source_system", "source_table", "source_id", "source_hash", "target_type", "target_id", "batch_id", "imported_at")
               if getattr(attrs, f).history.has_changes()]  # fmt: skip
    if changed:
        raise BillingImmutableError(f"import item evidence is immutable: {changed}")


@event.listens_for(BillingImportIssue, "before_update")
def _issue_guard(_m, _c, t) -> None:
    from sqlalchemy import inspect

    attrs = inspect(t).attrs
    changed = [
        f
        for f in ("batch_id", "source_table", "source_id", "classification", "reason", "created_at")
        if getattr(attrs, f).history.has_changes()
    ]
    resolved_before = attrs.resolved_at.history.deleted
    if changed or (resolved_before and resolved_before[0] is not None):
        raise BillingImmutableError(
            "import issues can only be resolved once; evidence fields are immutable"
        )
