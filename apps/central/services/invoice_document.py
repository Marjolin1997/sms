"""Faturë zyrtare PDF për Central (M11-E2): përshtatës nga rreshtat e RUAJTUR drejt renderuesit të përbashkët.

Vetëm lexim: issuer/bill_to janë snapshot-et e ruajtura në faturë (nuk lexohet asnjë profil/konfigurim i ndryshueshëm), shumat
janë vlerat e ruajtura (nuk rillogaritet asgjë), credit notes vijnë nga tabela e tyre. Një faturë = të njëjtat bajte PDF.
"""

import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from apps.central.api.admin_common import money_str
from apps.central.models.billing import Invoice
from apps.central.services import billing, credit_notes
from packages.invoice_pdf import InvoiceDoc, LineDoc, NoteDoc, render_invoice_pdf, safe_filename

log = logging.getLogger("central.invoice_pdf")


def _day(d: datetime | None) -> str:
    if d is None:
        return "-"
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(UTC).strftime("%Y-%m-%d")


def _qty(q: Decimal) -> str:
    return format(Decimal(q).normalize(), "f")


def build(db: Session, inv: Invoice) -> InvoiceDoc:
    lines = billing.lines_of(db, inv.id)
    notes = credit_notes.list_notes(db, invoice_id=inv.id, limit=500)
    return InvoiceDoc(
        number=inv.number, status=inv.status, currency=inv.currency, issued=_day(inv.issued_at), due=_day(inv.due_at),
        period_start=_day(inv.period_start), period_end=_day(inv.period_end), issuer=dict(inv.issuer or {}), bill_to=dict(inv.bill_to or {}),
        lines=[LineDoc(ln.description, _qty(ln.quantity), money_str(ln.unit_price), money_str(ln.amount)) for ln in lines],
        subtotal=money_str(inv.subtotal), vat_rate=str(inv.vat_rate), tax=money_str(inv.tax), total=money_str(inv.total),
        paid=_day(inv.paid_at) if inv.paid_at else None,
        credit_notes=[NoteDoc(n.number, money_str(n.amount), _day(n.issued_at), n.reason) for n in notes],
    )  # fmt: skip


def render(db: Session, inv: Invoice) -> tuple[bytes, str]:
    """→ (bajtet PDF, emri i sigurt i skedarit). Hedh përjashtim vetëm nëse renderimi dështon (thirrësi kthen 5xx të pastër)."""
    return render_invoice_pdf(build(db, inv)), safe_filename(inv.number)
