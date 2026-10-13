"""M11-H · E2E në shfletues (Chromium) kundër stack-ut REAL: uvicorn Enterprise + Central, PostgreSQL, punonjës, Vite, FakeProvider.

    . $PILOT_DIR/pilot.env && python -m e2e.pilot.ui_pilot            (stack-i i përgatitur nga e2e/pilot/prepare.sh + flow_central)

Rrjedha klienti (tenant A, në UI): kërkesë sender → Central (UI admin) miraton → projeksion → kontakte → campaign (wizard) → dërgim FakeProvider → DLR → detaje,
marrës, trafik, raporte, aktivitet, faturim + PDF; tenant B në kontekst të veçantë. Skenarë dështimi në UI. Central: admin + operator. Responsive + a11y bazë.
Pa SMS të vërtetë. Asnjë gabim i papritur në console/pageerror.
"""

import json
import os
import re
import time

from playwright.sync_api import sync_playwright

from e2e.pilot import lib
from e2e.pilot.lib import IDS, check, wait

ENT_UI, CEN_UI = "http://127.0.0.1:5173", "http://127.0.0.1:5174"
CHROME = os.environ.get("E2E_CHROME", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
errors: list[str] = []
http_bad: list[str] = []
writes: list[str] = []
RUN = str(int(time.time()) % 100000)
SND, CAMP = f"UIP{RUN}", f"UI pilot campaign {RUN}"
A_KEY, B_KEY = IDS["tenants"]["a"]["key"], IDS["tenants"]["b"]["key"]
admin_api = lib.central("PILOT_ADMIN_PASSWORD")
A_API, ta = lib.tenant("a")
B_API, tb = lib.tenant("b")


def guard(page, label):
    page.on("pageerror", lambda e: errors.append(f"{label} pageerror: {e}"))
    page.on(
        "console",
        lambda m: (
            errors.append(f"{label} console: {m.text}")
            if m.type == "error" and "Failed to load resource" not in m.text
            else None
        ),
    )
    page.on(
        "response",
        lambda r: (
            http_bad.append(
                f"{label} {r.status} {r.request.method} {re.sub(r'[0-9a-f-]{36}', '<id>', r.url.split('?')[0])}"
            )
            if r.status >= 400
            else None
        ),
    )
    page.on(
        "request",
        lambda r: (
            writes.append(f"{label} {r.method} {r.url.split('?')[0]}")
            if r.method not in ("GET", "HEAD")
            else None
        ),
    )


def hash_(p, h):
    p.evaluate(f"location.hash = {json.dumps(h)}")


def ent_login(b, key, label, width=1360):
    ctx = b.new_context(viewport={"width": width, "height": 900}, accept_downloads=True)
    ctx.add_init_script("try{localStorage.setItem('sms_lang','en')}catch(e){}")
    p = ctx.new_page()
    guard(p, label)
    p.goto(ENT_UI)
    p.evaluate("sessionStorage.clear()")
    p.reload()
    p.fill("input[type=password]", key)
    p.click("button:has-text('Sign in')")
    p.wait_for_selector("nav")
    return ctx, p


def cen_login(b, email, pw, label, width=1360):
    ctx = b.new_context(viewport={"width": width, "height": 900}, accept_downloads=True)
    p = ctx.new_page()
    guard(p, label)
    p.goto(CEN_UI)
    p.evaluate("sessionStorage.clear(); localStorage.setItem('central_lang','en')")
    p.reload()
    p.fill("input[type=email]", email)
    p.fill("input[type=password]", pw)
    p.click("button:has-text('Sign in')")
    p.wait_for_selector("nav[aria-label='Main navigation']")
    return ctx, p


def no_overflow(p):
    return not p.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
    )


