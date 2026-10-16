"""M9-g3: shlyerja e faturave në Central — alokimi i pagesës, shënimet e kreditit (credit notes) dhe numërimi i tyre.

- `InvoicePaymentAllocation`: prova e pandryshueshme që një pagesë `purpose=invoice` e mbyll një faturë (V1: një pagesë = një alokim i plotë;
  `UNIQUE(payment_id)` dhe `UNIQUE(invoice_id)`). FK e përbërë drejt pagesës e detyron shumën/monedhën/faturën të përputhen me pagesën.
- `CreditNote`: histori financiare e pandryshueshme (pa gjendje, pa fshirje). Vlen vetëm për fatura të PAGUARA; Σ shënimeve ≤ `invoice.total`.
- `CreditNoteSequence`: numërim pa boshllëqe `CN-{year}-{n:06d}` me rresht të kyçur (i ndarë nga numërimi i faturave).
Asnjë lidhje me wallet-in SMS apo me ledger-in tregtar të kredisë."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
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
)
from sqlalchemy.orm import Mapped, mapped_column

from apps.central.core.db import Base
from apps.central.core.timeutil import utcnow
from apps.central.models.billing import BillingImmutableError

MONEY = Numeric(20, 6)


class InvoicePaymentAllocation(Base):
    __tablename__ = "invoice_payment_allocations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    payment_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    currency: Mapped[str] = mapped_column(String(3))
    allocated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    allocated_by_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["payment_id", "invoice_id", "currency", "amount"],
            ["payments.id", "payments.invoice_id", "payments.currency", "payments.amount"],
            name="fk_allocations_payment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["invoice_id", "enterprise_id", "currency"],
            ["invoices.id", "invoices.enterprise_id", "invoices.currency"],
            name="fk_allocations_invoice_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("payment_id", name="uq_allocations_payment"),
        UniqueConstraint("invoice_id", name="uq_allocations_invoice"),
        CheckConstraint("amount > 0", name="amount_positive"),
    )


class CreditNoteSequence(Base):
    __tablename__ = "credit_note_sequence"

    year: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    last_number: Mapped[int] = mapped_column(BigInteger, default=0)

    __table_args__ = (CheckConstraint("last_number >= 0", name="non_negative"),)


class CreditNote(Base):
    __tablename__ = "credit_notes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    number: Mapped[str] = mapped_column(String(24), unique=True)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    invoice_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    currency: Mapped[str] = mapped_column(String(3))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    reason: Mapped[str] = mapped_column(String(500))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    issuer: Mapped[dict] = mapped_column(JSON)  # foto e issuer-it të faturës origjinale
    bill_to: Mapped[dict] = mapped_column(JSON)  # foto e bill-to të faturës origjinale
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["invoice_id", "enterprise_id", "currency"],
            ["invoices.id", "invoices.enterprise_id", "invoices.currency"],
            name="fk_credit_notes_invoice_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("invoice_id", "idempotency_key", name="uq_credit_notes_idempotency"),
        Index("ix_credit_notes_invoice", "invoice_id"),
        Index("ix_credit_notes_enterprise", "enterprise_id", "issued_at"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("length(trim(reason)) > 0", name="reason_required"),
        CheckConstraint("length(currency) = 3 AND currency = upper(currency)", name="currency"),
    )


@event.listens_for(InvoicePaymentAllocation, "before_update")
@event.listens_for(CreditNote, "before_update")
def _append_only(*_) -> None:
    raise BillingImmutableError("allocations and credit notes are immutable")


@event.listens_for(InvoicePaymentAllocation, "before_delete")
@event.listens_for(CreditNote, "before_delete")
@event.listens_for(CreditNoteSequence, "before_delete")
def _never_deleted(*_) -> None:
    raise BillingImmutableError("settlement rows are never deleted")
