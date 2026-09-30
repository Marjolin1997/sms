"""Test end-to-end i konsolës me Chromium të vërtetë.

Kërkon një stack që punon (API + frontend) me të dhënat demo:
    python -m scripts.seed_demo         # printon CLIENT_KEY dhe ADMIN_KEY
    E2E_CLIENT_KEY=... E2E_ADMIN_KEY=... E2E_BASE=http://127.0.0.1:5173 python e2e/test_console.py
Nuk ekzekutohet nga pytest/CI; është kontroll dore për ndërfaqen.
"""

import os
import re
import sys
import time

from playwright.sync_api import Page, sync_playwright

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:5173")
CHROME = os.environ.get("E2E_CHROME", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
SHOTS = os.environ.get("E2E_SHOTS", "")
CLIENT, ADMIN = os.environ["E2E_CLIENT_KEY"], os.environ["E2E_ADMIN_KEY"]
UNIQ = str(int(time.time()))[-6:]  # numër unik për çdo ekzekutim
NUMBER = f"+35569123{UNIQ}"
errors: list[str] = []
passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise AssertionError(msg)
    passed += 1


def has(needle, hay):
    """Krahasim pa dallim shkronjash (CSS text-transform ndryshon tekstin e dukshëm)."""
    return needle.lower() in hay.lower()


def login(page: Page, key: str):
    page.goto(BASE)
    page.evaluate("sessionStorage.clear()")
    page.reload()
    page.fill("input[type=password]", key)
    page.click("button:has-text('Sign in')")
    page.wait_for_selector("nav")


def go(page: Page, hash_: str, wait=700):
    page.evaluate(f"location.hash = '{hash_}'")
    page.wait_for_timeout(wait)


def shot(page, name):
    if SHOTS:
        page.screenshot(path=f"{SHOTS}/{name}.png", full_page=True)


def toast(page, text):
    page.wait_for_selector(f".toast:has-text('{text}')", timeout=6000)


def new_ctx(b, viewport, lang="en", **kw):
    """Konsola hapet shqip; skenarët e vjetër ekzekutohen në anglisht (localStorage)."""
    ctx = b.new_context(viewport=viewport, **kw)
    if lang:
        ctx.add_init_script(f"try{{localStorage.setItem('sms_lang','{lang}')}}catch(e){{}}")
    return ctx


def albanian_flow(b):
    ctx = new_ctx(b, {"width": 1360, "height": 900}, lang=None)  # pa preferencë: shqip
    p = ctx.new_page()
    p.on("pageerror", lambda e: errors.append(f"sq pageerror: {e}"))
    p.goto(BASE)
    p.wait_for_selector("input[type=password]")
    check(
        has("Hyni", p.inner_text("body")) or has("Kyçu", p.inner_text("body")),
        "login page in Albanian",
    )
    p.fill("input[type=password]", "sms_bad_key")
    p.click("button.primary")
    p.wait_for_selector(".alert.bad")
    check(has("nuk është i vlefshëm", p.inner_text(".alert.bad")), "invalid key error in Albanian")
    p.fill("input[type=password]", CLIENT)
    p.click("button.primary")
    p.wait_for_selector("nav")
    nav = p.inner_text("nav")
    check(all(x in nav for x in ["Portofoli", "Fushatat", "Kontaktet"]), "client menu in Albanian")
    for page_hash in [
        "messages",
        "campaigns",
        "contacts",
        "senders",
        "email",
        "webhooks",
        "wallet",
        "billing",
        "keys",
    ]:
        go(p, page_hash, 600)
        body = p.inner_text("main")
        check(
            not re.search(r"\b(Your|Nothing|Create|Sender IDs)\b", body),
            f"{page_hash}: no English left",
        )
    shot(p, "u13-sq-wallet")
    p.click(".side-f .langsw button:has-text('English')")
    p.wait_for_timeout(500)
    check("Wallet" in p.inner_text("nav"), "language switch to English works")
    p.click(".side-f .langsw button:has-text('Shqip')")
    p.wait_for_timeout(500)
    check("Portofoli" in p.inner_text("nav"), "language switch back to Albanian works")
    ctx.close()


def client_flow(b):
    ctx = new_ctx(b, {"width": 1360, "height": 900})
    p = ctx.new_page()
    p.on("pageerror", lambda e: errors.append(f"client pageerror: {e}"))
    p.on("console", lambda m: m.type == "error" and errors.append(f"client console: {m.text}"))
    # ---- hyrje e gabuar, pastaj e saktë
    p.goto(BASE)
    p.fill("input[type=password]", "sms_bad_key")
    p.click("button:has-text('Sign in')")
    p.wait_for_selector(".alert.bad")
    check(has("isn't valid", p.inner_text(".alert.bad")), "invalid key gives a friendly message")
    login(p, CLIENT)
    nav = p.inner_text("nav")
    check("Staff" not in nav and "Approvals" not in nav, "client does not see staff menu")
    check(
        all(
            x in nav
            for x in ["Send", "Message history", "Campaigns", "Contacts", "Wallet", "Billing"]
        ),
        "client menu",
    )

    # ---- overview + checklist
    go(p, "dashboard", 1500)
    check(has("Balance EUR", p.inner_text("main")), "balance card")
    check(p.locator(".checklist .step").count() == 0, "fully set-up account hides the checklist")
    shot(p, "u01-overview")

    # ---- send SMS: çmim live, numërues, validim
    go(p, "send", 1200)
    p.fill("input[placeholder='+355691234567']", "0691234567")
    check(
        has("international format", p.inner_text("main"))
        or has("Start with +", p.inner_text("main")),
        "phone validation message",
    )
    check(
        p.locator("button:has-text('Send SMS')").is_disabled(), "send disabled for invalid number"
    )
    p.fill("input[placeholder='+355691234567']", NUMBER)
    p.fill("textarea", "Hello from the console e2e test")
    p.wait_for_selector(".quote:has-text('Cost')", timeout=6000)
    check(has("0.045", p.inner_text(".quote")), "live price shown (0.045 per part)")
    check(
        has("31 characters", p.inner_text("main")) and has("1 part", p.inner_text("main")),
        "character counter",
    )
    p.fill("textarea", "ç" * 71)
    p.wait_for_selector(".quote:has-text('2 part')", timeout=6000)
    check(has("Unicode", p.inner_text("main")), "unicode detected")
    p.fill("textarea", "Hello from the console e2e test")
    p.click("button:has-text('Send SMS')")
    toast(p, "Message accepted")
    p.wait_for_selector("h3:has-text('Sent')")
    shot(p, "u02-send")

    # ---- historia
    go(p, "messages", 1200)
    check(has(UNIQ, p.inner_text("main")), "new message appears in history")
    p.fill("input[aria-label='Search']", UNIQ)
    p.wait_for_timeout(900)
    check(p.locator("tbody tr").count() == 1, "search narrows history")
    p.locator("tbody tr").first.click()
    p.wait_for_selector("h3:has-text('What happened')")
    p.fill("input[aria-label='Search']", "999999999")
    p.wait_for_timeout(900)
    check(has("No matches", p.inner_text("main")), "empty search state")
    shot(p, "u03-history")

    # ---- kontakte: import CSV, kërkim, lista
    go(p, "contacts", 900)
    p.click("[role=tab]:has-text('Import')")
    p.fill(
        "textarea",
        "phone,email,first_name,last_name\n+355691119001,zana@example.com,Zana,Krasniqi\n+355691119002,,Ilir,Berisha\nnot-a-number,,Bad,Row",
    )
    check(
        has("3 people found", p.inner_text("main")) or has("2 people found", p.inner_text("main")),
        "import preview counts",
    )
    p.click("button.primary:has-text('Import')")
    p.wait_for_selector(".alert.good:has-text('added')", timeout=6000)
    check(has("rejected", p.inner_text("main")), "invalid row reported")
    p.click("[role=tab]:has-text('People')")
    p.fill("input[aria-label='Search people']", "zana")
    p.wait_for_timeout(1000)
    check(
        p.locator("tbody tr").count() == 1 and has("Zana", p.inner_text("tbody")), "contact search"
    )
    shot(p, "u04-contacts")

    # ---- sender ID + template
    go(p, "senders", 900)
    p.fill("input[placeholder='ACME']", "X")
    check(
        has("3–11", p.inner_text("main")) or has("3-11", p.inner_text("main")),
        "sender validation hint",
    )
    p.fill("input[placeholder='ACME']", f"NB{UNIQ}")
    p.click("button:has-text('Submit for approval')")
    toast(p, "Request sent")
    check(has(f"NB{UNIQ}", p.inner_text("main")), "sender listed as pending")
    p.fill("input[placeholder='ACME']", f"OK{UNIQ}")
    p.click("button:has-text('Submit for approval')")
    p.wait_for_selector(f"tbody tr:has-text('OK{UNIQ}')")
    p.click("[role=tab]:has-text('Templates')")
    p.fill("input[placeholder='Login code']", f"Welcome {UNIQ}")
    p.fill("textarea", "Hi {{first_name}}, welcome!")
    check(has("first_name", p.inner_text("main")), "template variables detected")
    p.click("button:has-text('Submit for approval')")
    toast(p, "Sent for review")
    shot(p, "u05-senders")

    # ---- wallet, billing, webhooks, email, keys, campaigns
    for name, expect in [
        ("wallet", "Available to spend"),
        ("billing", "Your plan"),
        ("webhooks", "Add an endpoint"),
        ("email", "Add a sending domain"),
        ("keys", "Create an API key"),
        ("campaigns", "Your campaigns"),
    ]:
        go(p, name, 1300)
        check(has(expect, p.inner_text("main")), f"{name} page shows '{expect}'")
        shot(p, f"u06-{name}")
    go(p, "keys", 700)
    p.fill("input[placeholder='Production server']", f"e2e key {UNIQ}")
    p.click("button:has-text('Create key')")
    p.wait_for_selector(".secret code", timeout=6000)
    check(p.inner_text(".secret code").startswith("sms_"), "new key shown once")
    p.click('button:has-text("I\'ve saved it")')
    check(p.locator(".secret").count() == 0, "secret banner dismissed")
    # konfirmim revokimi: anulo
    p.locator(f"tbody tr:has-text('e2e key {UNIQ}') button:has-text('Revoke')").click()
    p.wait_for_selector("[role=dialog]")
    p.keyboard.press("Escape")
    check(p.locator("[role=dialog]").count() == 0, "dialog closes with Escape")
    # rrotullim: çelës i ri shfaqet një herë, i vjetri mbetet aktiv gjatë periudhës kalimtare
    p.locator(f"tbody tr:has-text('e2e key {UNIQ}') button:has-text('Rotate')").click()
    p.wait_for_selector("[role=dialog]")
    p.click("[role=dialog] button.primary")
    p.wait_for_selector(".secret code", timeout=6000)
    check(p.inner_text(".secret code").startswith("sms_"), "rotated key shown once")
    p.click('button:has-text("I\'ve saved it")')
    # eksport GDPR nga Contacts
    go(p, "contacts", 900)
    with p.expect_download() as dl:
        p.locator("tbody tr").first.locator("button:has-text('Export data')").click()
    check(dl.value.suggested_filename.startswith("contact-"), "contact export downloads a file")
    ctx.close()


def twofactor_flow(b):
    """Çelës i ri stafi: aktivizon 2FA (faqja Security); veprimi i ndjeshëm kërkon kod."""
    from app.core import totp

    ctx = new_ctx(b, {"width": 1360, "height": 900})
    r = ctx.request.post(
        f"{BASE}/v1/admin/api-keys",
        headers={"Authorization": f"Bearer {ADMIN}"},
        data={"name": f"e2e 2fa {UNIQ}", "role": "superadmin"},
    )
    check(r.status == 201, "staff key for 2FA test created")
    p = ctx.new_page()
    p.on("pageerror", lambda e: errors.append(f"2fa pageerror: {e}"))
    login(p, r.json()["key"])
    go(p, "security", 800)
    p.click("button:has-text('Set up two-factor')")
    p.wait_for_selector("code.wrap-code")
    secret = p.inner_text("code.wrap-code").strip()
    now_step = int(time.time() // totp.STEP)
    p.fill("input[placeholder='123456']", totp._code(secret, now_step))
    p.click("button:has-text('Confirm')")
    toast(p, "Two-factor is on")
    p.wait_for_timeout(1800)  # faqja rifreskohet
    go(p, "keys", 900)
    p.fill("input[placeholder='Production server']", f"2fa key {UNIQ}")
    p.select_option("select", "support")  # staf: një çelës klienti do të kërkonte llogari
    p.click("button:has-text('Create key')")
    p.wait_for_selector("[role=dialog]")
    check(has("Two-factor code", p.inner_text("[role=dialog]")), "sensitive action asks for a code")
    p.fill("[role=dialog] input", totp._code(secret, now_step + 1))
    p.click("[role=dialog] button.primary")
    p.wait_for_selector(".secret code", timeout=6000)
    check(True, "action succeeds with the code")
    ctx.close()


def new_client_checklist(b):
    """Llogari e re (globex): checklist i plotë me hapa të pa-bërë."""
    ctx = new_ctx(b, {"width": 1360, "height": 900})
    r = ctx.request.post(
        f"{BASE}/v1/admin/api-keys",
        headers={"Authorization": f"Bearer {ADMIN}"},
        data={"name": "e2e globex", "role": "client", "owner_ref": "globex"},
    )
    check(r.status == 201, "staff can create a client key")
    p = ctx.new_page()
    p.on("pageerror", lambda e: errors.append(f"globex pageerror: {e}"))
    login(p, r.json()["key"])
    go(p, "dashboard", 1500)
    check(p.locator(".checklist .step").count() >= 5, "new account sees the onboarding checklist")
    check(has("Get started", p.inner_text("main")), "checklist headline")
    go(p, "send", 1200)
    check(
        has("approved sender ID", p.inner_text("main")) or has("sender ID", p.inner_text("main")),
        "no-sender guidance instead of a broken form",
    )
    shot(p, "u13-new-account")
    ctx.close()


def staff_flow(b):
    ctx = new_ctx(b, {"width": 1360, "height": 900})
    p = ctx.new_page()
    p.on("pageerror", lambda e: errors.append(f"staff pageerror: {e}"))
    p.on("console", lambda m: m.type == "error" and errors.append(f"staff console: {m.text}"))
    login(p, ADMIN)
    check(
        has("Approvals", p.inner_text("nav")) and has("Finance", p.inner_text("nav")),
        "staff menu present",
    )
    go(p, "messages", 500)
    check(has("Choose an account", p.inner_text("main")), "account-required message for staff")
    # miratim sender ID i sapo-kërkuar nga klienti
    go(p, "approvals", 1500)
    check(has(f"NB{UNIQ}", p.inner_text("main")), "pending sender IDs listed")
    shot(p, "u07-approvals")
    row = p.locator(f"tbody tr:has-text('NB{UNIQ}')")
    row.locator("button:has-text('Reject')").click()
    p.wait_for_selector("[role=dialog]")
    check(
        p.locator("[role=dialog] button:has-text('Reject')").is_disabled(),
        "reason required before rejecting",
    )
    p.fill("[role=dialog] input", "Brand not verified")
    p.click("[role=dialog] button:has-text('Reject')")
    toast(p, "Decision saved")
    p.wait_for_timeout(600)
    p.locator(f"tbody tr:has-text('OK{UNIQ}')").locator("button:has-text('Approve')").click()
    toast(p, "Approved")
    p.click("[role=tab]:has-text('Templates')")
    p.wait_for_timeout(500)
    check(has("Porosia u nis", p.inner_text("main")), "pending template listed")
    # finance
    go(p, "finance", 1300)
    check(has("globex", p.inner_text("main")), "pending top-up listed")
    shot(p, "u08-finance")
    p.click("[role=tab]:has-text('Correction')")
    check(p.locator("button:has-text('Review correction')").is_disabled(), "correction needs input")
    # accounts + rates + admin
    go(p, "accounts", 1500)
    check(
        has("acme", p.inner_text("main")) and has("globex", p.inner_text("main")), "accounts listed"
    )
    shot(p, "u09-accounts")
    p.locator("tbody tr:has-text('globex')").click()
    p.wait_for_selector("h3:has-text('Sending')")
    p.click("button:has-text('Open as this account')")
    p.wait_for_timeout(1500)
    check(p.input_value("input.owner") == "globex", "impersonation picks the account")
    go(p, "rates", 1300)
    check(has("Price lists", p.inner_text("main")), "rates page")
    p.locator("tbody tr:has-text('acme-standard')").click()
    p.wait_for_selector("h3:has-text('Try a price')")
    p.fill("input[placeholder='+355691234567']", "+355691234567")
    p.click("button:has-text('Price it')")
    p.wait_for_selector(".quote:has-text('Total')", timeout=6000)
    shot(p, "u10-rates")
    p.click("[role=tab]:has-text('Routes')") if p.locator(
        "[role=tab]:has-text('Routes')"
    ).count() else None
    go(p, "admin", 1300)
    check(has("Kill switches", p.inner_text("main")), "admin page")
    p.locator("button:has-text('Pause')").first.click()
    p.wait_for_selector("[role=dialog]")
    check(
        p.locator("[role=dialog] button:has-text('Pause')").is_disabled(),
        "reason required to pause",
    )
    p.keyboard.press("Escape")
    p.click("[role=tab]:has-text('Audit log')")
    p.wait_for_timeout(800)
    check(
        has("sender.reject", p.inner_text("main")) or has("sender.approve", p.inner_text("main")),
        "audit shows the decisions just made",
    )
    shot(p, "u11-admin")
    ctx.close()


def mobile_flow(b):
    ctx = new_ctx(b, {"width": 390, "height": 800}, is_mobile=True)
    p = ctx.new_page()
    p.on("pageerror", lambda e: errors.append(f"mobile pageerror: {e}"))
    login(p, CLIENT)
    p.wait_for_timeout(1000)
    check(p.locator(".hamburger").is_visible(), "hamburger visible on mobile")
    p.click(".hamburger")
    p.wait_for_timeout(400)
    p.click("nav a:has-text('Message history')")
    p.wait_for_timeout(1200)
    check(
        p.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"),
        "no horizontal scroll on mobile",
    )
    shot(p, "u12-mobile")
    ctx.close()


with sync_playwright() as pw:
    b = pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
    t0 = time.time()
    try:
        albanian_flow(b)
        client_flow(b)
        twofactor_flow(b)
        new_client_checklist(b)
        staff_flow(b)
        mobile_flow(b)
    finally:
        b.close()
print(f"{passed} checks passed in {time.time() - t0:.1f}s")
errs = [e for e in errors if not re.search(r"favicon|status of 4(01|09|22|03)", e)]
print("console/page errors:", errs or "none")
sys.exit(1 if errs else 0)
