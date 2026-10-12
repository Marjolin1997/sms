"""E2E i faturimit/wallet-it të klientit Enterprise me Chromium të vërtetë (M11-E2).

Kërkon: Enterprise API + vite i `frontend` me DB të përkohshme të mbushur nga e2e/enterprise_billing_seed.py.

    E2E_BASE=http://127.0.0.1:5173 E2E_IDS=ids.json python e2e/test_enterprise_billing.py
"""

import io
import json
import os
import re

from playwright.sync_api import sync_playwright
from pypdf import PdfReader

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:5173")
CHROME = os.environ.get("E2E_CHROME", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
IDS = json.load(open(os.environ["E2E_IDS"]))
errors: list[str] = []
passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise AssertionError(msg)
    passed += 1


def go(p, h):
    p.evaluate(f"location.hash = {json.dumps(h)}")


def main(b):
    ctx = b.new_context(viewport={"width": 1360, "height": 900})
    ctx.add_init_script("try{localStorage.setItem('sms_lang','en')}catch(e){}")
    p = ctx.new_page()
    p.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    p.on(
        "console",
        lambda m: (
            errors.append(f"console: {m.text}")
            if m.type == "error" and "Failed to load resource" not in m.text
            else None
        ),
    )
    seen = []
    p.on(
        "request",
        lambda r: seen.append(f"{r.method} {r.url}") if r.method not in ("GET", "HEAD") else None,
    )
    pdfs = []
    p.on("response", lambda r: pdfs.append(r) if "/pdf" in r.url else None)
    p.goto(BASE)
    p.evaluate("sessionStorage.clear()")
    p.reload()
    p.fill("input[type=password]", IDS["key_a"])
    p.click("button:has-text('Sign in')")
    p.wait_for_selector("nav")
    # wallet normal (EUR) dhe pending (USD)
    go(p, "#wallet")
    p.wait_for_selector("text=Available to spend")
    body = p.inner_text("main")
    check(
        "1,234.50" in body.replace(" ", ",") or "1.234,50" in body,
        "EUR wallet shows the server's balance exactly",
    )
    check("Pending recalculation" not in body, "normal wallet: no pending state")
    p.get_by_role("tab", name=re.compile("USD")).click()
    p.wait_for_selector("[data-testid=pending-recalculation]")
    stat = p.locator(".stat:has-text('Available to spend')").inner_text()
    check(
        "Pending recalculation" in stat and "50" in stat,
        "USD wallet: amount shown but labelled Pending recalculation",
    )
    check(
        "isn't final yet" in p.inner_text("[data-testid=pending-recalculation]"),
        "pending banner explains it is not final",
    )
    p.get_by_role("tab", name=re.compile("EUR")).click()
    p.wait_for_function("!document.querySelector('[data-testid=pending-recalculation]')")
    # faturat
    go(p, "#billing")
    p.wait_for_selector(f"text={IDS['invoice_a_number']}")
    check(
        IDS["invoice_b_number"] not in p.inner_text("main"),
        "only the tenant's own invoice is listed",
    )
    p.get_by_role("button", name=re.compile("Details of invoice")).click()
    p.wait_for_selector("text=Invoice details")
    p.wait_for_selector("td:has-text('Gold - monthly fee')")
    check(
        "Gold - monthly fee" in p.inner_text("main")
        and "1,200.00" in p.inner_text("main").replace(" ", ","),
        "invoice detail with lines and server totals",
    )
    with p.expect_download() as dl:
        p.get_by_role("button", name=re.compile("Download official PDF")).first.click()
    d = dl.value
    check(
        d.suggested_filename == f"invoice-{IDS['invoice_a_number']}.pdf",
        f"server filename: {d.suggested_filename}",
    )
    raw = open(d.path(), "rb").read()
    check(raw.startswith(b"%PDF-"), "downloaded file is a PDF")
    text = "\n".join(pg.extract_text() for pg in PdfReader(io.BytesIO(raw)).pages)
    check(
        IDS["invoice_a_number"] in text
        and "Müller & Söhne – Tiranë Sh.p.k." in text
        and "1,200.00" in text,
        "PDF has number, Unicode bill-to snapshot and the stored total",
    )
    check(
        any(r.headers.get("content-type") == "application/pdf" for r in pdfs),
        "HTTP response is application/pdf",
    )
    # cross-tenant: kërkesa të drejtpërdrejta me çelësin e A drejt faturës së B
    h = {"Authorization": f"Bearer {IDS['key_a']}"}
    for suffix in ("", "/pdf", "/html"):
        r = p.request.get(f"{BASE}/v1/billing/invoices/{IDS['invoice_b']}{suffix}", headers=h)
        check(r.status == 404, f"cross-tenant {suffix or 'detail'} is 404 (got {r.status})")
    r = p.request.get(f"{BASE}/v1/billing/invoices/{IDS['invoice_a']}/pdf", headers=h)
    check(r.status == 200 and r.body().startswith(b"%PDF-"), "own PDF over the API")
    r = p.request.get(f"{BASE}/v1/billing/invoices/{IDS['invoice_a']}/pdf")
    check(r.status in (401, 403), f"no key ⇒ {r.status}")
    # fushat e brendshme nuk ekspozohen
    inv = p.request.get(f"{BASE}/v1/billing/invoices/{IDS['invoice_a']}", headers=h).json()
    check(
        set(inv)
        <= {
            "id",
            "number",
            "status",
            "currency",
            "subtotal",
            "vat_rate",
            "tax",
            "total",
            "issued_at",
            "due_at",
            "paid_at",
            "paid_via",
            "period_start",
            "period_end",
            "lines",
        },
        f"no internal fields: {sorted(inv)}",
    )
    # mobile
    p.set_viewport_size({"width": 390, "height": 800})
    for hh in ("#wallet", "#billing"):
        go(p, hh)
        p.wait_for_timeout(700)
        check(
            not p.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"),
            f"no horizontal scroll at 390px on {hh}",
        )
    check(seen == [], f"no mutation requests: {seen}")
    ctx.close()


if __name__ == "__main__":
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=CHROME)
        main(b)
        b.close()
    print(f"{passed} checks passed")
    print("console/page errors:", errors or "none")
    raise SystemExit(1 if errors else 0)
