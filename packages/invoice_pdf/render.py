"""Renderues i përbashkët i faturës zyrtare PDF (M11-E2). Përdoret nga Enterprise dhe Central.

Parime:
- Hyrja është një `InvoiceDoc` me TEKSTE të gatshme (shumat janë stringje dhjetore të ruajtura në DB). Renderuesi
  NUK llogarit asnjë total, tatim apo zbritje: vetëm i vendos në faqe. Formatimi lejohet (grupim mijëshash), rillogaritja jo.
- Asnjë e dhënë e ndryshueshme nuk lexohet këtu (as DB, as konfigurim): thirrësi jep snapshot-in e ruajtur.
- Fontet DejaVu Sans janë të bashkangjitura në repo (`fonts/`), ndaj shqipja, gjermanishtja dhe shkronjat Unicode
  të përbashkëta printohen njëlloj kudo; asnjë font i makinës nuk përdoret.
- Dalja është e përcaktuar: `invariant=1` heq datën e krijimit/ID-në e rastësishme, pra e njëjta faturë jep të njëjtat bajte.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_FONTS = Path(__file__).parent / "fonts"
_REGULAR, _BOLD = "InvoiceSans", "InvoiceSans-Bold"
_registered = False


def _fonts() -> None:
    global _registered
    if not _registered:
        pdfmetrics.registerFont(TTFont(_REGULAR, str(_FONTS / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont(_BOLD, str(_FONTS / "DejaVuSans-Bold.ttf")))
        pdfmetrics.registerFontFamily(
            _REGULAR, normal=_REGULAR, bold=_BOLD, italic=_REGULAR, boldItalic=_BOLD
        )
        _registered = True


@dataclass(frozen=True)
class LineDoc:
    description: str
    quantity: str
    unit_price: str
    amount: str


@dataclass(frozen=True)
class NoteDoc:
    number: str
    amount: str
    issued: str
    reason: str = ""


@dataclass(frozen=True)
class InvoiceDoc:
    number: str
    status: str
    currency: str
    issued: str  # data tekst e gatshme (YYYY-MM-DD, UTC)
    due: str
    period_start: str
    period_end: str
    issuer: dict
    bill_to: dict
    lines: list[LineDoc]
    subtotal: str
    vat_rate: str
    tax: str
    total: str
    paid: str | None = None
    credit_notes: list[NoteDoc] = field(default_factory=list)
    labels: dict = field(default_factory=dict)  # etiketat e përkthyera; mungesa ⇒ anglisht


_L = {
    "invoice": "Invoice",
    "issued": "Issued",
    "due": "Due",
    "period": "Period",
    "to": "to",
    "paid": "Paid",
    "issuer": "From",
    "bill_to": "Billed to",
    "description": "Description",
    "qty": "Qty",
    "unit": "Unit price",
    "amount": "Amount",
    "subtotal": "Subtotal",
    "vat": "VAT",
    "total": "Total",
    "credit_notes": "Credit notes",
    "status": "Status",
    "tax_id": "Tax ID",
    "page": "Page",
}


def _group(value: str) -> str:
    """ "1234567.5" → "1,234,567.50": grupim me string (kurrë float). Vlerë që s'është dhjetore kthehet siç është."""
    try:
        d = Decimal(value)
    except (InvalidOperation, TypeError):
        return str(value)
    if not d.is_finite():
        return str(value)
    sign, digits, exp = d.as_tuple()
    text = format(d, "f")
    neg = text.startswith("-")
    int_part, _, frac = text.lstrip("-").partition(".")
    frac = frac.rstrip("0")
    frac = frac.ljust(2, "0")
    grouped = re.sub(r"\B(?=(\d{3})+(?!\d))", ",", int_part)
    return f"{'-' if neg else ''}{grouped}.{frac}"


def _qty(value: str) -> str:
    try:
        d = Decimal(value)
    except (InvalidOperation, TypeError):
        return str(value)
    text = format(d.normalize(), "f") if d.is_finite() else str(value)
    int_part, _, frac = text.lstrip("-").partition(".")
    grouped = re.sub(r"\B(?=(\d{3})+(?!\d))", ",", int_part)
    return f"{'-' if text.startswith('-') else ''}{grouped}{'.' + frac if frac else ''}"


def safe_filename(number: str) -> str:
    """invoice-<numri>.pdf: vetëm [A-Za-z0-9._-]; çdo gjë tjetër bëhet "_" (pa shteg, pa CR/LF, pa Unicode në header)."""
    base = unicodedata.normalize("NFKD", str(number)).encode("ascii", "ignore").decode()
    base = (
        re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9._-]+", "_", base)).strip("._-")[:48]
        or "invoice"
    )
    return f"invoice-{base}.pdf"


def _party(d: dict, tax_label: str) -> str:
    """Fushat e njohura të snapshot-it, me escape; asnjë fushë tjetër nuk del."""
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
    parts = []
    name = d.get("legal_name") or d.get("name")  # bill_to: legal_name; issuer-i i Central: name
    if name:
        parts.append(f"<b>{esc(name)}</b>")
    for key in ("address", "country"):
        if d.get(key):
            parts.append(esc(d[key]))
    if d.get("tax_id"):
        parts.append(f"{esc(tax_label)}: {esc(d['tax_id'])}")
    if d.get("email"):
        parts.append(esc(d["email"]))
    return "<br/>".join(parts) or "-"