def customer_flow(b):
    ctx, p = ent_login(b, A_KEY, "ent-A")
    nav = p.locator("nav").inner_text()
    check(
        "Campaigns" in nav and "Reports" in nav and "Staff" not in nav,
        "customer menu: messaging + reports, no Staff group",
    )
    hash_(p, "#dashboard")
    p.wait_for_timeout(800)
    # --- sender request în UI ---
    hash_(p, "#senders")
    p.wait_for_selector("text=Request a sender ID")
    p.get_by_label(re.compile("^Sender ID")).fill(SND)
    p.get_by_role("button", name="Submit for approval").click()
    p.wait_for_selector(".toast:has-text('Request sent')")
    check(True, "sender requested from the UI")
    cs = wait(lambda: lib.central_sender(admin_api, SND), f"Central received {SND}")
    check(cs["status"] == "pending", "Central sees the UI-created request as pending")
    # --- Central miraton nga UI admin ---
    cctx, c = cen_login(b, "admin@pilot.test", os.environ["PILOT_ADMIN_PASSWORD"], "cen-admin")
    hash_(c, f"#senders/{cs['id']}")
    c.wait_for_selector(f"text={SND}")
    c.get_by_role("button", name=re.compile("^Approve")).first.click()
    c.get_by_role("dialog").get_by_role("button", name="Approve", exact=True).click()
    wait(
        lambda: lib.central_sender(admin_api, SND)["status"] == "approved",
        "Central UI approval persisted",
    )
    check(True, "admin approved it through the Central UI")
    # --- projeksion → UI klient ---
    wait(
        lambda: any(
            s["value"] == SND and s["effective_status"] == "approved"
            for s in A_API.get("/v1/sender-ids")[1]
        ),
        "projection reaches Enterprise",
    )
    hash_(p, "#senders")
    p.reload()
    p.wait_for_selector(f"text={SND}")
    row = p.locator(f"tbody tr:has-text('{SND}')").first.inner_text().lower()
    check("approved" in row, f"customer UI shows the sender as approved: {row[:60]!r}")
    # --- kontakte (import në UI) ---
    hash_(p, "#contacts?tab=import")
    p.get_by_label("Or paste here").fill(
        "phone,first_name\n"
        + "\n".join(f"{ta['phone_prefix']}{i:04d},UiPerson{i}" for i in (51, 52, 53))
    )
    p.get_by_role("button", name=re.compile("Import 3 people")).click()
    p.wait_for_selector("text=imported", timeout=15000)
    check(True, "contacts imported from the UI (server-validated)")
    # --- campaign (wizard) me sender të ri ---
    hash_(p, "#campaigns/new")
    p.get_by_label(re.compile("^Name")).fill(CAMP)
    sel = p.get_by_label(re.compile("^Sender"))
    if sel.count():
        sel.first.select_option(value=SND)
    p.get_by_role("button", name="Next").click()
    p.get_by_label(re.compile("^Who receives it")).select_option(index=1)
    p.wait_for_selector("text=can be contacted")
    p.get_by_role("button", name="Next").click()
    p.get_by_role("textbox", name=re.compile("Message")).fill("Hello from the pilot UI")
    p.get_by_role("button", name="Next").click()
    p.get_by_role("button", name="Next").click()
    send_btn = p.get_by_role("button", name=re.compile("^Send now")).first
    send_btn.click()
    dlg = p.get_by_role("dialog")
    check(dlg.count() == 1, "send-now asks for confirmation in a dialog (double-submit guard)")
    check(
        p.evaluate(
            "document.activeElement && document.activeElement.closest('[role=dialog]') !== null"
        ),
        "Enterprise confirm dialog: focus moves inside it",
    )
    for _ in range(6):
        p.keyboard.press("Tab")
    check(
        p.evaluate("document.activeElement.closest('[role=dialog]') !== null"),
        "Enterprise confirm dialog: Tab stays inside (focus trap)",
    )
    p.keyboard.press("Escape")
    p.wait_for_function("document.querySelector('[role=dialog]') === null")
    check(p.evaluate("location.hash.includes('new')"), "Esc cancels: nothing was sent")
    check(
        p.evaluate(
            "document.activeElement && document.activeElement.textContent.trim().startsWith('Send now')"
        ),
        "focus returns to the button that opened the dialog",
    )
    send_btn.click()
    p.get_by_role("dialog").get_by_role("button", name=re.compile("Send now")).click()
    p.wait_for_function(
        "location.hash.startsWith('#campaigns/') && !location.hash.includes('new')", timeout=20000
    )
    cid = int(re.search(r"#campaigns/(\d+)", p.evaluate("location.hash")).group(1))
    check(
        True,
        f"campaign {cid} created and started from the UI (server estimate/balance, double-submit guarded)",
    )
    wait(
        lambda: A_API.get(f"/v1/campaigns/{cid}")[1]["status"] == "completed",
        "campaign completes",
        timeout=90,
    )
    msgs = wait(
        lambda: (lambda r: r if len(r["items"]) >= 20 else None)(
            A_API.get("/v1/messages", campaign_id=cid, limit=100)[1]
        ),
        "campaign messages",
    )
    for it in msgs["items"]:
        if it["to"].endswith(("0001", "0002")):
            continue
        wait(
            lambda it=it: (
                A_API.get(f"/v1/messages/{it['id']}")[1]["status"] in ("sent", "delivered")
            ),
            "dispatched",
            timeout=90,
        )
        cur = A_API.get(f"/v1/messages/{it['id']}")[1]
        if cur["status"] == "sent":
            lib.dlr(cur["provider_message_id"])
    p.reload()
    p.wait_for_selector("text=Delivery summary")
    check("Hello from the pilot UI" in p.inner_text("main"), "campaign overview shows the message")
    p.get_by_role("tab", name="Recipients").click()
    p.wait_for_selector("main tbody tr")
    check(
        p.locator("main tbody tr").count() >= 20,
        "recipients tab lists the audience (server-paginated)",
    )
    p.get_by_role("tab", name="SMS traffic").click()
    p.wait_for_selector("main tbody tr")
    txt = p.inner_text("main").lower()
    check(
        "delivered" in txt and "unknown" not in txt,
        "traffic tab: delivered/failed, UNKNOWN never shown",
    )
    p.get_by_role("tab", name="Activity").click()
    p.wait_for_selector("text=Campaign created")
    # --- raporte + aktivitet ---
    hash_(p, "#reports")
    p.wait_for_selector("text=SMS submitted")
    check(
        re.search(r"\d+\s*%", p.inner_text("main")) is not None,
        "reports overview shows the server delivery rate",
    )
    hash_(p, "#reports?tab=campaigns")
    p.wait_for_selector(f"td:has-text('{CAMP}')")
    hash_(p, "#reports?tab=failures")
    p.wait_for_selector("text=By error code")
    check(
        "fake_rejected" in p.inner_text("main"),
        "failures report shows the FakeProvider rejection with its code",
    )
    with p.expect_download() as d:
        hash_(p, "#reports?tab=usage")
        p.wait_for_selector("text=SMS cost (delivered)")
        p.get_by_role("button", name="Export SMS (CSV)").click()
    raw = open(d.value.path(), "rb").read()
    check(
        raw.startswith(b"\xef\xbb\xbf") and b"delivered" in raw and b"unknown" not in raw.lower(),
        "SMS CSV: UTF-8 BOM, public statuses only",
    )
    hash_(p, "#activity")
    p.wait_for_selector("main tbody tr")
    body = p.inner_text("main").lower()
    check(
        "sender id requested" in body and "campaign started" in body.replace("or scheduled", ""),
        "activity log lists the sender request and the campaign start",
    )
    check("tenant-b" not in body and "pilotb" not in body, "activity log shows nothing of tenant B")
    # --- faturim + PDF ---
    hash_(p, "#billing")
    p.wait_for_selector(f"text={ta['invoice_number']}")
    with p.expect_download() as d:
        p.get_by_role(
            "button", name=re.compile(f"Download official PDF of invoice {ta['invoice_number']}")
        ).click()
    pdf = open(d.value.path(), "rb").read()
    check(pdf[:5] == b"%PDF-", "official invoice PDF downloaded from the UI")
    check(tb["invoice_number"] not in p.inner_text("main"), "tenant B's invoice is not listed")
    # --- skenarë dështimi në UI ---
    hash_(p, f"#campaigns/{B_API.get('/v1/campaigns')[1][0]['id']}")
    p.wait_for_timeout(1200)
    t = p.inner_text("main")
    check(
        "find that" in t.lower() or "not found" in t.lower(),
        "tenant B's campaign URL → plain not-found state",
    )
    check(
        "Traceback" not in t and "tenant-b" not in t and "Pilot B" not in t,
        "…and nothing of tenant B",
    )
    hash_(p, "#campaigns")
    p.wait_for_selector("main tbody tr")
    ctx.set_offline(True)
    hash_(p, "#campaigns?status=running")
    p.wait_for_timeout(2500)
    shown = p.inner_text("main")
    check(
        ("connect" in shown.lower() or "connection" in shown.lower() or "network" in shown.lower())
        and "TypeError" not in shown
        and "Failed to fetch" not in shown,
        "network interruption → friendly connection message, no raw error",
    )
    ctx.set_offline(False)
    # 5xx e sanitizuar (përgjigje e simuluar nga rrjeti): UI nuk shfaq trupin e papërpunuar
    p.route(
        "**/v1/campaigns**",
        lambda r: r.fulfill(
            status=500,
            content_type="application/json",
            body=json.dumps(
                {"detail": "Traceback (most recent call last): secret_dsn=postgresql://u:p@h/db"}
            ),
        ),
    )
    hash_(p, "#campaigns?status=draft")
    p.wait_for_timeout(1500)
    shown = p.inner_text("body")
    check(
        "secret_dsn" not in shown
        and "Traceback" not in shown
        and p.locator("[role=alert], .alert").count() >= 1,
        "a 5xx is shown as a sanitized error state (no raw body)",
    )
    p.unroute("**/v1/campaigns**")
    p.route(
        "**/v1/campaigns**",
        lambda r: r.fulfill(
            status=429,
            headers={"Retry-After": "3"},
            content_type="application/json",
            body=json.dumps({"detail": {"code": "rate_limited", "message": "slow"}}),
        ),
    )
    hash_(p, "#campaigns?status=paused")
    p.wait_for_timeout(1500)
    check(p.locator("[role=alert], .alert").count() >= 1, "429 is a friendly retry state")
    p.unroute("**/v1/campaigns**")
    # sesioni i skaduar: çelës i revokuar → 401 → kthim te hyrja
    st, k = A_API.post("/v1/portal/api-keys", {"name": "ui-expire"})
    ctx2, q = ent_login(b, k["key"], "ent-A2")
    A_API.post(f"/v1/portal/api-keys/{k['id']}/revoke", {})
    hash_(q, "#contacts")
    q.reload()
    q.wait_for_selector("input[type=password]", timeout=15000)
    check(True, "401 (revoked key) returns to the sign-in screen")
    ctx2.close()
    # 403 mbetet i kyçur: roli i stafit hap faqe që s'e ka lejen
    sctx, s = ent_login(b, IDS["keys"]["support"], "ent-support")
    hash_(s, "#wallet")
    s.wait_for_timeout(1000)
    hash_(s, "#rates")
    s.wait_for_timeout(1000)
    check(
        "sign in" not in s.inner_text("body").lower() and s.locator("nav").count() == 1,
        "403 (support lacks rates:read) keeps the session signed in",
    )
    sctx.close()
    # tenant B në kontekst të veçantë
    bctx, bp = ent_login(b, B_KEY, "ent-B")
    hash_(bp, "#campaigns")
    bp.wait_for_selector("main tbody tr")
    bt = bp.inner_text("main")
    check(CAMP not in bt and SND not in bt, "tenant B does not see tenant A's UI campaign")
    hash_(bp, "#senders")
    bp.wait_for_timeout(1200)
    check(
        SND not in bp.inner_text("main")
        and "PILOTA" not in bp.inner_text("main").replace("PILOTB", ""),
        "tenant B does not see tenant A's senders",
    )
    hash_(bp, "#billing")
    bp.wait_for_selector(f"text={tb['invoice_number']}")
    check(
        ta["invoice_number"] not in bp.inner_text("main"),
        "tenant B does not see tenant A's invoice",
    )
    bctx.close()
    cctx.close()
    return ctx


