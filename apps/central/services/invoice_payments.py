"""M9-g3: pagesat e faturave (Central). `payments` me `purpose=invoice` (pa llogari krediti, pa ledger tregtar, pa wallet SMS).

V1: një pagesë = alokim i plotë; shuma = `invoice.total` e faturës OPEN (pa pjesëtime, pa mbipagesë/nënpagesë, pa konvertim në kredi). Pa HTTP, pa commit.

`approve` është ATOMIK: kyç pagesën → kyç faturën; valido pending + maker-checker + faturë OPEN + shumë/monedhë të sakta + pa alokim paraprak; krijo alokimin e
pandryshueshëm; pagesa → approved; fatura → paid (`paid_at`); audit i pagesës dhe i shlyerjes. Çdo dështim rikthen gjithçka. Rendi i kyçjeve (pagesë → faturë) është
i njëjtë kudo; `void_invoice` kyç vetëm faturën ⇒ asnjë deadlock; kush fiton mbi faturë e vendos rezultatin e vetëm terminal (paid | void)."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.central.core.errors import Conflict, Invalid, NotFound
from apps.central.core.timeutil import utcnow
from apps.central.models.billing import INV_OPEN, INV_PAID
from apps.central.models.money import APPROVED, PENDING, PURPOSE_INVOICE, REJECTED, Payment
from apps.central.models.settlement import InvoicePaymentAllocation
from apps.central.services import audit, billing, money_common, payments

ACTION_CREATE, ACTION_APPROVE, ACTION_REJECT = "payment.create", "payment.approve", "payment.reject"
ACTION_SETTLE = "invoice.settle"
RESOURCE = "payment"


def get(db: Session, payment_id, *, lock: bool = False) -> Payment:
    p = payments.get(db, payment_id, lock=lock)
    if p.purpose != PURPOSE_INVOICE:
        raise NotFound("invoice payment not found")
    return p


def list_payments(db: Session, *, invoice_id=None, enterprise_id=None, status: str | None = None, limit: int = 100,
                  offset: int = 0) -> list[Payment]:  # fmt: skip
    q = select(Payment).where(Payment.purpose == PURPOSE_INVOICE)
    if invoice_id is not None:
        q = q.where(Payment.invoice_id == money_common.uid(invoice_id, "invoice id"))
    if enterprise_id is not None:
        q = q.where(Payment.enterprise_id == money_common.uid(enterprise_id, "enterprise id"))
    if status is not None:
        q = q.where(Payment.status == status)
    q = q.order_by(Payment.created_at, Payment.id)
    return list(db.scalars(q.limit(max(1, min(limit, 500))).offset(max(0, offset))))


def allocation_of_payment(db: Session, payment_id) -> InvoicePaymentAllocation | None:
    return db.scalar(
        select(InvoicePaymentAllocation).where(InvoicePaymentAllocation.payment_id == payment_id)
    )


def allocation_of_invoice(db: Session, invoice_id) -> InvoicePaymentAllocation | None:
    return db.scalar(
        select(InvoicePaymentAllocation).where(InvoicePaymentAllocation.invoice_id == invoice_id)
    )


def list_allocations(db: Session, *, invoice_id=None, enterprise_id=None, limit: int = 100, offset: int = 0) -> list[InvoicePaymentAllocation]:  # fmt: skip
    q = select(InvoicePaymentAllocation)
    if invoice_id is not None:
        q = q.where(
            InvoicePaymentAllocation.invoice_id == money_common.uid(invoice_id, "invoice id")
        )
    if enterprise_id is not None:
        q = q.where(
            InvoicePaymentAllocation.enterprise_id
            == money_common.uid(enterprise_id, "enterprise id")
        )
    q = q.order_by(InvoicePaymentAllocation.allocated_at, InvoicePaymentAllocation.id)
    return list(db.scalars(q.limit(max(1, min(limit, 500))).offset(max(0, offset))))


def get_allocation(db: Session, allocation_id) -> InvoicePaymentAllocation:
    row = db.get(InvoicePaymentAllocation, money_common.uid(allocation_id, "allocation id"))
    if row is None:
        raise NotFound("allocation not found")
    return row


def _outstanding(inv) -> Decimal:
    """V1: faturë OPEN pa alokime të pjesshme ⇒ outstanding = total."""
    return Decimal(inv.total).quantize(money_common.QUANT)


def create(db: Session, invoice_id, amount, *, actor=None, system: str | None = None, currency=None, source: str = "manual",
           external_reference=None, note=None, now: datetime | None = None) -> Payment:  # fmt: skip
    """Pagesë `pending` për një faturë OPEN: shuma = outstanding, monedha = monedha e faturës (përndryshe Conflict). `(source, external_reference)`
    është çelësi i idempotencës: e njëjta përmbajtje ⇒ pagesa ekzistuese; përmbajtje tjetër ⇒ Conflict."""
    if (actor is None) == (system is None):
        raise Invalid("exactly one of actor or system is required")
    actor_id = label = None
    if actor is not None:
        actor = money_common.admin(actor)
        actor_id = actor.id
    else:
        label = money_common.system_label(system)
    amt = money_common.money(amount)
    src = money_common.source_name(source)
    ref = money_common.external_reference(external_reference)
    text_note = money_common.optional_text(note, "note")

    def existing() -> Payment | None:
        if ref is None:
            return None
        return db.scalar(
            select(Payment).where(Payment.source == src, Payment.external_reference == ref)
        )

    def check(p: Payment) -> Payment:
        if (p.purpose, p.invoice_id, p.amount) != (PURPOSE_INVOICE, inv.id, amt):
            raise Conflict("external_reference was already used for a different payment")
        return p

    inv = billing.get_invoice(db, invoice_id, lock=True)
    if currency is not None and money_common.currency(currency) != inv.currency:
        raise Conflict("payment currency must equal the invoice currency (no FX)")
    if (p := existing()) is not None:
        return check(p)
    if inv.status != INV_OPEN:
        raise Conflict(f"invoice is {inv.status}: only an open invoice can receive a payment")
    if amt != _outstanding(inv):
        raise Conflict(
            "V1: the payment amount must equal the invoice outstanding amount (no partial payments or overpayment)"
        )
    now = now or utcnow()
    row = Payment(enterprise_id=inv.enterprise_id, purpose=PURPOSE_INVOICE, invoice_id=inv.id, account_id=None, currency=inv.currency,
                  amount=amt, source=src, external_reference=ref, note=text_note, status=PENDING, created_by_id=actor_id,
                  created_by_label=label, created_at=now, updated_at=now)  # fmt: skip
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:  # gara mbi (source, external_reference)
        if (winner := existing()) is None:
            raise
        return check(winner)
    detail = {"purpose": PURPOSE_INVOICE, "amount": str(amt), "currency": inv.currency, "invoice": inv.number,
              "invoice_id": str(inv.id), "source": src, "external_reference": ref}  # fmt: skip
    if actor is not None:
        audit.record(db, actor, ACTION_CREATE, RESOURCE, row.id, detail, now=now)
    else:
        audit.record_system(
            db,
            label=label,
            action=ACTION_CREATE,
            resource_type=RESOURCE,
            resource_id=row.id,
            detail=detail,
            now=now,
        )
    return row


def approve(db: Session, payment_id, actor, *, now: datetime | None = None) -> Payment:
    """pending → approved + alokim + faturë paid, atomikisht. approved ⇒ no-op (pa alokim/audit të dytë); rejected ⇒ Conflict."""
    actor = money_common.admin(actor)
    actor_id = actor.id  # lexo para ndryshimeve
    pre = get(db, payment_id)
    p = get(db, pre.id, lock=True)  # 1. kyç pagesën
    if p.status == APPROVED:
        if allocation_of_payment(db, p.id) is None:
            raise Conflict("approved invoice payment has no allocation (inconsistent state)")
        return p
    if p.status == REJECTED:
        raise Conflict("payment was rejected")
    if p.created_by_id is not None and p.created_by_id == actor_id:
        raise Conflict("maker-checker: the creator cannot approve their own payment")
    inv = billing.get_invoice(db, p.invoice_id, lock=True)  # 2. kyç faturën
    if inv.status != INV_OPEN:
        raise Conflict(f"invoice is {inv.status}: it cannot be settled by this payment")
    if p.currency != inv.currency or p.enterprise_id != inv.enterprise_id:
        raise Conflict("payment currency/enterprise does not match the invoice")
    if Decimal(p.amount).quantize(money_common.QUANT) != _outstanding(inv):
        raise Conflict("V1: the payment amount must equal the invoice outstanding amount")
    if allocation_of_invoice(db, inv.id) is not None or allocation_of_payment(db, p.id) is not None:
        raise Conflict("the invoice or the payment already has an allocation")
    now = billing.utc(now or utcnow())
    p.status, p.approved_at, p.approved_by_id, p.updated_at = APPROVED, now, actor_id, now
    inv.status, inv.paid_at = INV_PAID, now
    alloc = InvoicePaymentAllocation(payment_id=p.id, invoice_id=inv.id, enterprise_id=inv.enterprise_id, amount=p.amount,
                                     currency=inv.currency, allocated_at=now, allocated_by_id=actor_id)  # fmt: skip
    db.add(alloc)
    db.flush()
    audit.record(db, actor, ACTION_APPROVE, RESOURCE, p.id,
                 {"purpose": PURPOSE_INVOICE, "amount": str(p.amount), "currency": p.currency, "invoice": inv.number,
                  "allocation_id": str(alloc.id)}, now=now)  # fmt: skip
    audit.record(db, actor, ACTION_SETTLE, "invoice", inv.id,
                 {"number": inv.number, "total": str(inv.total), "currency": inv.currency, "payment_id": str(p.id)}, now=now)  # fmt: skip
    return p


def reject(db: Session, payment_id, actor, reason, *, now: datetime | None = None) -> Payment:
    """pending → rejected (faturë e pandryshuar). approved ⇒ Conflict (përdor credit note)."""
    return payments.reject(db, payment_id, actor, reason, now=now, purpose=PURPOSE_INVOICE)
