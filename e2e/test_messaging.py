"""E2E i punës ditore të klientit Enterprise: kontakte, lista, campaign-e, trafik (M11-F) me Chromium të vërtetë.

NUK dërgon SMS të vërtetë: API-ja E2E nuk ka punonjës dërgimi dhe asnjë kredencial ofruesi; seed-i përdor FakeProvider brenda procesit.

    E2E_BASE=http://127.0.0.1:5173 E2E_IDS=ids.json E2E_DB=ent.db python e2e/test_messaging.py
"""

import json
import os
import re
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta

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


def main(b):
    # --- provë se s'ka SMS të vërtetë ---
    secrets = [k for k in os.environ if re.search(r"TWILIO|VONAGE|INFOBIP|SMPP", k, re.I)]
    check(not secrets, f"no provider credentials in the environment: {secrets}")
    ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
    check(not re.search(r"python\S*\s+-m\s+app\.worker", ps), "no dispatch worker is running")
    con = sqlite3.connect(DB)
    provs = {r[0] for r in con.execute("select distinct provider from sms_routes")}
    check(provs == {"fake"}, f"the only configured route provider is the in-process fake: {provs}")
    con.close()

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
    writes = []
    p.on(
        "request",
        lambda r: (
            writes.append(f"{r.method} {r.url.split('?')[0].replace(BASE, '')}")
            if r.method not in ("GET", "HEAD")
            else None
        ),
    )
    p.goto(BASE)
    p.evaluate("sessionStorage.clear()")
    p.reload()
    p.fill("input[type=password]", IDS["key_m"])
    p.click("button:has-text('Sign in')")
    p.wait_for_selector("nav")

    # ---------------- kontaktet ----------------
    go(p, "#contacts")
    p.wait_for_selector("text=Person01")
    check(rows(p) == 30, f"30 people listed (got {rows(p)})")
    p.get_by_label("Search people").fill("Person05")
    p.wait_for_function("document.querySelectorAll('main tbody tr').length === 1")
    check("q=Person05" in p.evaluate("location.hash"), "search is kept in the URL")
    p.reload()
    p.wait_for_function("document.querySelectorAll('main tbody tr').length === 1")
    check(
        p.get_by_label("Search people").input_value() == "Person05",
        "the filtered view survives a reload (deep link)",
    )
    p.get_by_role("button", name="Clear filters").click()
    p.wait_for_function("document.querySelectorAll('main tbody tr').length === 30")
    p.get_by_label("Filter by list").select_option(label="In list: VIP")
    p.wait_for_function("document.querySelectorAll('main tbody tr').length === 28")
    # listat: numërim nga serveri
    go(p, "#contacts?tab=lists")
    p.wait_for_selector("td:has-text('VIP')")
    vip = p.locator("tbody tr:has-text('VIP')").inner_text()
    check("28" in vip, f"list count comes from the server: {vip!r}")
    check("0" in p.locator("tbody tr:has-text('Empty')").inner_text(), "empty list shows 0")
    p.get_by_role("button", name="Open list VIP").click()
    p.wait_for_selector("text=28 people in this list")
    p.wait_for_selector("text=can receive SMS promotions")
    # importi: 1 rresht i keq
    go(p, "#contacts?tab=import")
    p.get_by_label("Or paste here").fill(
        "phone,first_name\n+355691239001,Imp1\n+355691239002,Imp2\nnot-a-number,Bad"
    )
    p.get_by_role("button", name=re.compile("Import 3 people")).click()
    p.wait_for_selector("text=Partly imported")
    check(
        "1" in p.locator(".alert").first.inner_text() and "rejected" in p.inner_text("main"),
        "import reports the rejected row from the server",
    )
    check(
        "not-a-number" not in p.locator("details").inner_text(),
        "raw CSV content is not echoed in the error list",
    )
    # kontakt i vetëm
    go(p, "#contacts?tab=add")
    p.get_by_label(re.compile("^Phone")).fill("+355 69 123 9100")
    p.get_by_role("button", name="Save person").click()
    p.wait_for_selector(".toast:has-text('Saved')")

    # ---------------- lista e campaign-eve ----------------
    go(p, "#campaigns")
    p.wait_for_selector("main tbody tr")
    check(rows(p) == 25, f"first page has 25 campaigns (got {rows(p)})")
    p.get_by_role("button", name="Older").click()
    p.wait_for_function("document.querySelectorAll('main tbody tr').length === 4")
    check("before=" in p.evaluate("location.hash"), "keyset position is kept in the URL")
    done = p.locator("tbody tr:has-text('Autumn sale')").inner_text()
    check(
        "28" in done and "completed" in done,
        f"recipients count and status come from the server: {done!r}",
    )
    go(p, "#campaigns?status=draft")
    p.wait_for_function("document.querySelectorAll('main tbody tr').length === 25")
    go(p, "#campaigns?status=scheduled")
    p.wait_for_selector("td:has-text('Scheduled promo')")
    go(p, "#campaigns?q=autumn")
    p.wait_for_function("document.querySelectorAll('main tbody tr').length === 1")

    # ---------------- krijimi me planifikim ----------------
    go(p, "#campaigns/new")
    p.get_by_label(re.compile("^Name")).fill("E2E promo")
    p.get_by_role("button", name="Next").click()
    p.get_by_label(re.compile("^Who receives it")).select_option(label="VIP (28 people)")
    p.wait_for_selector("text=can be contacted")
    p.get_by_role("button", name="Next").click()
    p.get_by_role("textbox", name=re.compile("Message")).fill("Hello from the E2E run")
    p.wait_for_selector("text=estimate; the server counts")
    p.get_by_role("button", name="Next").click()
    later = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%d")
    p.get_by_label("Schedule for later").check()
    p.get_by_label("Time zone").select_option("utc")
    p.get_by_label("Date", exact=True).fill(later)
    p.get_by_label("Time", exact=True).fill("09:30")
    p.wait_for_selector(f"text={later} 09:30 UTC")
    p.get_by_role("button", name="Next").click()
    review = (
        p.get_by_label("Review", exact=True).inner_text()
        if p.get_by_label("Review", exact=True).count()
        else p.inner_text("main")
    )
    check(
        "E2E promo" in review
        and "ACME" in review
        and "VIP" in review
        and f"{later} 09:30 UTC" in review,
        "review shows the exact UTC instant",
    )
    p.set_viewport_size({"width": 390, "height": 800})
    check(
        not p.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"),
        "wizard review fits 390 px",
    )
    p.set_viewport_size({"width": 1360, "height": 900})
    p.get_by_role("button", name="Schedule", exact=True).click()
    p.wait_for_selector("text=E2E promo")
    p.wait_for_function("location.hash.startsWith('#campaigns/') && !location.hash.includes('new')")
    p.wait_for_selector("text=scheduled")
    check(f"{later} 09:30 UTC" in p.inner_text("main"), "detail shows the scheduled instant in UTC")
    p.get_by_role("tab", name="Activity").click()
    p.wait_for_selector("text=Scheduled / started")
    check("Campaign created" in p.inner_text("main"), "activity lists the recorded events")

    # ---------------- detaji i campaign-it të përfunduar ----------------
    cid = IDS["campaign_done"]
    go(p, f"#campaigns/{cid}")
    p.wait_for_selector("text=Delivery summary")
    check(
        "Big sale for you!" in p.inner_text("main") and "VIP" in p.inner_text("main"),
        "overview shows the message and the list",
    )
    go(p, f"#campaigns/{cid}?tab=recipients")
    p.wait_for_selector("td:has-text('Person01')")
    check(rows(p) == 25, "recipients page is server-paginated (25)")
    p.get_by_role("button", name="Next").click()
    p.wait_for_function("document.querySelectorAll('main tbody tr').length === 3")
    go(p, f"#campaigns/{cid}?tab=traffic")
    p.wait_for_selector("td:has-text('delivered')")
    txt = p.inner_text("main")
    check(
        "Rejected by the provider" in txt or "Not delivered" in txt or "Failed" in txt,
        "a failed message shows a failure category",
    )
    unk = p.locator("tbody tr:has-text('sending')")
    check(
        unk.count() >= 1 and "failed" not in unk.first.inner_text().lower(),
        "the UNKNOWN provider state is shown as sending, never failed",
    )
    p.locator("tbody tr:has-text('delivered')").first.click()
    p.wait_for_selector("text=What happened")
    go(p, f"#messages?campaign={cid}&status=failed")
    p.wait_for_function("document.querySelectorAll('main tbody tr').length >= 1")
    p.get_by_label("From date").fill("2030-01-02")
    p.get_by_label("To date").fill("2030-01-01")
    p.wait_for_selector("text=must not be before the start date")

    # ---------------- tenant tjetër ----------------
    h = {"Authorization": f"Bearer {IDS['key_m']}"}
    for path in (
        f"/v1/campaigns/{IDS['other_campaign']}",
        f"/v1/campaigns/{IDS['other_campaign']}/recipients",
        f"/v1/campaigns/{IDS['other_campaign']}/activity",
        f"/v1/contacts/{IDS['other_contact']}",
    ):
        check(
            p.request.get(f"{BASE}{path}", headers=h).status == 404, f"cross-tenant {path} is 404"
        )
    check(
        p.request.get(f"{BASE}/v1/messages?campaign_id={IDS['other_campaign']}", headers=h).json()[
            "items"
        ]
        == [],
        "cross-tenant campaign filter on messages is empty",
    )

    # ---------------- mobile ----------------
    p.set_viewport_size({"width": 390, "height": 800})
    for hh in (
        "#contacts",
        "#contacts?tab=lists",
        "#campaigns",
        f"#campaigns/{cid}",
        f"#campaigns/{cid}?tab=traffic",
        "#campaigns/new",
        "#messages",
    ):
        go(p, hh)
        p.wait_for_timeout(700)
        check(
            not p.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"),
            f"no horizontal scroll at 390px on {hh}",
        )
    # asnjë kërkesë e padëshiruar: vetëm veprimet e skenarit
    allowed = {
        "POST /v1/contacts/import",
        "POST /v1/contacts",
        "POST /v1/campaigns",
        re.compile(r"POST /v1/campaigns/\d+/schedule"),
    }
    bad = [
        w
        for w in writes
        if not any((a.match(w) if hasattr(a, "match") else a == w) for a in allowed)
    ]
    check(not bad, f"only the scenario's own writes happened: {bad}")
    con = sqlite3.connect(DB)
    n_prov = con.execute(
        "select count(*) from sms_messages where provider not in ('fake')"
    ).fetchone()[0]
    check(n_prov == 0, "no message was ever routed to a real provider")
    ctx.close()


if __name__ == "__main__":
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=CHROME)
        main(b)
        b.close()
    print(f"{passed} checks passed")
    print("console/page errors:", errors or "none")
    raise SystemExit(1 if errors else 0)
