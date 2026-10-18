"""Faturë zyrtare PDF për Enterprise (M11-E2): përshtatës nga rreshtat e RUAJTUR drejt renderuesit të përbashkët.

Vetëm lexim, asnjë rillogaritje: totalet/rreshtat janë vlerat e ruajtura; bill-to është snapshot-i i ruajtur në faturë.
Lëshuesi vjen nga `invoice_issuer.issuer_of` (snapshot i ruajtur te fatura; vetëm faturat legacy pa snapshot bien eksplicit te `SMS_ISSUER_*`).
"""

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal

from app.core.texts import tr
from app.models.billing import Invoice, InvoiceLine
from app.services.invoice_issuer import issuer_of
from packages.invoice_pdf import InvoiceDoc, LineDoc, render_invoice_pdf, safe_filename

log = logging.getLogger("invoice_pdf")
_LABELS = {
    "invoice": "Invoice", "issued": "Issued", "due": "Due", "period": "Period", "to": "to", "paid": "Paid", "issuer": "From",
    "bill_to": "Billed to", "description": "Description", "qty": "Qty", "unit": "Unit price", "amount": "Amount",
    "subtotal": "Subtotal", "vat": "VAT", "total": "Total", "credit_notes": "Credit notes", "status": "Status",
    "tax_id": "Tax ID", "page": "Page",
}  # fmt: skip


def _day(d: datetime | None) -> str:
    if d is None:
        return "-"
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(UTC).strftime("%Y-%m-%d")


def _m(v: Decimal) -> str:
    return format(Decimal(v), "f")


def build(inv: Invoice, lines: list[InvoiceLine]) -> InvoiceDoc:
    return InvoiceDoc(
        number=inv.number, status=tr(inv.status.value), currency=inv.currency, issued=_day(inv.issued_at), due=_day(inv.due_at),
        period_start=_day(inv.period_start), period_end=_day(inv.period_end),
        issuer=issuer_of(inv)[0],
        bill_to=json.loads(inv.bill_to),
        lines=[LineDoc(ln.description, format(Decimal(ln.quantity).normalize(), "f"), _m(ln.unit_price), _m(ln.amount)) for ln in lines],
        subtotal=_m(inv.subtotal), vat_rate=_m(inv.vat_rate), tax=_m(inv.tax), total=_m(inv.total),
        paid=_day(inv.paid_at) if inv.paid_at else None,
        labels={k: tr(v) for k, v in _LABELS.items()},
    )  # fmt: skip


def render(inv: Invoice, lines: list[InvoiceLine]) -> tuple[bytes, str]:
    return render_invoice_pdf(build(inv, lines)), safe_filename(inv.number)
