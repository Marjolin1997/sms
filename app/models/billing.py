"""Faturimi: plane, abonime, profil fature, fatura të pandryshueshme, pagesa online."""

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.rates import PK
from app.models.tenant import TenantOwned
from app.models.wallet import MONEY, utcnow


class PlanStatus(enum.StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"  # s'mund të caktohet më; abonimet ekzistuese vazhdojnë


class Plan(Base):
    """Plan i pandryshueshëm: një ndryshim çmimi = plan i ri (kodi + versioni)."""

    __tablename__ = "sms_plans"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    currency: Mapped[str] = mapped_column(String(3))
    monthly_fee: Mapped[Decimal] = mapped_column(MONEY)
    included_emails: Mapped[int] = mapped_column(Integer, default=0)
    email_overage_price: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=Decimal(0))
    status: Mapped[PlanStatus] = mapped_column(
        Enum(PlanStatus, native_enum=False, length=16), default=PlanStatus.ACTIVE
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint("monthly_fee >= 0", name="fee_non_negative"),
        CheckConstraint("included_emails >= 0", name="included_non_negative"),
        CheckConstraint("email_overage_price >= 0", name="overage_non_negative"),
    )


class SubStatus(enum.StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"


class Subscription(TenantOwned, Base):
    __tablename__ = "sms_subscriptions"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_ref: Mapped[str] = mapped_column(String(64), unique=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("sms_plans.id"))
    pending_plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("sms_plans.id")
    )  # nga periudha tjetër
    status: Mapped[SubStatus] = mapped_column(
        Enum(SubStatus, native_enum=False, length=16), default=SubStatus.ACTIVE
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # ankorë e periudhave
    periods_billed: Mapped[int] = mapped_column(Integer, default=0)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_pay: Mapped[bool] = mapped_column(
        Boolean, default=True
    )  # provo wallet-in kur lëshohet fatura
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BillingProfile(TenantOwned, Base):
    __tablename__ = "sms_billing_profiles"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_ref: Mapped[str] = mapped_column(String(64), unique=True)
    legal_name: Mapped[str] = mapped_column(String(120))
    address: Mapped[str] = mapped_column(String(300))
    country: Mapped[str] = mapped_column(String(2))
    tax_id: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str] = mapped_column(String(254))
    # Vendoset vetëm nga stafi (klienti s'mund të zgjedhë tatimin e vet), p.sh. 0.2 = 20%.
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal(0))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (CheckConstraint("vat_rate >= 0 AND vat_rate <= 1", name="vat_range"),)


class InvoiceStatus(enum.StrEnum):
    OPEN = "open"
    PAID = "paid"
    VOID = "void"


class InvoiceCounter(Base):
    """Numërim pa boshllëqe për vit: rreshti kyçet FOR UPDATE në të njëjtin transaksion."""

    __tablename__ = "sms_invoice_counters"

    year: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    last_number: Mapped[int] = mapped_column(Integer, default=0)


class Invoice(TenantOwned, Base):
    __tablename__ = "sms_invoices"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    number: Mapped[str] = mapped_column(String(24), unique=True)
    owner_ref: Mapped[str] = mapped_column(String(64))
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("sms_subscriptions.id"))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    currency: Mapped[str] = mapped_column(String(3))
    subtotal: Mapped[Decimal] = mapped_column(MONEY)
    vat_rate: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    tax: Mapped[Decimal] = mapped_column(MONEY)
    total: Mapped[Decimal] = mapped_column(MONEY)
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, native_enum=False, length=16), default=InvoiceStatus.OPEN
    )
    # Fotografi e profilit në çastin e lëshimit; profili i ri s'ndryshon faturat e vjetra.
    bill_to: Mapped[str] = mapped_column(Text)  # JSON i profilit në çastin e lëshimit
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_via: Mapped[str | None] = mapped_column(String(24))  # wallet | online
    voided_reason: Mapped[str | None] = mapped_column(String(200))

    __table_args__ = (
        UniqueConstraint("subscription_id", "period_start", name="uq_sms_invoices_sub_period"),
        CheckConstraint("total >= 0", name="total_non_negative"),
        Index("ix_sms_invoices_owner", "owner_ref", "id"),
        Index("ix_sms_invoices_status_due", "status", "due_at"),
    )


class InvoiceLine(Base):
    __tablename__ = "sms_invoice_lines"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("sms_invoices.id"), index=True)
    description: Mapped[str] = mapped_column(String(200))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    amount: Mapped[Decimal] = mapped_column(MONEY)


class PaymentPurpose(enum.StrEnum):
    TOPUP = "topup"
    INVOICE = "invoice"


class PaymentStatus(enum.StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"


class Payment(TenantOwned, Base):
    """Seancë pagese online. Shuma vendoset NGA SERVERI kur krijohet; webhook-u i gateway-t
    verifikohet kundrejt kësaj shume, kurrë kundrejt asaj që dërgon klienti."""

    __tablename__ = "sms_payments"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_ref: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[PaymentPurpose] = mapped_column(
        Enum(PaymentPurpose, native_enum=False, length=16)
    )
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("sms_invoices.id"))
    wallet_id: Mapped[int | None] = mapped_column(ForeignKey("sms_wallets.id"))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3))
    provider: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128))
    checkout_url: Mapped[str] = mapped_column(String(2000))
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, native_enum=False, length=16), default=PaymentStatus.PENDING
    )
    failure_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_sms_payments_provider_ext"),
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_sms_payments_owner", "owner_ref", "id"),
    )


class InvoiceImmutableError(RuntimeError):
    pass


_FROZEN = ("number", "owner_ref", "period_start", "period_end", "currency", "subtotal",
           "vat_rate", "tax", "total", "bill_to", "issued_at")  # fmt: skip


@event.listens_for(Invoice, "before_update")
def _invoice_frozen(_m, _c, target):
    from sqlalchemy import inspect

    attrs = inspect(target).attrs
    changed = [f for f in _FROZEN if getattr(attrs, f).history.has_changes()]
    if changed:
        raise InvoiceImmutableError(f"invoice fields are immutable once issued: {changed}")


@event.listens_for(Invoice, "before_delete")
@event.listens_for(InvoiceLine, "before_update")
@event.listens_for(InvoiceLine, "before_delete")
def _invoice_no_delete(*_):
    raise InvoiceImmutableError("issued invoices and their lines cannot be modified or deleted")