def render_invoice_pdf(doc: InvoiceDoc) -> bytes:
    _fonts()
    L = {**_L, **doc.labels}
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
    base = ParagraphStyle("b", fontName=_REGULAR, fontSize=9, leading=12)
    right = ParagraphStyle("r", parent=base, alignment=TA_RIGHT)
    bold_r = ParagraphStyle("br", parent=right, fontName=_BOLD)
    title = ParagraphStyle("t", parent=base, fontName=_BOLD, fontSize=18, leading=22)
    muted = ParagraphStyle("m", parent=base, textColor=colors.HexColor("#555555"))
    th = ParagraphStyle("th", parent=base, fontName=_BOLD)
    th_r = ParagraphStyle("thr", parent=th, alignment=TA_RIGHT)
    buf = BytesIO()
    pdf = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f"{L['invoice']} {doc.number}",
        author="",
        invariant=1,
    )
    story = [Paragraph(f"{esc(L['invoice'])} {esc(doc.number)}", title), Spacer(1, 3 * mm)]
    meta = f"{esc(L['issued'])}: {esc(doc.issued)} &nbsp;·&nbsp; {esc(L['due'])}: {esc(doc.due)} &nbsp;·&nbsp; {esc(L['period'])}: {esc(doc.period_start)} {esc(L['to'])} {esc(doc.period_end)}"
    meta += f" &nbsp;·&nbsp; {esc(L['status'])}: {esc(doc.status)}"
    if doc.paid:
        meta += f" &nbsp;·&nbsp; {esc(L['paid'])}: {esc(doc.paid)}"
    story += [Paragraph(meta, muted), Spacer(1, 6 * mm)]
    parties = Table(
        [
            [
                Paragraph(f"<b>{esc(L['issuer'])}</b>", base),
                Paragraph(f"<b>{esc(L['bill_to'])}</b>", base),
            ],
            [
                Paragraph(_party(doc.issuer, L["tax_id"]), base),
                Paragraph(_party(doc.bill_to, L["tax_id"]), base),
            ],
        ],
        colWidths=[87 * mm, 87 * mm],
    )
    parties.setStyle(
        TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)])
    )
    story += [parties, Spacer(1, 6 * mm)]
    cur = esc(doc.currency)
    rows = [
        [
            Paragraph(esc(L["description"]), th),
            Paragraph(esc(L["qty"]), th_r),
            Paragraph(esc(L["unit"]), th_r),
            Paragraph(f"{esc(L['amount'])} ({cur})", th_r),
        ]
    ]
    for ln in doc.lines:
        rows.append(
            [
                Paragraph(esc(ln.description), base),
                Paragraph(esc(_qty(ln.quantity)), right),
                Paragraph(esc(_group(ln.unit_price)), right),
                Paragraph(esc(_group(ln.amount)), right),
            ]
        )
    lines = Table(rows, colWidths=[84 * mm, 24 * mm, 30 * mm, 36 * mm], repeatRows=1)
    lines.setStyle(
        TableStyle(
            [
                ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black),
                ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#cccccc")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (0, -1), 0),
            ]
        )
    )
    story += [lines, Spacer(1, 4 * mm)]
    vat = f"{esc(L['vat'])} ({esc(doc.vat_rate)})"
    totals = Table(
        [
            [
                Paragraph(esc(L["subtotal"]), right),
                Paragraph(f"{esc(_group(doc.subtotal))} {cur}", right),
            ],
            [Paragraph(vat, right), Paragraph(f"{esc(_group(doc.tax))} {cur}", right)],
            [
                Paragraph(esc(L["total"]), bold_r),
                Paragraph(f"{esc(_group(doc.total))} {cur}", bold_r),
            ],
        ],
        colWidths=[138 * mm, 36 * mm],
    )
    totals.setStyle(TableStyle([("LINEABOVE", (0, 2), (-1, 2), 0.8, colors.black)]))
    story.append(totals)
    if doc.credit_notes:
        story += [
            Spacer(1, 8 * mm),
            Paragraph(f"<b>{esc(L['credit_notes'])}</b>", base),
            Spacer(1, 2 * mm),
        ]
        notes = Table(
            [
                [
                    Paragraph(esc(n.number), base),
                    Paragraph(esc(n.issued), base),
                    Paragraph(esc(n.reason), base),
                    Paragraph(f"{esc(_group(n.amount))} {cur}", right),
                ]
                for n in doc.credit_notes
            ],
            colWidths=[34 * mm, 24 * mm, 80 * mm, 36 * mm],
        )
        notes.setStyle(
            TableStyle(
                [
                    ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
                    ("LEFTPADDING", (0, 0), (0, -1), 0),
                ]
            )
        )
        story.append(notes)

    def footer(canvas, d):
        canvas.saveState()
        canvas.setFont(_REGULAR, 8)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawString(18 * mm, 10 * mm, f"{doc.number}")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"{L['page']} {d.page}")
        canvas.restoreState()

    pdf.build(story, onFirstPage=footer, onLaterPages=footer)
    out = buf.getvalue()
    if not out.startswith(b"%PDF-"):
        raise RuntimeError("renderer produced a non-PDF payload")
    return out
