"""E2E i auditit të konsolës Central (M11-G) me Chromium të vërtetë: Sender decisions, Commercial audit, Operations state, Staff activity.

Kërkon stack-un e përkohshëm (shih e2e/central_seed.py + e2e/commercial_seed.py). Vetëm lexim; nuk dërgon SMS dhe nuk ka provider.

    E2E_BASE=http://127.0.0.1:5174 E2E_IDS=ids.json E2E_ADMIN_PW=... E2E_OP_PW=... python e2e/test_central_audit.py
"""

import csv
import io
import json
import os
import re

from playwright.sync_api import sync_playwright

from e2e.browser_util import dump_failure, launch

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:5174")
IDS = json.load(open(os.environ["E2E_IDS"]))
ADMIN_PW, OP_PW = os.environ["E2E_ADMIN_PW"], os.environ["E2E_OP_PW"]
errors: list[str] = []
passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise AssertionError(msg)
    passed += 1


def go(p, h):
    p.evaluate(f"location.hash = {json.dumps(h)}")


def rows(p):
    return p.locator("main tbody tr").count()


def session(b, email, pw, width=1360):
    ctx = b.new_context(viewport={"width": width, "height": 900}, accept_downloads=True)
    p = ctx.new_page()
    writes: list[str] = []
    p.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    p.on(
        "console",
        lambda m: (
            errors.append(f"console: {m.text}")
            if m.type == "error" and "Failed to load resource" not in m.text
            else None
        ),
    )
    p.on(
        "request",
        lambda r: (
            writes.append(f"{r.method} {r.url.split('?')[0].replace(BASE, '')}")
            if r.method not in ("GET", "HEAD") and "/auth/token" not in r.url
            else None
        ),
    )
    p.goto(BASE)
    p.evaluate("sessionStorage.clear(); localStorage.setItem('central_lang','en')")
    p.reload()
    p.fill("input[type=email]", email)
    p.fill("input[type=password]", pw)
    p.click("button:has-text('Sign in')")
    p.wait_for_selector("nav[aria-label='Main navigation']")
    return ctx, p, writes


def csv_of(p, button):
    with p.expect_download() as d:
        button.click()
    raw = open(d.value.path(), "rb").read()
    check(raw.startswith(b"\xef\xbb\xbf"), "CSV starts with the UTF-8 BOM")
    return list(csv.reader(io.StringIO(raw.decode("utf-8").lstrip("﻿"))))


def flow(b, email, pw, label):
    ctx, p, writes = session(b, email, pw)
    nav = p.locator("nav[aria-label='Main navigation']").inner_text()
    for item in ("Sender decisions", "Commercial audit", "Operations state", "Staff activity"):
        check(item in nav, f"{label}: '{item}' is in the Audit menu")
    # ---- vendimet për sender-at ----
    go(p, "#audit-senders")
    p.wait_for_selector("main tbody tr")
    body = p.inner_text("main")
    check(
        "ACMEPEND" in body and "AUTOOK" in body and "DENIED1" in body, f"{label}: decisions listed"
    )
    check(
        "system" in body.lower() and "sender policy" in body.lower(),
        f"{label}: policy decisions are labelled as system",
    )
    n_all = rows(p)
    p.get_by_label("Category").select_option("policy_auto_approved")
    p.wait_for_function(f"document.querySelectorAll('main tbody tr').length < {n_all}")
    check(
        "AUTOOK" in p.inner_text("main") and "ACMEPEND" not in p.inner_text("main"),
        f"{label}: category filter is applied by the server",
    )
    check(
        "category=policy_auto_approved" in p.evaluate("location.hash"),
        f"{label}: filter in the URL",
    )
    p.reload()
    p.wait_for_selector("main tbody tr")
    check(
        p.get_by_label("Category").input_value() == "policy_auto_approved",
        f"{label}: deep link survives reload",
    )
    data = csv_of(p, p.get_by_role("button", name="Export CSV"))
    check(
        data[0][0] == "decided_at_utc"
        and all(r[8] == "policy_auto_approved" for r in data[1:])
        and len(data) > 1,
        f"{label}: CSV carries the filter",
    )
    # data e pavlefshme
    go(p, "#audit-senders")
    p.wait_for_selector("main tbody tr")
    p.get_by_label("From").fill("2026-10-09")
    p.get_by_label("To").fill("2026-10-01")
    p.wait_for_selector("text=The end date must not be before the start date.")
    # ---- komercial ----
    go(p, "#audit-commercial")
    p.wait_for_selector("text=Commercial audit")
    p.wait_for_timeout(800)
    body = p.inner_text("main")
    check(
        "Payments" in body and "Invoices" in body, f"{label}: links to the payment/invoice records"
    )
    check(
        p.locator("select[aria-label='Area']").count() == 0,
        f"{label}: commercial audit scope is fixed",
    )
    # ---- operacione ----
    go(p, "#audit-ops")
    p.wait_for_selector("text=Operations: current state only")
    body = p.inner_text("main")
    check("keeps only the latest report" in body, f"{label}: states there is no history")
    check(p.locator("main svg, main canvas").count() == 0, f"{label}: no fake trend chart")
    # ---- aktiviteti i stafit ----
    go(p, "#audit-staff")
    p.wait_for_selector("main tbody tr")
    body = p.inner_text("main")
    check("admin@e2e.test" in body, f"{label}: staff identity shown per the auth model")
    p.get_by_label("Area").select_option("sender")
    p.wait_for_timeout(700)
    acts = [
        r.split("\t")[2] if "\t" in r else r for r in p.locator("main tbody tr").all_inner_texts()
    ]
    check(
        acts and all("sender" in a for a in acts),
        f"{label}: sender scope shows only sender actions: {acts[:3]}",
    )
    audit_csv = csv_of(p, p.get_by_role("button", name="Export CSV"))
    check(
        all(r[4].startswith("sender") for r in audit_csv[1:]),
        f"{label}: audit CSV carries the scope",
    )
    # ---- vetëm lexim ----
    for h in ("#audit-senders", "#audit-commercial", "#audit-ops", "#audit-staff"):
        go(p, h)
        p.wait_for_selector("main")
        p.wait_for_timeout(300)
        check(
            p.get_by_role(
                "button",
                name=re.compile("^(Approve|Reject|Revoke|Resubmit|Save|Delete|Create)", re.I),
            ).count()
            == 0,
            f"{label}: no mutation controls on {h}",
        )
    check(not writes, f"{label}: no write requests: {writes}")
    ctx.close()
    # ---- responsive ----
    ctx2, q, _ = session(b, email, pw, width=390)
    for h in ("#audit-senders", "#audit-commercial", "#audit-ops", "#audit-staff"):
        go(q, h)
        q.wait_for_selector("main")
        q.wait_for_timeout(500)
        wide = q.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
        )
        check(not wide, f"{label}: no horizontal scroll at 390px on {h}")
    ctx2.close()


def main(b):
    flow(b, "admin@e2e.test", ADMIN_PW, "admin")
    flow(b, "operator@e2e.test", OP_PW, "operator")


if __name__ == "__main__":
    with sync_playwright() as pw:
        br = launch(pw)
        try:
            main(br)
        except BaseException:
            dump_failure(br, "central-audit")
            raise
        finally:
            br.close()
    print(f"{passed} checks passed")
    print("console/page errors:", errors or "none")
    raise SystemExit(1 if errors else 0)
