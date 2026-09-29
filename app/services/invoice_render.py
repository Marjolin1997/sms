"""Faturë e printueshme (HTML i vetë-mjaftueshëm). Çdo vlerë e ndryshueshme kalon nga escape."""

import html as html_lib
import json

from app.core.config import settings
from app.models.billing import Invoice, InvoiceLine
from app.services.billing import CENT, cents


def invoice_html(inv: Invoice, lines: list[InvoiceLine]) -> str:
    e = html_lib.escape
    bill = json.loads(inv.bill_to)
    rows = "".join(
        f"<tr><td>{e(ln.description)}</td><td class=n>{ln.quantity.normalize():f}</td>"
        f"<td class=n>{cents(ln.unit_price) if ln.unit_price >= CENT else ln.unit_price.normalize():f}</td>"
        f"<td class=n>{ln.amount:.2f}</td></tr>"
        for ln in lines
    )
    d = lambda x: x.strftime("%Y-%m-%d")  # noqa: E731
    return f"""<!doctype html><html lang=en><meta charset=utf-8><title>Invoice {e(inv.number)}</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;max-width:720px;margin:32px auto;color:#111}}
h1{{font-size:22px;margin:0}}table{{width:100%;border-collapse:collapse;margin:24px 0}}
th,td{{padding:8px;border-bottom:1px solid #ddd;text-align:left}}.n{{text-align:right}}
.muted{{color:#666}}.tot td{{border:0;font-weight:700}}.badge{{border:1px solid #999;padding:2px 8px;border-radius:99px}}
@media print{{body{{margin:0}}}}</style>
<h1>Invoice {e(inv.number)} <span class=badge>{e(inv.status.value)}</span></h1>
<p class=muted>Issued {d(inv.issued_at)} &middot; Due {d(inv.due_at)} &middot; Period {d(inv.period_start)} to {d(inv.period_end)}</p>
<table><tr><td><b>{e(settings.issuer_name)}</b><br>{e(settings.issuer_address)}<br>{e(settings.issuer_tax_id)}</td>
<td><b>{e(bill["legal_name"])}</b><br>{e(bill["address"])}<br>{e(bill["country"])}<br>{e(bill.get("tax_id") or "")}</td></tr></table>
<table><thead><tr><th>Description</th><th class=n>Qty</th><th class=n>Unit</th><th class=n>Amount</th></tr></thead>
<tbody>{rows}</tbody>
<tfoot><tr class=tot><td colspan=3 class=n>Subtotal</td><td class=n>{inv.subtotal:.2f} {e(inv.currency)}</td></tr>
<tr class=tot><td colspan=3 class=n>VAT ({inv.vat_rate * 100:.2f}%)</td><td class=n>{inv.tax:.2f} {e(inv.currency)}</td></tr>
<tr class=tot><td colspan=3 class=n>Total</td><td class=n>{inv.total:.2f} {e(inv.currency)}</td></tr></tfoot></table>
</html>"""