def central_flow(b):
    ctx, p = cen_login(b, "admin@pilot.test", os.environ["PILOT_ADMIN_PASSWORD"], "cen-admin")
    pages = {
        "#dashboard": "Sender",
        "#senders": "Sender Registry",
        "#policies": "Country Policies",
        "#ops": "Overall sender governance health",
        "#ops-sync": "Sync",
        "#com": "Commercial",
        "#com-invoices": "Invoice",
        "#audit-senders": "Sender decisions",
        "#audit-staff": "Staff activity",
        "#audit-ops": "Operations: current state only",
    }
    for h, needle in pages.items():
        hash_(p, h)
        p.wait_for_timeout(900)
        t = p.inner_text("main")
        check(
            needle.lower() in t.lower() and "something went wrong" not in t.lower(),
            f"admin: {h} renders",
        )
    hash_(p, "#senders")
    p.wait_for_selector("main tbody tr")
    t = p.inner_text("main")
    check(
        "PILOTA" in t and "PILOTB" in t and SND in t,
        "admin sees every tenant's senders (cross-tenant is the staff role's purpose)",
    )
    hash_(p, "#ops")
    p.wait_for_timeout(1200)
    check(
        "fresh" in p.inner_text("main").lower() or "pass" in p.inner_text("main").lower(),
        "operations/readiness shows evidence from the live Enterprise reporter",
    )
    hash_(p, "#audit-senders")
    p.wait_for_selector("main tbody tr")
    check(
        "PILOTB" in p.inner_text("main") and "revoked" in p.inner_text("main").lower(),
        "sender decision audit includes the revoke",
    )
    ctx.close()
    octx, o = cen_login(
        b, "operator@pilot.test", os.environ["PILOT_OPERATOR_PASSWORD"], "cen-operator"
    )
    for h in (
        "#senders",
        "#policies",
        "#com",
        "#com-invoices",
        "#ops",
        "#audit-senders",
        "#audit-commercial",
        "#audit-staff",
    ):
        hash_(o, h)
        o.wait_for_timeout(700)
        n = o.get_by_role(
            "button",
            name=re.compile(
                "^(Approve|Reject|Revoke|Resubmit|Save|Delete|Create|Assign|Void|Issue|Add|Record)",
                re.I,
            ),
        ).count()
        check(n == 0, f"operator: no mutation controls on {h}")
    cs = lib.central_sender(admin_api, SND)
    hash_(o, f"#senders/{cs['id']}")
    o.wait_for_selector(f"text={SND}")
    check(
        o.get_by_role("button", name=re.compile("Approve|Reject|Revoke|Resubmit")).count() == 0
        and "read-only" in o.inner_text("body").lower(),
        "operator sender detail is read-only (with notice)",
    )
    octx.close()


