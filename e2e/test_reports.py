"""E2E i raporteve dhe regjistrit të aktivitetit të klientit Enterprise (M11-G) me Chromium të vërtetë.

NUK dërgon SMS të vërtetë: API-ja E2E nuk ka punonjës dërgimi dhe asnjë kredencial ofruesi; të gjitha rrugët janë `fake`.

    E2E_BASE=http://127.0.0.1:5173 E2E_IDS=ids.json E2E_DB=ent.db python e2e/test_reports.py
"""

import csv
import io
import json
import os
import re
import sqlite3
import subprocess

from playwright.sync_api import sync_playwright

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:5173")
CHROME = os.environ.get("E2E_CHROME", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
IDS = json.load(open(os.environ["E2E_IDS"]))
DB = os.environ["E2E_DB"]
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


def csv_of(p, button):
    with p.expect_download() as d:
        button.click()
    raw = open(d.value.path(), "rb").read()
    check(raw.startswith(b"\xef\xbb\xbf"), "CSV starts with the UTF-8 BOM")
    return list(csv.reader(io.StringIO(raw.decode("utf-8").lstrip("﻿"))))


def login(b, key, width=1360):
    ctx = b.new_context(viewport={"width": width, "height": 900}, accept_downloads=True)
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
    p.goto(BASE)
    p.evaluate("sessionStorage.clear()")
    p.reload()
    p.fill("input[type=password]", key)
    p.click("button:has-text('Sign in')")
    p.wait_for_selector("nav")
    return ctx, p


def main(b):
    secrets = [k for k in os.environ if re.search(r"TWILIO|VONAGE|INFOBIP|SMPP", k, re.I)]
    check(not secrets, f"no provider credentials in the environment: {secrets}")
    ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    check(not re.search(r"python\S*\s+-m\s+app\.worker", ps), "no dispatch worker is running")
    con = sqlite3.connect(DB)
    check(
        {r[0] for r in con.execute("select distinct provider from sms_routes")} == {"fake"},
        "the only route provider is the in-process fake",
    )
    db_status = dict(
        con.execute(
            "select status, count(*) from sms_messages where owner_ref='e2e-m' group by status"
        ).fetchall()
    )
    total = sum(db_status.values())
    failed = db_status.get("FAILED", 0)
    unknown = db_status.get("UNKNOWN", 0)
    check(unknown >= 1 and failed >= 1, f"seed has failed and UNKNOWN messages: {db_status}")

    ctx, p = login(b, IDS["key_m"])
    writes = []
    p.on(
        "request",
        lambda r: (
            writes.append(f"{r.method} {r.url.split('?')[0].replace(BASE, '')}")
            if r.method not in ("GET", "HEAD")
            else None
        ),
    )
    # ---- navigim ----
    nav = p.locator("nav").inner_text()
    check("Reports" in nav and "Activity log" in nav, "Reports and Activity log are in the menu")
    go(p, "#reports")
    p.wait_for_selector("text=SMS submitted")
    body = p.inner_text("main")
    check(str(total) in body, f"overview total comes from the server ({total})")
    check("unknown" not in body.lower(), "UNKNOWN never appears on the overview")
    still = p.locator(".stat:has-text('Still sending')").inner_text()
    inflight = sum(db_status.get(k, 0) for k in ("QUEUED", "SENDING", "SENT", "UNKNOWN"))
    check(f"\n{inflight}\n" in still, f"UNKNOWN counts as still sending ({inflight}): {still!r}")
    check("Days are UTC" in body, "the UTC day semantics are stated")
    # ---- data ----
    p.get_by_label("To").fill("2020-01-01")
    p.wait_for_selector("text=The end date must not be before the start date.")
    check("to=2020-01-01" not in p.evaluate("location.hash"), "invalid range never reaches the URL")
    p.get_by_label("To").fill("2030-01-01")
    p.wait_for_selector("text=The range is limited to 366 days.")
    p.get_by_role("button", name="Last 7 days").click()
    p.wait_for_function("location.hash.includes('from=')")
    p.wait_for_selector("text=SMS submitted")
    check(str(total) in p.inner_text("main"), "a preset range keeps today's data")
    # ---- campaign report ----
    p.get_by_role("tab", name="Campaigns").click()
    p.wait_for_selector("main tbody tr")
    check(rows(p) == 25, f"first report page has 25 campaigns (got {rows(p)})")
    p.get_by_label("Search").fill("Autumn")
    p.wait_for_function("document.querySelectorAll('main tbody tr').length === 1")
    tr = p.locator("main tbody tr").inner_text()
    check("Autumn sale" in tr and "%" in tr, f"delivery rate shown from the server: {tr!r}")
    p.get_by_role("button", name="Clear filters").click()
    p.wait_for_function("document.querySelectorAll('main tbody tr').length === 25")
    p.get_by_label("Filter by status").select_option("completed")
    p.wait_for_function("document.querySelectorAll('main tbody tr').length === 1")
    check("status=completed" in p.evaluate("location.hash"), "status filter is in the URL")
    p.get_by_label("Filter by status").select_option("")
    p.wait_for_function("document.querySelectorAll('main tbody tr').length === 25")
    p.get_by_role("button", name="Older").click()
    p.wait_for_function("document.querySelectorAll('main tbody tr').length < 25")
    check(rows(p) >= 1, "second page of campaigns")
    p.get_by_role("button", name="Newest").click()
    p.wait_for_function("document.querySelectorAll('main tbody tr').length === 25")
    # CSV i campaign-eve: UTF-8 + formula e neutralizuar
    data = csv_of(p, p.get_by_role("button", name="Export CSV"))
    names = [r[1] for r in data[1:]]
    check(any(n.startswith("'=cmd") for n in names), "formula campaign name is neutralized in CSV")
    check(not any(n[:1] in "=+-@" for n in names), "no CSV cell starts with a formula character")
    check(data[0][0] == "id" and len(data) - 1 > 25, f"all campaigns exported ({len(data) - 1})")
    # ---- dështimet ----
    p.get_by_role("tab", name="Delivery & failures").click()
    p.wait_for_selector("text=By error code")
    body = p.inner_text("main")
    check(
        "twilio_30003" in body and "Rejected by the provider" in body, "provider failure category"
    )
    check("weird_x9" in body, "unknown error code stays visible")
    check(
        "unknown" not in body.lower().replace("unknown error", ""), "UNKNOWN is not shown as failed"
    )
    failed_card = p.locator(".stat:has-text('Failed')").first.inner_text()
    check(str(failed) in failed_card, f"failed count equals the database ({failed})")
    p.get_by_label("Campaign number").fill(str(IDS["other_campaign"]))
    p.get_by_label("Campaign number").press("Enter")
    p.get_by_text(re.compile("find that")).first.wait_for()
    check("Traceback" not in p.inner_text("main"), "another tenant's campaign is a plain not-found")
    p.get_by_label("Campaign number").fill("")
    p.get_by_label("Campaign number").press("Enter")
    # ---- përdorimi ----
    go(p, "#reports?tab=usage")
    p.wait_for_selector("text=SMS cost (delivered)")
    check(p.get_by_role("img").count() >= 1, "usage chart has an accessible description")
    check(p.locator("table").count() >= 1, "usage has a text table equivalent")
    msg_rows = csv_of(p, p.get_by_role("button", name="Export SMS (CSV)"))
    check(
        {r[4] for r in msg_rows[1:]} >= {"delivered", "failed", "sending"},
        "messages CSV uses public statuses",
    )
    check("unknown" not in {r[4] for r in msg_rows[1:]}, "messages CSV never contains UNKNOWN")
    check(
        len(msg_rows) - 1 == total,
        f"messages CSV has every message ({len(msg_rows) - 1} vs {total})",
    )
    # ---- aktiviteti ----
    go(p, "#activity")
    p.wait_for_selector("text=Activity log")
    p.wait_for_selector("main tbody tr")
    body = p.inner_text("main")
    check("Campaign paused" in body and "wallet.adjust" in body, "own audit rows listed")
    check(
        "campaign.cancel" not in body and "Campaign cancelled" not in body,
        "other tenant's audit is not listed",
    )
    check(
        "legacy.row" not in body and "cross_tenant" not in body,
        "legacy/global and staff-access rows are hidden",
    )
    check(
        "SECRET-TOKEN" not in body and "secret.example" not in body and "staff-person" not in body,
        "no secrets or staff identity",
    )
    check("you" in body.lower() and "our staff" in body.lower(), "safe actor labels")
    p.get_by_label("Filter by action").select_option("campaign.pause")
    p.wait_for_function("document.querySelectorAll('main tbody tr').length === 1")
    check("action=campaign.pause" in p.evaluate("location.hash"), "action filter in the URL")
    au = csv_of(p, p.get_by_role("button", name="Export CSV"))
    check({r[3] for r in au[1:]} == {"campaign.pause"}, "audit CSV carries the filter")
    check(
        "SECRET-TOKEN" not in json.dumps(au) and "staff-person" not in json.dumps(au),
        "audit CSV has no secrets",
    )
    # ---- responsive ----
    for h in (
        "#reports",
        "#reports?tab=campaigns",
        "#reports?tab=failures",
        "#reports?tab=usage",
        "#activity",
    ):
        ctx2, q = login(b, IDS["key_m"], width=390)
        go(q, h)
        q.wait_for_selector("main")
        q.wait_for_timeout(500)
        wide = q.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
        )
        check(not wide, f"no horizontal scroll at 390px on {h}")
        ctx2.close()
    # ---- vetëm lexim ----
    check(not writes, f"reports and activity made no write requests: {writes}")
    ctx.close()
    # ---- tenant tjetër ----
    ctx3, x = login(b, IDS["key_x"])
    go(x, "#reports?tab=campaigns")
    x.wait_for_selector("text=Campaign performance")
    x.wait_for_timeout(600)
    check("Autumn sale" not in x.inner_text("main"), "tenant B does not see tenant A's campaigns")
    go(x, "#activity")
    x.wait_for_timeout(800)
    xb = x.inner_text("main")
    check(
        "Campaign paused" not in xb and "wallet.adjust" not in xb,
        "tenant B does not see tenant A's audit",
    )
    with x.expect_download() as d:
        x.get_by_role("button", name="Export CSV").click()
    xr = open(d.value.path(), "rb").read().decode("utf-8")
    check(
        "campaign.pause" not in xr and "wallet.adjust" not in xr,
        "tenant B audit export has no tenant A rows",
    )
    ctx3.close()
    n_prov = con.execute(
        "select count(*) from sms_messages where provider not in ('fake')"
    ).fetchone()[0]
    check(n_prov == 0, "no message was ever routed to a real provider")


if __name__ == "__main__":
    with sync_playwright() as pw:
        br = pw.chromium.launch(executable_path=CHROME)
        main(br)
        br.close()
    print(f"{passed} checks passed")
    print("console/page errors:", errors or "none")
    raise SystemExit(1 if errors else 0)
