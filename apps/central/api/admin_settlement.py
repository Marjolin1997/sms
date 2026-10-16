"""API admin e shlyerjes së faturave (M9-g3): pagesa fature, alokime (vetëm lexim), credit notes, përmbledhje/aging. Admin = shkrim, operator = lexim.
Pa DELETE, pa API klienti, pa wallet SMS. Shumat janë string dhjetorë (kurrë float); `extra=forbid`; çelës idempotence i qartë (external_reference / idempotency_key)."""

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, StrictStr
from sqlalchemy.orm import Session

from apps.central.api.admin_common import (
    STRICT,
    Amount,
    Currency,
    IdempotencyKey,
    Note,
    Reason,
    actor_of,
    iso,
    money_str,
    page,
)
from apps.central.api.deps import get_db, require_role
from apps.central.models.billing import Invoice
from apps.central.models.money import Payment
from apps.central.models.settlement import CreditNote, InvoicePaymentAllocation
from apps.central.models.user import CentralUser, Role
from apps.central.services import credit_notes, invoice_payments, settlement_reports

router = APIRouter(prefix="/admin/billing")
READ = require_role(Role.ADMIN, Role.OPERATOR)
WRITE = require_role(Role.ADMIN)
MAX_PAGE = 200
Ref = Field(min_length=1, max_length=128)


class InvoicePaymentIn(BaseModel):
    model_config = STRICT
    invoice_id: uuid.UUID
    amount: Amount
    external_reference: StrictStr = Ref
    currency: Currency | None = None
    source: StrictStr = "manual"
    note: Note | None = None


class RejectIn(BaseModel):
    model_config = STRICT
    reason: Reason


class CreditNoteIn(BaseModel):
    model_config = STRICT
    invoice_id: uuid.UUID
    amount: Amount
    reason: Reason
    idempotency_key: IdempotencyKey
    currency: Currency | None = None


def payment_out(p: Payment, alloc: InvoicePaymentAllocation | None = None) -> dict:
    return {
        "id": str(p.id), "purpose": p.purpose, "invoice_id": str(p.invoice_id), "enterprise_id": str(p.enterprise_id),
        "currency": p.currency, "amount": money_str(p.amount), "source": p.source, "external_reference": p.external_reference,
        "note": p.note, "status": p.status, "created_by": actor_of(p.created_by_id, p.created_by_label), "created_at": iso(p.created_at),
        "approved_by": actor_of(p.approved_by_id, None), "approved_at": iso(p.approved_at),
        "rejected_by": actor_of(p.rejected_by_id, None), "rejected_at": iso(p.rejected_at), "rejection_reason": p.rejection_reason,
        "allocation_id": None if alloc is None else str(alloc.id),
    }  # fmt: skip


def allocation_out(a: InvoicePaymentAllocation) -> dict:
    return {"id": str(a.id), "payment_id": str(a.payment_id), "invoice_id": str(a.invoice_id), "enterprise_id": str(a.enterprise_id),
            "amount": money_str(a.amount), "currency": a.currency, "allocated_at": iso(a.allocated_at),
            "allocated_by": actor_of(a.allocated_by_id, None)}  # fmt: skip


def credit_note_out(n: CreditNote) -> dict:
    return {"id": str(n.id), "number": n.number, "invoice_id": str(n.invoice_id), "enterprise_id": str(n.enterprise_id),
            "currency": n.currency, "amount": money_str(n.amount), "reason": n.reason, "issued_at": iso(n.issued_at),
            "created_by": actor_of(n.created_by_id, None), "issuer": n.issuer, "bill_to": n.bill_to}  # fmt: skip


def settlement_out(db: Session, inv: Invoice) -> dict:
    """Pamja e shlyerjes për detajin e faturës: alokimi, pagesat, credit notes, neto."""
    s = settlement_reports.invoice_settlement(db, inv)
    return {"allocation": None if s["allocation"] is None else allocation_out(s["allocation"]),
            "payments": [payment_out(p) for p in s["payments"]], "credit_notes": [credit_note_out(n) for n in s["credit_notes"]],
            "credited_total": money_str(s["credited_total"]), "net_amount": money_str(s["net_amount"])}  # fmt: skip


# --- pagesat e faturave --------------------------------------------------------------------------------------------


