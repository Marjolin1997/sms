"""M9-g3: pamje vetëm-lexim e shlyerjes — rakordim, aging, anomali. Asnjë korrigjim automatik, asnjë mutacion, pa PII (vetëm UUID/numra/shuma).

Anomalitë (secila kufizohet në `LIMIT` rreshta): paid pa alokim; pagesë fature e miratuar pa alokim; alokim i papërputhshëm (shuma/monedha/status);
alokim i dyfishtë; credit notes mbi total ose mbi faturë jo-të-paguar. Aging: faturat OPEN sipas ditëve pas `due_at` (current, 1–30, 31–60, 61–90, 90+); pa interes/penalitet."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from apps.central.core.timeutil import utcnow
from apps.central.models.billing import INV_OPEN, INV_PAID, Invoice
from apps.central.models.money import APPROVED, PENDING, PURPOSE_INVOICE, REJECTED, Payment
from apps.central.models.settlement import CreditNote, InvoicePaymentAllocation
from apps.central.services import billing, credit_notes, invoice_payments

LIMIT = 50
BUCKETS = ("current", "1-30", "31-60", "61-90", "90+")


def bucket(now: datetime, due_at: datetime) -> str:
    days = (
        (billing.utc(now) - billing.utc(due_at)).days
        if billing.utc(now) > billing.utc(due_at)
        else 0
    )
    if days <= 0:
        return "current"
    return "1-30" if days <= 30 else "31-60" if days <= 60 else "61-90" if days <= 90 else "90+"


def aging(db: Session, now: datetime | None = None, enterprise_id=None) -> dict:
    """{currency: {bucket: {"count": n, "amount": "x"}}} pér faturat OPEN."""
    now = billing.utc(now or utcnow())
    q = select(Invoice.currency, Invoice.due_at, Invoice.total).where(Invoice.status == INV_OPEN)
    if enterprise_id is not None:
        q = q.where(Invoice.enterprise_id == enterprise_id)
    out: dict = {}
    for cur, due, total in db.execute(q):
        b = out.setdefault(cur, {k: {"count": 0, "amount": Decimal(0)} for k in BUCKETS})[
            bucket(now, due)
        ]
        b["count"] += 1
        b["amount"] += Decimal(total)
    return {
        c: {k: {"count": v["count"], "amount": format(v["amount"], "f")} for k, v in bs.items()}
        for c, bs in out.items()
    }


def invoice_settlement(db: Session, inv: Invoice) -> dict:
    """Gjendja ekonomike e një fature: pagesa/alokimi, credit notes, neto."""
    alloc = invoice_payments.allocation_of_invoice(db, inv.id)
    notes = credit_notes.list_notes(db, invoice_id=inv.id, limit=500)
    credited = credit_notes.credited_total(db, inv.id)
    pays = invoice_payments.list_payments(db, invoice_id=inv.id, limit=500)
    return {"allocation": alloc, "credit_notes": notes, "credited_total": credited, "net_amount": Decimal(inv.total) - credited,
            "payments": pays}  # fmt: skip


def anomalies(db: Session) -> dict[str, list[str]]:
    lim = LIMIT
    paid_wo = db.scalars(
        select(Invoice.id).outerjoin(InvoicePaymentAllocation, InvoicePaymentAllocation.invoice_id == Invoice.id)
        .where(Invoice.status == INV_PAID, InvoicePaymentAllocation.id.is_(None)).limit(lim)
    )  # fmt: skip
    pay_wo = db.scalars(
        select(Payment.id).outerjoin(InvoicePaymentAllocation, InvoicePaymentAllocation.payment_id == Payment.id)
        .where(Payment.purpose == PURPOSE_INVOICE, Payment.status == APPROVED, InvoicePaymentAllocation.id.is_(None)).limit(lim)
    )  # fmt: skip
    mismatch = db.scalars(
        select(InvoicePaymentAllocation.id).join(Invoice, Invoice.id == InvoicePaymentAllocation.invoice_id)
        .join(Payment, Payment.id == InvoicePaymentAllocation.payment_id)
        .where(or_(InvoicePaymentAllocation.amount != Invoice.total, InvoicePaymentAllocation.currency != Invoice.currency,
                   Invoice.status != INV_PAID, Payment.status != APPROVED, Payment.invoice_id != InvoicePaymentAllocation.invoice_id))
        .limit(lim)
    )  # fmt: skip
    dup = db.scalars(select(InvoicePaymentAllocation.invoice_id).group_by(InvoicePaymentAllocation.invoice_id)
                     .having(func.count() > 1).limit(lim))  # fmt: skip
    over = db.scalars(
        select(CreditNote.invoice_id).join(Invoice, Invoice.id == CreditNote.invoice_id).group_by(CreditNote.invoice_id, Invoice.total)
        .having(func.sum(CreditNote.amount) > Invoice.total).limit(lim)
    )  # fmt: skip
    unpaid_cn = db.scalars(
        select(CreditNote.id)
        .join(Invoice, Invoice.id == CreditNote.invoice_id)
        .where(Invoice.status != INV_PAID)
        .limit(lim)
    )
    return {"paid_invoice_without_allocation": [str(x) for x in paid_wo], "approved_payment_without_allocation": [str(x) for x in pay_wo],
            "allocation_mismatch": [str(x) for x in mismatch], "duplicate_allocation": [str(x) for x in dup],
            "credit_notes_over_total": [str(x) for x in over], "credit_note_on_unpaid_invoice": [str(x) for x in unpaid_cn]}  # fmt: skip


def summary(db: Session, now: datetime | None = None, stale_seconds: int = 172800) -> dict:
    now = billing.utc(now or utcnow())
    inv = {
        s: n for s, n in db.execute(select(Invoice.status, func.count()).group_by(Invoice.status))
    }
    pay = {
        s: n
        for s, n in db.execute(
            select(Payment.status, func.count())
            .where(Payment.purpose == PURPOSE_INVOICE)
            .group_by(Payment.status)
        )
    }
    overdue = (
        db.scalar(
            select(func.count())
            .select_from(Invoice)
            .where(Invoice.status == INV_OPEN, Invoice.due_at < now)
        )
        or 0
    )
    pend_ages = [
        int((now - billing.utc(c)).total_seconds())
        for c in db.scalars(
            select(Payment.created_at).where(
                Payment.purpose == PURPOSE_INVOICE, Payment.status == PENDING
            )
        )
    ]
    rejected_unpaid = db.scalars(
        select(Payment.id).join(Invoice, Invoice.id == Payment.invoice_id)
        .where(Payment.purpose == PURPOSE_INVOICE, Payment.status == REJECTED, Invoice.status == INV_OPEN, Invoice.due_at < now)
        .limit(LIMIT)
    )  # fmt: skip
    return {
        "invoices": {"open": inv.get(INV_OPEN, 0), "paid": inv.get(INV_PAID, 0), "void": inv.get("void", 0), "overdue_open": int(overdue)},
        "invoice_payments": {"pending": pay.get(PENDING, 0), "approved": pay.get(APPROVED, 0), "rejected": pay.get(REJECTED, 0),
                             "stale_pending": sum(1 for a in pend_ages if a > stale_seconds), "stale_after_seconds": stale_seconds},
        "rejected_on_overdue_open_invoice": [str(x) for x in rejected_unpaid],
        "aging": aging(db, now), "anomalies": anomalies(db),
    }  # fmt: skip


__all__ = ["aging", "anomalies", "bucket", "invoice_settlement", "summary"]
