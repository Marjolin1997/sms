"""M9-g1: faturimi periodik në Central — plane tregtare të versionuara, abonime, periudha faturimi, profile, fatura.

Parime (të miratuara):
- `PlanVersion`: draft → active → retired; fushat financiare (monedhë, `monthly_fee`, `included_emails`) ngrihen me aktivizimin.
  Korrigjim = version i ri. Çmimi i email overage NUK është këtu (M9-e: çmim Central për produktin email).
- `BillingSubscription`: një per enterprise. Faturim në ARREARS, pa proporcion: periudha k = [add_months(anchor, k − base),
  add_months(anchor, k − base + 1)); `next_period_index` përparon vetëm brenda transaksionit të faturimit.
- `BillingPeriod`: prova e qëndrueshme që çdo periudhë e mbyllur u përpunua saktësisht një herë (`invoiced` | `no_charge`).
- `Invoice`/`InvoiceLine`: të pandryshueshme; `subtotal = Σ lines.amount`, `tax = cents(subtotal × vat_rate)`, `total = subtotal + tax`
  (HALF_UP). Bill-to, issuer, VAT dhe versioni i planit ngrihen në lëshim.
- Numërimi: `InvoiceNumberSequence` me rresht të kyçur (`FOR UPDATE`), pa boshllëqe.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from apps.central.core.db import Base
from apps.central.core.timeutil import utcnow
from apps.central.models.product import ImmutableError

MONEY = Numeric(20, 6)
V_DRAFT, V_ACTIVE, V_RETIRED = "draft", "active", "retired"
SUB_ACTIVE, SUB_CANCELLED = "active", "cancelled"
INV_OPEN, INV_PAID, INV_VOID = "open", "paid", "void"
P_INVOICED, P_NO_CHARGE = "invoiced", "no_charge"
L_MONTHLY_FEE, L_EMAIL_OVERAGE, L_ADJUSTMENT = "monthly_fee", "email_overage", "adjustment"
L_LEGACY = "legacy"  # M9-g4: linjë e importuar që s'mapohet në mënyrë eksplicite (shuma ruhet, kuptimi mbetet legacy)
LINE_TYPES = (L_MONTHLY_FEE, L_EMAIL_OVERAGE, L_ADJUSTMENT, L_LEGACY)
PROV_CENTRAL, PROV_LEGACY = "central", "legacy_import"


class BillingImmutableError(ImmutableError):
    pass


class CommercialPlan(Base):
    __tablename__ = "commercial_plans"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlanVersion(Base):
    __tablename__ = "plan_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("commercial_plans.id", ondelete="RESTRICT")
    )
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(8), default=V_DRAFT)
    currency: Mapped[str] = mapped_column(String(3))
    monthly_fee: Mapped[Decimal] = mapped_column(MONEY)
    included_emails: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    activated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retire_reason: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        UniqueConstraint("plan_id", "version", name="uq_plan_versions_plan_version"),
        Index(
            "uq_plan_versions_one_draft",
            "plan_id",
            unique=True,
            postgresql_where=text("status = 'draft'"),
            sqlite_where=text("status = 'draft'"),
        ),  # fmt: skip
        CheckConstraint("status in ('draft', 'active', 'retired')", name="status"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("monthly_fee >= 0", name="fee_non_negative"),
        CheckConstraint("included_emails >= 0", name="included_non_negative"),
        CheckConstraint("length(currency) = 3 AND currency = upper(currency)", name="currency"),
        CheckConstraint(
            "(status = 'draft' AND content_hash IS NULL AND activated_at IS NULL) OR "
            "(status <> 'draft' AND content_hash IS NOT NULL AND activated_at IS NOT NULL)",
            name="status_consistency",
        ),
        CheckConstraint(
            "(status = 'retired' AND retired_at IS NOT NULL AND retire_reason IS NOT NULL) OR "
            "(status <> 'retired' AND retired_at IS NULL AND retire_reason IS NULL)",
            name="retire_consistency",
        ),
    )


class BillingProfile(Base):
    __tablename__ = "billing_profiles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("enterprises.id", ondelete="RESTRICT"), unique=True
    )
    legal_name: Mapped[str] = mapped_column(String(120))
    address: Mapped[str] = mapped_column(String(300))
    country: Mapped[str] = mapped_column(String(2))
    tax_id: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str] = mapped_column(String(254))
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal(0))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint("vat_rate >= 0 AND vat_rate <= 1", name="vat_range"),
        CheckConstraint("length(country) = 2", name="country"),
    )


class BillingSubscription(Base):
    __tablename__ = "billing_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("enterprises.id", ondelete="RESTRICT"), unique=True
    )
    plan_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("plan_versions.id", ondelete="RESTRICT")
    )
    pending_plan_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("plan_versions.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(10), default=SUB_ACTIVE)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    anchor_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # indeksi i periudhës së parë të ankorës aktuale (rifillimi pas anulimit e vazhdon numërimin, s'e rinis)
    anchor_period_index: Mapped[int] = mapped_column(Integer, default=0)
    next_period_index: Mapped[int] = mapped_column(Integer, default=0)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint("status in ('active', 'cancelled')", name="status"),
        CheckConstraint(
            "anchor_period_index >= 0 AND next_period_index >= anchor_period_index", name="indexes"
        ),
        CheckConstraint(
            "(status = 'cancelled' AND cancelled_at IS NOT NULL) OR "
            "(status = 'active' AND cancelled_at IS NULL)",
            name="cancel_consistency",
        ),
    )


class InvoiceNumberSequence(Base):
    __tablename__ = "invoice_number_sequence"

    year: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    last_number: Mapped[int] = mapped_column(BigInteger, default=0)

    __table_args__ = (CheckConstraint("last_number >= 0", name="non_negative"),)


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    number: Mapped[str] = mapped_column(String(24), unique=True)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("enterprises.id", ondelete="RESTRICT")
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("billing_subscriptions.id", ondelete="RESTRICT")
    )
    period_index: Mapped[int] = mapped_column(Integer)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # M9-g4: faturat legacy s'kanë referencë historike të versionit të planit ⇒ NULL (kurrë e shpikur); `provenance` e ndan nga faturat e lëshuara nga Central
    plan_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("plan_versions.id", ondelete="RESTRICT")
    )
    provenance: Mapped[str] = mapped_column(
        String(16), default=PROV_CENTRAL, server_default=PROV_CENTRAL
    )
    currency: Mapped[str] = mapped_column(String(3))
    subtotal: Mapped[Decimal] = mapped_column(MONEY)
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    tax: Mapped[Decimal] = mapped_column(MONEY)
    total: Mapped[Decimal] = mapped_column(MONEY)
    status: Mapped[str] = mapped_column(String(8), default=INV_OPEN)
    bill_to: Mapped[dict] = mapped_column(JSON)
    issuer: Mapped[dict] = mapped_column(JSON)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # rezervuar g3
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    voided_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("id", "currency", name="uq_invoices_id_currency"),
        UniqueConstraint(
            "id", "enterprise_id", "currency", name="uq_invoices_id_enterprise_currency"
        ),
        UniqueConstraint("subscription_id", "period_index", name="uq_invoices_sub_period_index"),
        UniqueConstraint("subscription_id", "period_start", name="uq_invoices_sub_period_start"),
        Index("ix_invoices_enterprise", "enterprise_id", "issued_at"),
        Index("ix_invoices_status_due", "status", "due_at"),
        CheckConstraint("status in ('open', 'paid', 'void')", name="status"),
        CheckConstraint("subtotal >= 0 AND tax >= 0 AND total = subtotal + tax", name="arithmetic"),
        CheckConstraint("vat_rate >= 0 AND vat_rate <= 1", name="vat_range"),
        CheckConstraint("length(currency) = 3 AND currency = upper(currency)", name="currency"),
        CheckConstraint("period_end > period_start", name="period_order"),
        CheckConstraint("provenance in ('central', 'legacy_import')", name="provenance"),
        CheckConstraint(
            "provenance = 'legacy_import' OR plan_version_id IS NOT NULL",
            name="plan_version_required",
        ),
        CheckConstraint(
            "(status = 'open' AND paid_at IS NULL AND voided_at IS NULL AND voided_reason IS NULL) OR "
            "(status = 'paid' AND paid_at IS NOT NULL AND voided_at IS NULL AND voided_reason IS NULL) OR "
            "(status = 'void' AND paid_at IS NULL AND voided_at IS NOT NULL AND voided_reason IS NOT NULL "
            "AND length(trim(voided_reason)) > 0)",
            name="status_consistency",
        ),
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    currency: Mapped[str] = mapped_column(String(3))
    line_no: Mapped[int] = mapped_column(Integer)
    line_type: Mapped[str] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    plan_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("plan_versions.id", ondelete="RESTRICT")
    )
    pricing_source: Mapped[str | None] = mapped_column(String(16))
    price_book_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("price_books.id", ondelete="RESTRICT")
    )
    price_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("price_versions.id", ondelete="RESTRICT")
    )
    price_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("price_rules.id", ondelete="RESTRICT")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["invoice_id", "currency"], ["invoices.id", "invoices.currency"],
            name="fk_invoice_lines_invoice_currency", ondelete="RESTRICT",
        ),
        UniqueConstraint("invoice_id", "line_no", name="uq_invoice_lines_no"),
        Index("ix_invoice_lines_invoice", "invoice_id"),
        CheckConstraint(
            "line_type in ('monthly_fee', 'email_overage', 'adjustment', 'legacy')", name="line_type"
        ),
        CheckConstraint("quantity > 0 AND unit_price >= 0 AND amount >= 0", name="positive"),
        CheckConstraint("period_end > period_start", name="period_order"),
    )  # fmt: skip


class BillingPeriod(Base):
    __tablename__ = "billing_periods"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("billing_subscriptions.id", ondelete="RESTRICT")
    )
    enterprise_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("enterprises.id", ondelete="RESTRICT")
    )
    period_index: Mapped[int] = mapped_column(Integer)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    plan_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("plan_versions.id", ondelete="RESTRICT")
    )
    provenance: Mapped[str] = mapped_column(
        String(16), default=PROV_CENTRAL, server_default=PROV_CENTRAL
    )
    status: Mapped[str] = mapped_column(String(10))
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("invoices.id", ondelete="RESTRICT")
    )
    billed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # M9-g2: numëruesit kumulativë (baseline → prerje) dhe raportet që i provojnë; NULL kur periudha s'ka matje email
    usage_from: Mapped[int | None] = mapped_column(BigInteger)
    usage_to: Mapped[int | None] = mapped_column(BigInteger)
    usage_from_report_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("billing_usage_reports.report_id", ondelete="RESTRICT")
    )
    usage_to_report_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("billing_usage_reports.report_id", ondelete="RESTRICT")
    )
    # M9-g4: baseline i hapjes (periudha e parë pas importit pa raport para kufirit): provon `usage_from` kur s'ka raport
    usage_from_baseline_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("billing_usage_baselines.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("subscription_id", "period_index", name="uq_billing_periods_index"),
        UniqueConstraint("subscription_id", "period_start", name="uq_billing_periods_start"),
        UniqueConstraint("invoice_id", name="uq_billing_periods_invoice"),
        CheckConstraint("status in ('invoiced', 'no_charge')", name="status"),
        CheckConstraint("period_end > period_start", name="period_order"),
        CheckConstraint("provenance in ('central', 'legacy_import')", name="provenance"),
        CheckConstraint(
            "provenance = 'legacy_import' OR plan_version_id IS NOT NULL",
            name="plan_version_required",
        ),
        CheckConstraint(
            "usage_from IS NULL OR (usage_to IS NOT NULL AND usage_to >= usage_from AND usage_from >= 0)",
            name="usage_order",
        ),
        CheckConstraint(
            "(status = 'invoiced' AND invoice_id IS NOT NULL) OR "
            "(status = 'no_charge' AND invoice_id IS NULL)",
            name="invoice_consistency",
        ),
    )


# --- mbrojtjet ORM (shtresa e dytë; e para është triggeri PG) ---------------------------------------------------


def _frozen(target, fields, what: str) -> None:
    attrs = inspect(target).attrs
    changed = [f for f in fields if getattr(attrs, f).history.has_changes()]
    if changed:
        raise BillingImmutableError(f"{what} fields are immutable: {changed}")


@event.listens_for(CommercialPlan, "before_update")
def _plan_frozen(_m, _c, t) -> None:
    _frozen(t, ("id", "code", "name", "created_by_id", "created_at"), "commercial plan")


@event.listens_for(PlanVersion, "before_update")
def _version_guard(_m, _c, t) -> None:
    _frozen(t, ("id", "plan_id", "version", "created_by_id", "created_at"), "plan version")
    attrs = inspect(t).attrs
    old = attrs.status.history.deleted[0] if attrs.status.history.deleted else t.status
    if old != V_DRAFT:
        _frozen(
            t,
            (
                "currency",
                "monthly_fee",
                "included_emails",
                "content_hash",
                "activated_at",
                "activated_by_id",
            ),
            "activated plan version",
        )
        if old == V_RETIRED and t.status != V_RETIRED:
            raise BillingImmutableError("retired plan version is final")
        if old == V_ACTIVE and t.status not in (V_ACTIVE, V_RETIRED):
            raise BillingImmutableError("an active plan version can only be retired")
    elif t.status not in (V_DRAFT, V_ACTIVE):
        raise BillingImmutableError("a draft can only be activated")


@event.listens_for(BillingSubscription, "before_update")
def _sub_frozen(_m, _c, t) -> None:
    _frozen(t, ("id", "enterprise_id", "created_at"), "billing subscription")


_INVOICE_FROZEN = (
    "id", "provenance", "number", "enterprise_id", "subscription_id", "period_index", "period_start", "period_end",
    "plan_version_id", "currency", "subtotal", "vat_rate", "tax", "total", "bill_to", "issuer",
    "issued_at", "due_at", "created_at",
)  # fmt: skip


@event.listens_for(Invoice, "before_update")
def _invoice_guard(_m, _c, t) -> None:
    _frozen(t, _INVOICE_FROZEN, "invoice")
    attrs = inspect(t).attrs
    old = attrs.status.history.deleted[0] if attrs.status.history.deleted else t.status
    if old != INV_OPEN and t.status != old:
        raise BillingImmutableError(f"invoice status {old} is final")
    if old != INV_OPEN:
        _frozen(t, ("paid_at", "voided_at", "voided_by_id", "voided_reason"), "settled invoice")


@event.listens_for(BillingPeriod, "before_update")
@event.listens_for(InvoiceLine, "before_update")
def _append_only(*_) -> None:
    raise BillingImmutableError("billing periods and invoice lines are append-only")


@event.listens_for(CommercialPlan, "before_delete")
@event.listens_for(PlanVersion, "before_delete")
@event.listens_for(BillingSubscription, "before_delete")
@event.listens_for(BillingProfile, "before_delete")
@event.listens_for(Invoice, "before_delete")
@event.listens_for(InvoiceLine, "before_delete")
@event.listens_for(BillingPeriod, "before_delete")
@event.listens_for(InvoiceNumberSequence, "before_delete")
def _never_deleted(*_) -> None:
    raise BillingImmutableError("billing rows are never deleted")
