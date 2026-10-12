"""E2E i konsolës Central me Chromium të vërtetë (Sender Governance).

Kërkon stack-un e përkohshëm të zhvillimit (shih e2e/central_seed.py): Central API + Vite i frontend-central.
    E2E_BASE=http://127.0.0.1:5174 E2E_IDS=ids.json E2E_ADMIN_PW=... E2E_OP_PW=... python e2e/test_central_console.py
Nuk ekzekutohet nga pytest/CI; është kontroll dore për ndërfaqen.
"""

import json
import os
import re

from playwright.sync_api import Page, sync_playwright

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:5174")
CHROME = os.environ.get("E2E_CHROME", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
IDS = json.load(open(os.environ["E2E_IDS"]))
ADMIN_PW, OP_PW = os.environ["E2E_ADMIN_PW"], os.environ["E2E_OP_PW"]
errors: list[str] = []
http_errors: list[str] = []  # përgjigje HTTP >= 400 të pritura nga skenarët negativë (shih EXPECTED_HTTP)
passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise AssertionError(msg)
    passed += 1


def text(p, sel="main"):
    """Page + selektor, ose drejtpërdrejt një locator."""
    return p.inner_text() if hasattr(p, "inner_text") and not hasattr(p, "goto") else p.inner_text(sel)


def has(needle, hay):
    return needle.lower() in hay.lower()


def new_page(b, width=1360, height=900):
    ctx = b.new_context(viewport={"width": width, "height": height})
    p = ctx.new_page()
    p.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    # Chromium e regjistron çdo përgjigje HTTP >= 400 si "Failed to load resource" në console: ato i numërojmë veç, me URL, dhe i krahasojmë me të pritshmet.
    p.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" and "Failed to load resource" not in m.text else None)
    p.on("response", lambda r: http_errors.append(f"{r.status} {r.request.method} {r.url.split('?')[0].replace(BASE, '')}") if r.status >= 400 else None)
    p.goto(BASE)
    p.evaluate("sessionStorage.clear(); localStorage.setItem('central_lang','en')")
    p.reload()
    return p


def login(p: Page, email, pw):
    p.fill("input[type=email]", email)
    p.fill("input[type=password]", pw)
    p.click("button:has-text('Sign in')")
    p.wait_for_selector("nav[aria-label='Main navigation']")


def go(p, hash_, wait=None):
    p.evaluate(f"location.hash = {json.dumps(hash_)}")
    if wait:
        p.wait_for_selector(wait)


def row(p, value):
    return p.locator(f"tbody tr:has-text('{value}')")


def dialog_submit(p, name):
    d = p.get_by_role("dialog")
    d.get_by_role("button", name=name, exact=True).click()


def admin_flow(b):
    p = new_page(b)
    # bad login first
    p.fill("input[type=email]", "admin@e2e.test"); p.fill("input[type=password]", "wrong-password")
    p.click("button:has-text('Sign in')")
    p.wait_for_selector("[role=alert]")
    check(has("isn't right", text(p, "body")), "bad login message")
    login(p, "admin@e2e.test", ADMIN_PW)
    check(not has("Read-only access", text(p, "aside")), "admin has no read-only badge")
    check(p.evaluate("sessionStorage.getItem('central_token')") not in (None, ""), "token in sessionStorage")
    check(ADMIN_PW not in p.content(), "password not in DOM")

    # registry
    go(p, "#senders", "tbody tr")
    body = text(p)
    for v in ("ACMEPEND", "GLOBEXPEND", "AUTOOK", "DENIED1"):
        check(v in body, f"registry lists {v}")
    check(has("approved", text(row(p, "AUTOOK"))), "AUTOOK auto-approved")
    check(has("rejected", text(row(p, "DENIED1"))), "DENIED1 rejected by policy")
    # server filter
    p.select_option("select[aria-label='Status']", "pending")
    p.click("button:has-text('Apply filters')")
    p.wait_for_function("document.querySelectorAll('tbody tr').length === 2")
    check("status=pending" in p.evaluate("location.hash"), "filter in URL")
    p.reload(); p.wait_for_selector("tbody tr")
    check(p.locator("tbody tr").count() == 2 and "GLOBEXPEND" in text(p), "deep link reproduces filter")
    p.screenshot(path=os.environ.get("E2E_SHOT", "/dev/null")) if os.environ.get("E2E_SHOT") else None

    # approve ACMEPEND
    go(p, f"#senders/{IDS['pending_acme']}", "h3:has-text('ACMEPEND')")
    p.click("button:has-text('Approve')")
    p.get_by_role("dialog").get_by_label(re.compile("Evidence reference")).fill("TICKET-42")
    dialog_submit(p, "Approve")
    p.wait_for_selector("main .badge.good:has-text('approved')")
    p.wait_for_selector("li:has-text('Evidence: TICKET-42')")
    check("Admin" in text(p, "ol.timeline"), "history shows an admin actor")
    check(p.locator("button:has-text('Revoke')").count() == 1 and p.locator("button:has-text('Approve')").count() == 0, "revoke offered after approve")

    # reject GLOBEXPEND (reason required)
    go(p, f"#senders/{IDS['pending_globex']}", "h3:has-text('GLOBEXPEND')")
    p.click("button:has-text('Reject')")
    check(p.get_by_role("dialog").get_by_role("button", name="Reject", exact=True).is_disabled(), "reject disabled without reason")
    p.get_by_role("dialog").get_by_label("Reason").fill("Brand mismatch")
    dialog_submit(p, "Reject")
    p.wait_for_selector("main .badge.bad:has-text('rejected')")
    check("Brand mismatch" in text(p, "ol.timeline"), "reject reason in history")

    # revoke + wording, then resubmit
    go(p, f"#senders/{IDS['pending_acme']}", "button:has-text('Revoke')")
    p.click("button:has-text('Revoke')")
    dtext = text(p, "[role=dialog]")
    check("can't be recalled" in dtext, "revoke copy mentions provider calls can't be recalled")
    p.get_by_role("dialog").get_by_label("Reason").fill("Policy breach")
    dialog_submit(p, "Revoke")
    p.wait_for_selector("main .badge.bad:has-text('revoked')")
    p.click("button:has-text('Resubmit')")
    dialog_submit(p, "Resubmit")
    p.wait_for_selector("main .badge.warn:has-text('pending')")
    p.wait_for_function("document.querySelectorAll('ol.timeline li').length >= 4", timeout=8000)
    check(p.locator("ol.timeline li").count() >= 4, "history grows: requested, approved, revoked, resubmitted")

    # auto-approved / policy-denied
    go(p, f"#senders/{IDS['auto_ok']}", "h3:has-text('AUTOOK')")
    check("Approved automatically" in text(p), "auto-approved note")
    check("System (sender policy)" in text(p), "system actor shown")
    go(p, f"#senders/{IDS['denied']}", "h3:has-text('DENIED1')")
    check("Rejected automatically" in text(p), "policy-denied note")

    # policies: list, default preview, create, no-change, dangerous change
    go(p, "#policies", "text=Current policies")
    check("XK" in text(p) and "DE" in text(p), "explicit policies listed")
    p.get_by_label("Country (ISO code)").first.fill("AL")
    p.click("button:has-text('Preview')")
    p.wait_for_selector("text=Default policy")
    p.click("button:has-text('New policy revision')")
    d = p.get_by_role("dialog")
    d.get_by_label(re.compile("Country")).fill("AL")
    d.get_by_label(re.compile("^Reason")).fill("pilot")
    d.get_by_role("checkbox", name="Requires approval").uncheck()
    dialog_submit(p, "Save revision")
    p.wait_for_selector("[role=status]:has-text('Policy revision 1 saved for AL alphanumeric')")
    p.click("button:has-text('New policy revision')")
    d = p.get_by_role("dialog")
    d.get_by_label(re.compile("Country")).fill("AL")
    d.get_by_label(re.compile("^Reason")).fill("same again")
    d.get_by_role("checkbox", name="Requires approval").uncheck()
    dialog_submit(p, "Save revision")
    p.wait_for_selector("text=No change: this is identical")
    # dangerous: deny XK alphanumeric (AUTOOK is approved there)
    p.locator("tbody tr:has-text('XK')").get_by_role("button", name="New revision").click()
    d = p.get_by_role("dialog")
    d.get_by_label(re.compile("^Reason")).fill("fraud wave")
    d.get_by_role("checkbox", name="Allowed").uncheck()
    d.get_by_role("checkbox", name="Requires approval").check()
    dialog_submit(p, "Save revision")
    check("may revoke currently approved sender IDs" in text(p, "[role=dialog]"), "dangerous-change warning")
    dialog_submit(p, "Yes, apply this change")
    p.wait_for_selector("text=1 approved sender IDs were revoked.")
    go(p, f"#senders/{IDS['auto_ok']}", "h3:has-text('AUTOOK')")
    p.wait_for_selector("main .badge.bad:has-text('revoked')")
    check("Revoked automatically by a policy change" in text(p), "policy_revoked note")
    # policy detail + history, immutable
    go(p, "#policies", "tbody tr")
    p.locator("tbody tr:has-text('XK')").get_by_role("link", name="Details").click()
    p.wait_for_selector("text=Revision history")
    p.wait_for_function("document.querySelectorAll('ol.timeline li').length === 2", timeout=8000)
    check(p.locator("ol.timeline li").count() == 2 and not p.get_by_role("button", name=re.compile("Edit|Delete")).count(), "immutable history with 2 revisions")
    # 404s
    go(p, "#senders/00000000-0000-4000-8000-000000000000", "text=We couldn't find that")
    go(p, "#nope", "text=This page doesn't exist")
    # responsive
    p.set_viewport_size({"width": 390, "height": 800})
    go(p, "#senders", "tbody tr")
    visible = p.evaluate("[...document.querySelectorAll('tbody tr:first-child td')].filter(td => td.offsetParent !== null).length")
    check(visible <= 5, f"narrow registry hides diagnostic columns (visible={visible})")
    check(not p.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"), "no horizontal page scroll at 390px")
    # session expiry via bad token
    p.evaluate("sessionStorage.setItem('central_token','garbage')")
    p.reload()
    p.wait_for_selector("input[type=email]")
    p.context.close()


def operator_flow(b):
    p = new_page(b)
    login(p, "operator@e2e.test", OP_PW)
    check(has("Read-only access", text(p, "aside")), "operator read-only badge")
    go(p, "#senders", "tbody tr")
    go(p, f"#senders/{IDS['pending_globex']}", "h3:has-text('GLOBEXPEND')") if False else None
    go(p, f"#senders/{IDS['pending_acme']}", "h3:has-text('ACMEPEND')")
    check(p.get_by_role("button", name=re.compile("Approve|Reject|Revoke|Resubmit")).count() == 0, "operator has no sender actions")
    check("Read-only access" in text(p), "read-only note on detail")
    check("Diagnostics" not in text(p), "diagnostics hidden from operator")
    go(p, "#policies", "text=Current policies")
    check(p.get_by_role("button", name=re.compile("New policy revision|New revision")).count() == 0, "operator has no policy create")
    # a forced write is rejected by the backend with 403 and the session survives
    status = p.evaluate("""async () => (await fetch('/admin/sender-policies', {method:'POST', headers:{'Content-Type':'application/json','Authorization':'Bearer '+sessionStorage.getItem('central_token')}, body: JSON.stringify({country:'AL',sender_kind:'alphanumeric',allowed:true,requires_approval:true,reason:'x'})})).status""")
    check(status == 403, f"backend rejects operator write (got {status})")
    check(p.locator("nav[aria-label='Main navigation']").count() == 1, "still signed in after 403")
    p.set_viewport_size({"width": 820, "height": 900})
    go(p, "#senders", "tbody tr")
    p.context.close()


if __name__ == "__main__":
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=CHROME)
        admin_flow(b)
        operator_flow(b)
        b.close()
    # skenarët negativë të qëllimshëm: hyrje e gabuar, token i prishur, 404 për sender-in/rrugën e panjohur, shkrim i operatorit
    uniq = sorted(set(re.sub(r"/[0-9a-f-]{36}", "/<id>", e) for e in http_errors))
    expected = ["401 GET /auth/me", "401 POST /auth/token", "403 POST /admin/sender-policies", "404 GET /admin/senders/<id>", "404 GET /admin/senders/<id>/history"]
    check(uniq == expected, f"only the intended negative responses occurred: {uniq}")
    print(f"{passed} checks passed")
    print("console/page errors:", errors or "none")
    raise SystemExit(1 if errors else 0)