@router.get("/invoice-payments")
def list_invoice_payments(invoice_id: uuid.UUID | None = None, enterprise_id: uuid.UUID | None = None,
                          status: str | None = Query(None, pattern="^(pending|approved|rejected)$"),
                          limit: int = Query(50, ge=1, le=MAX_PAGE), offset: int = Query(0, ge=0),
                          db: Session = Depends(get_db), _: CentralUser = Depends(READ)):  # fmt: skip
    rows = invoice_payments.list_payments(
        db,
        invoice_id=invoice_id,
        enterprise_id=enterprise_id,
        status=status,
        limit=limit + 1,
        offset=offset,
    )
    return page(rows, limit, offset, payment_out)


@router.post("/invoice-payments", status_code=201)
def create_invoice_payment(
    body: InvoicePaymentIn, db: Session = Depends(get_db), actor: CentralUser = Depends(WRITE)
):
    p = invoice_payments.create(db, body.invoice_id, body.amount, actor=actor, currency=body.currency, source=body.source,
                                external_reference=body.external_reference, note=body.note)  # fmt: skip
    db.commit()
    return payment_out(p)


@router.get("/invoice-payments/{payment_id}")
def get_invoice_payment(
    payment_id: uuid.UUID, db: Session = Depends(get_db), _: CentralUser = Depends(READ)
):
    p = invoice_payments.get(db, payment_id)
    return payment_out(p, invoice_payments.allocation_of_payment(db, p.id))


@router.post("/invoice-payments/{payment_id}/approve")
def approve_invoice_payment(
    payment_id: uuid.UUID, db: Session = Depends(get_db), actor: CentralUser = Depends(WRITE)
):
    p = invoice_payments.approve(db, payment_id, actor)
    alloc = invoice_payments.allocation_of_payment(db, p.id)
    out = payment_out(p, alloc)
    db.commit()
    return out


@router.post("/invoice-payments/{payment_id}/reject")
def reject_invoice_payment(
    payment_id: uuid.UUID,
    body: RejectIn,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    p = invoice_payments.reject(db, payment_id, actor, body.reason)
    db.commit()
    return payment_out(p)


# --- alokimet (vetëm lexim) ----------------------------------------------------------------------------------------


@router.get("/allocations")
def list_allocations(invoice_id: uuid.UUID | None = None, enterprise_id: uuid.UUID | None = None, limit: int = Query(50, ge=1, le=MAX_PAGE),
                     offset: int = Query(0, ge=0), db: Session = Depends(get_db), _: CentralUser = Depends(READ)):  # fmt: skip
    rows = invoice_payments.list_allocations(
        db, invoice_id=invoice_id, enterprise_id=enterprise_id, limit=limit + 1, offset=offset
    )
    return page(rows, limit, offset, allocation_out)


@router.get("/allocations/{allocation_id}")
def get_allocation(
    allocation_id: uuid.UUID, db: Session = Depends(get_db), _: CentralUser = Depends(READ)
):
    return allocation_out(invoice_payments.get_allocation(db, allocation_id))


# --- credit notes ----------------------------------------------------------------------------------------------------


@router.get("/credit-notes")
def list_credit_notes(invoice_id: uuid.UUID | None = None, enterprise_id: uuid.UUID | None = None, limit: int = Query(50, ge=1, le=MAX_PAGE),
                      offset: int = Query(0, ge=0), db: Session = Depends(get_db), _: CentralUser = Depends(READ)):  # fmt: skip
    rows = credit_notes.list_notes(
        db, invoice_id=invoice_id, enterprise_id=enterprise_id, limit=limit + 1, offset=offset
    )
    return page(rows, limit, offset, credit_note_out)


@router.post("/credit-notes", status_code=201)
def issue_credit_note(
    body: CreditNoteIn, db: Session = Depends(get_db), actor: CentralUser = Depends(WRITE)
):
    n = credit_notes.issue(
        db,
        actor,
        body.invoice_id,
        body.amount,
        body.reason,
        body.idempotency_key,
        currency=body.currency,
    )
    out = credit_note_out(n)
    db.commit()
    return out


@router.get("/credit-notes/{credit_note_id}")
def get_credit_note(
    credit_note_id: uuid.UUID, db: Session = Depends(get_db), _: CentralUser = Depends(READ)
):
    return credit_note_out(credit_notes.get(db, credit_note_id))


# --- përmbledhje / aging -----------------------------------------------------------------------------------------------


@router.get("/settlement")
def settlement_summary(db: Session = Depends(get_db), _: CentralUser = Depends(READ)):
    """Rakordim vetëm-lexim: numërues faturash/pagesash, aging i faturave OPEN, anomali të shlyerjes (pa korrigjim automatik)."""
    from apps.central.core.config import settings

    return settlement_reports.summary(db, stale_seconds=settings.payment_pending_stale_seconds)