def responsive_a11y(b):
    pages_ent = [
        "#dashboard",
        "#contacts",
        "#contacts?tab=lists",
        "#senders",
        "#campaigns",
        f"#campaigns/{A_API.get('/v1/campaigns')[1][0]['id']}",
        "#messages",
        "#reports",
        "#reports?tab=failures",
        "#activity",
        "#billing",
        "#wallet",
    ]
    pages_cen = [
        "#dashboard",
        "#senders",
        "#policies",
        "#ops",
        "#com",
        "#com-invoices",
        "#com-payments",
        "#audit-senders",
        "#audit-staff",
    ]
    for width in (390, 768, 1360):
        ctx, p = ent_login(b, A_KEY, f"ent-{width}", width)
        for h in pages_ent:
            hash_(p, h)
            p.wait_for_timeout(600)
            check(no_overflow(p), f"Enterprise {h} has no horizontal page overflow at {width}px")
        ctx.close()
        ctx, p = cen_login(
            b, "admin@pilot.test", os.environ["PILOT_ADMIN_PASSWORD"], f"cen-{width}", width
        )
        for h in pages_cen:
            hash_(p, h)
            p.wait_for_timeout(600)
            check(no_overflow(p), f"Central {h} has no horizontal page overflow at {width}px")
        ctx.close()
    # a11y bazë: emra të aksesueshëm, landmark-e, aria-current, fokus, dialog
    ctx, p = ent_login(b, A_KEY, "ent-a11y")
    for h in ("#campaigns/new", "#contacts?tab=add", "#senders", "#reports", "#activity"):
        hash_(p, h)
        p.wait_for_timeout(800)
        unnamed = p.evaluate("""() => [...document.querySelectorAll('input:not([type=hidden]),select,textarea')].filter(e => e.offsetParent !== null).filter(e => {
            const n = e.getAttribute('aria-label') || e.getAttribute('aria-labelledby') || (e.id && document.querySelector(`label[for="${e.id}"]`)) || e.closest('label'); return !n; }).map(e => e.outerHTML.slice(0, 80))""")
        check(not unnamed, f"{h}: every visible form control has an accessible name {unnamed[:2]}")
    check(
        p.locator("main").count() == 1 and p.locator("nav").count() >= 1,
        "landmarks: one <main>, navigation present",
    )
    check(
        p.locator("nav [aria-current]").count() >= 1,
        "navigation marks the current page (aria-current)",
    )
    p.keyboard.press("Tab")
    focus = p.evaluate(
        "(() => { const a = document.activeElement; const cs = getComputedStyle(a); return a && a !== document.body ? (cs.outlineStyle !== 'none' || cs.boxShadow !== 'none') : false })()"
    )
    check(focus, "keyboard focus is visible on the first focusable element")
    hash_(p, "#reports?tab=usage")
    p.wait_for_selector("table")
    check(p.locator("table").count() >= 1, "usage report has a text table next to the chart")
    ctx.close()
    ctx, c = cen_login(b, "admin@pilot.test", os.environ["PILOT_ADMIN_PASSWORD"], "cen-a11y")
    cs = lib.central_sender(admin_api, "PILOTB")
    hash_(c, f"#senders/{cs['id']}")
    c.wait_for_selector("text=PILOTB")
    btn = c.get_by_role("button", name=re.compile("^(Resubmit|Approve)")).first
    btn.focus()
    btn.click()
    dlg = c.get_by_role("dialog")
    dlg.wait_for()
    inside = c.evaluate(
        "document.activeElement && document.activeElement.closest('[role=dialog]') !== null"
    )
    check(inside, "dialog opens with focus inside it")
    for _ in range(8):
        c.keyboard.press("Tab")
    check(
        c.evaluate("document.activeElement.closest('[role=dialog]') !== null"),
        "Tab does not leave the dialog (focus trap)",
    )
    c.keyboard.press("Escape")
    c.wait_for_function("document.querySelector('[role=dialog]') === null")
    check(c.locator("[role=dialog]").count() == 0, "Esc closes the dialog")
    check(
        c.evaluate("document.activeElement && document.activeElement.tagName")
        in ("BUTTON", "A", "BODY"),
        "focus returns to the page after the dialog",
    )
    ctx.close()


def i18n_pass(b):
    ctx = b.new_context(viewport={"width": 1360, "height": 900})
    ctx.add_init_script("try{localStorage.setItem('sms_lang','sq')}catch(e){}")
    p = ctx.new_page()
    guard(p, "ent-sq")
    p.goto(ENT_UI)
    p.evaluate("sessionStorage.clear()")
    p.reload()
    p.fill("input[type=password]", A_KEY)
    p.locator("form button").first.click()
    p.wait_for_selector("nav")
    EN = re.compile(
        r"\b(Campaigns|Message history|Sender IDs|Delivery|Recipients|Sign out|Loading|Something went wrong|No data|Refresh|Older|Newest|Filter by)\b"
    )
    for h in (
        "#dashboard",
        "#contacts",
        "#senders",
        "#campaigns",
        "#campaigns/new",
        "#messages",
        "#reports",
        "#reports?tab=failures",
        "#activity",
        "#billing",
        "#wallet",
    ):
        hash_(p, h)
        p.wait_for_timeout(900)
        text = p.inner_text("body")
        left = sorted(set(EN.findall(text)))
        check(not left, f"Albanian UI {h}: no obvious English strings left {left}")
    ctx.close()
    ctx = b.new_context(viewport={"width": 1360, "height": 900})
    ctx.add_init_script("try{localStorage.setItem('central_lang','sq')}catch(e){}")
    p = ctx.new_page()
    guard(p, "cen-sq")
    p.goto(CEN_UI)
    p.evaluate("sessionStorage.clear(); localStorage.setItem('central_lang','sq')")
    p.reload()
    p.fill("input[type=email]", "admin@pilot.test")
    p.fill("input[type=password]", os.environ["PILOT_ADMIN_PASSWORD"])
    p.locator("form button").first.click()
    p.wait_for_selector("nav[aria-label]")
    EN2 = re.compile(
        r"\b(Sender Registry|Country Policies|Staff activity|Sender decisions|Loading|Something went wrong|Refresh|Previous|Next|Filter)\b"
    )
    for h in (
        "#dashboard",
        "#senders",
        "#policies",
        "#ops",
        "#com",
        "#audit-senders",
        "#audit-staff",
        "#audit-commercial",
    ):
        hash_(p, h)
        p.wait_for_timeout(900)
        left = sorted(set(EN2.findall(p.inner_text("body"))))
        check(not left, f"Albanian Central {h}: no obvious English strings left {left}")
    ctx.close()


def main():
    secrets = [
        k
        for k in os.environ
        if re.search(r"TWILIO|VONAGE|INFOBIP|SMPP|SMTP_|HTTP_PROVIDER", k, re.I)
    ]
    check(not secrets, f"no real provider credentials in the environment: {secrets}")
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=CHROME)
        customer_flow(b)
        central_flow(b)
        responsive_a11y(b)
        i18n_pass(b)
        b.close()
    allow = re.compile(r"(404|409|422|429|500|401|403|402) ")
    unexpected = [x for x in http_bad if not allow.search(x)]
    check(not errors, f"no console errors / page errors / unhandled rejections: {errors[:3]}")
    check(not unexpected, f"no unexpected HTTP errors: {unexpected[:3]}")
    print("expected negative responses seen:", sorted(set(http_bad))[:12])
    print(
        "write requests issued by the UI:",
        sorted(set(re.sub(r"[0-9a-f]{8}-[0-9a-f-]{27}|/\d+(?=/|$)", "/<id>", w) for w in writes)),
    )
    print(f"{lib.passed} checks")


main()
