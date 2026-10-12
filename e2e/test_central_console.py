"""E2E i konsolës Central me Chromium të vërtetë (Sender Governance + Operations).

Kërkon stack-un e përkohshëm të zhvillimit (shih e2e/central_seed.py): Central API + Vite i
frontend-central, me CENTRAL_DATABASE_URL (DB e përkohshme) dhe PYTHONPATH drejt repo-s.

    E2E_BASE=http://127.0.0.1:5174 E2E_IDS=ids.json E2E_ADMIN_PW=... E2E_OP_PW=... \\
    python e2e/test_central_console.py

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
http_errors: list[
    str
] = []  # përgjigje HTTP >= 400 të pritura nga skenarët negativë (shih EXPECTED_HTTP)
passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise AssertionError(msg)
    passed += 1


def text(p, sel="main"):
    """Page + selektor, ose drejtpërdrejt një locator."""
    return (
        p.inner_text() if hasattr(p, "inner_text") and not hasattr(p, "goto") else p.inner_text(sel)
    )


def has(needle, hay):
    return needle.lower() in hay.lower()


def new_page(b, width=1360, height=900):
    ctx = b.new_context(viewport={"width": width, "height": height})
    p = ctx.new_page()
    p.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    # Chromium e regjistron çdo përgjigje HTTP >= 400 si "Failed to load resource" në console: ato i numërojmë veç, me URL, dhe i krahasojmë me të pritshmet.
    p.on(
        "console",
        lambda m: (
            errors.append(f"console: {m.text}")
            if m.type == "error" and "Failed to load resource" not in m.text
            else None
        ),
    )
    p.on(
        "response",
        lambda r: (
            http_errors.append(
                f"{r.status} {r.request.method} {r.url.split('?')[0].replace(BASE, '')}"
            )
            if r.status >= 400
            else None
        ),
    )
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
    p.fill("input[type=email]", "admin@e2e.test")
    p.fill("input[type=password]", "wrong-password")
    p.click("button:has-text('Sign in')")
    p.wait_for_selector("[role=alert]")
    check(has("isn't right", text(p, "body")), "bad login message")
    login(p, "admin@e2e.test", ADMIN_PW)
    check(not has("Read-only access", text(p, "aside")), "admin has no read-only badge")
    check(
        p.evaluate("sessionStorage.getItem('central_token')") not in (None, ""),
        "token in sessionStorage",
    )
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
    p.reload()
    p.wait_for_selector("tbody tr")
    check(
        p.locator("tbody tr").count() == 2 and "GLOBEXPEND" in text(p),
        "deep link reproduces filter",
    )
    p.screenshot(path=os.environ.get("E2E_SHOT", "/dev/null")) if os.environ.get(
        "E2E_SHOT"
    ) else None

    # approve ACMEPEND
    go(p, f"#senders/{IDS['pending_acme']}", "h3:has-text('ACMEPEND')")
    p.click("button:has-text('Approve')")
    p.get_by_role("dialog").get_by_label(re.compile("Evidence reference")).fill("TICKET-42")
    dialog_submit(p, "Approve")
    p.wait_for_selector("main .badge.good:has-text('approved')")
    p.wait_for_selector("li:has-text('Evidence: TICKET-42')")
    check("Admin" in text(p, "ol.timeline"), "history shows an admin actor")
    check(
        p.locator("button:has-text('Revoke')").count() == 1
        and p.locator("button:has-text('Approve')").count() == 0,
        "revoke offered after approve",
    )

    # reject GLOBEXPEND (reason required)
    go(p, f"#senders/{IDS['pending_globex']}", "h3:has-text('GLOBEXPEND')")
    p.click("button:has-text('Reject')")
    check(
        p.get_by_role("dialog").get_by_role("button", name="Reject", exact=True).is_disabled(),
        "reject disabled without reason",
    )
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
    check(
        p.locator("ol.timeline li").count() >= 4,
        "history grows: requested, approved, revoked, resubmitted",
    )

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
    check(
        "may revoke currently approved sender IDs" in text(p, "[role=dialog]"),
        "dangerous-change warning",
    )
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
    check(
        p.locator("ol.timeline li").count() == 2
        and not p.get_by_role("button", name=re.compile("Edit|Delete")).count(),
        "immutable history with 2 revisions",
    )
    # 404s
    go(p, "#senders/00000000-0000-4000-8000-000000000000", "text=We couldn't find that")
    go(p, "#nope", "text=This page doesn't exist")
    # responsive
    p.set_viewport_size({"width": 390, "height": 800})
    go(p, "#senders", "tbody tr")
    visible = p.evaluate(
        "[...document.querySelectorAll('tbody tr:first-child td')].filter(td => td.offsetParent !== null).length"
    )
    check(visible <= 5, f"narrow registry hides diagnostic columns (visible={visible})")
    check(
        not p.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"),
        "no horizontal page scroll at 390px",
    )
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
    check(
        p.get_by_role("button", name=re.compile("Approve|Reject|Revoke|Resubmit")).count() == 0,
        "operator has no sender actions",
    )
    check("Read-only access" in text(p), "read-only note on detail")
    check("Diagnostics" not in text(p), "diagnostics hidden from operator")
    go(p, "#policies", "text=Current policies")
    check(
        p.get_by_role("button", name=re.compile("New policy revision|New revision")).count() == 0,
        "operator has no policy create",
    )
    # a forced write is rejected by the backend with 403 and the session survives
    status = p.evaluate(
        """async () => (await fetch('/admin/sender-policies', {method:'POST', headers:{'Content-Type':'application/json','Authorization':'Bearer '+sessionStorage.getItem('central_token')}, body: JSON.stringify({country:'AL',sender_kind:'alphanumeric',allowed:true,requires_approval:true,reason:'x'})})).status"""
    )
    check(status == 403, f"backend rejects operator write (got {status})")
    check(p.locator("nav[aria-label='Main navigation']").count() == 1, "still signed in after 403")
    p.set_viewport_size({"width": 820, "height": 900})
    go(p, "#senders", "tbody tr")
    p.context.close()


def add_unresolved_bootstrap_run():
    """Kusht WARN në DB-në e përkohshme: një ekzekutim bootstrap me elemente të pazgjidhura (e njëjta rrugë që lexon API-ja)."""
    from datetime import UTC, datetime

    from sqlalchemy.orm import Session

    from apps.central.core.db import make_engine
    from apps.central.models.sender import SenderBootstrapRun

    with Session(make_engine()) as db:
        now = datetime.now(UTC)
        db.add(
            SenderBootstrapRun(
                artifact_hash="a" * 64,
                report_hash="b" * 64,
                source_revision="e2e-rev",
                status="completed",
                started_at=now,
                completed_at=now,
                tenant_count=2,
                sender_count=10,
                imported_count=7,
                unresolved_count=3,
            )
        )
        db.commit()


def ops_flow(b):
    p = new_page(b)
    login(p, "admin@e2e.test", ADMIN_PW)
    go(p, "#ops", "text=Overall sender governance health")
    overall = p.locator("[role=status][aria-label='Overall sender governance health'] .badge")
    check(
        has("unknown", overall.inner_text()),
        "overall is UNKNOWN while Enterprise evidence is unavailable",
    )
    check(
        p.locator("a[aria-label='Central readiness: pass']").count() == 1,
        "Central readiness card is PASS",
    )
    check(has("Registry states are valid", text(p)), "central checks listed")
    go(p, "#ops-sync", "text=Central → Enterprise sync feed")
    check(
        has("python -m scripts.sender_sync_readiness", text(p))
        and has("evidence unavailable", text(p)),
        "sync: Enterprise runtime explicitly unavailable",
    )
    check(
        p.locator("li:has-text('Sender requests are received from Enterprise') .badge.good").count()
        == 1,
        "request intake PASS (evidence present)",
    )
    go(p, "#ops-bootstrap", "text=Latest bootstrap run")
    check(has("No bootstrap run is recorded", text(p)), "bootstrap: no run yet")
    go(p, "#ops-alerts", "text=No active alerts")
    check(
        p.locator("button:visible").all_inner_texts() == ["Refresh", "Sign out"]
        or set(p.locator("main button:visible").all_inner_texts()) == {"Refresh"},
        "no mutation controls on Operations",
    )
    # WARN: ekzekutim bootstrap me të pazgjidhura, pastaj rifreskim manual
    add_unresolved_bootstrap_run()
    go(p, "#ops", "text=Overall sender governance health")
    p.click("button:has-text('Refresh')")
    p.wait_for_function(
        "document.querySelector(\"[aria-label='Overall sender governance health'] .badge\").textContent.includes('warn')",
        timeout=8000,
    )
    go(p, "#ops-bootstrap", "text=Latest bootstrap run")
    p.wait_for_selector("li:has-text('Bootstrap unresolved items') .badge.warn")
    check(
        "block cutover readiness" in text(p) and "e2e-rev" in text(p),
        "bootstrap WARN rendered with blocking explanation",
    )
    check("aaaaaaaaaaaa" in text(p) and "a" * 20 not in text(p), "hashes are shortened")
    go(p, "#ops-alerts", "text=Bootstrap items unresolved")
    check(
        p.locator("tbody tr").count() == 1 and has("warning", text(p, "tbody")),
        "alert listed with severity",
    )
    p.set_viewport_size({"width": 390, "height": 800})
    go(p, "#ops", "text=Overall sender governance health")
    check(
        not p.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"),
        "no horizontal page scroll on Operations at 390px",
    )
    p.context.close()
    # operator: aceleshin pamje, vetëm lexim
    q = new_page(b)
    login(q, "operator@e2e.test", OP_PW)
    go(q, "#ops", "text=Overall sender governance health")
    q.wait_for_function(
        "document.querySelector(\"[aria-label='Overall sender governance health'] .badge\").textContent.includes('warn')",
        timeout=8000,
    )
    check(has("Read-only access", text(q, "aside")), "operator sees the read-only badge")
    check(
        set(q.locator("main button:visible").all_inner_texts()) == {"Refresh"},
        "operator has only Refresh on Operations",
    )
    for page_ in ("#ops-sync", "#ops-bootstrap", "#ops-alerts"):
        go(q, page_, "text=Generated")
    q.context.close()


def run_report(scenario):
    """Raportuesi Enterprise REAL (e2e/ops_report.py) kundër DB-së Enterprise të përkohshme dhe Central API real."""
    import subprocess
    import sys

    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "SMS_ENV": "development",
        "SMS_DATABASE_URL": f"sqlite:///{os.environ['E2E_ENT_DB']}",
        "SMS_CP_BASE_URL": os.environ["E2E_CENTRAL_URL"],
        "SMS_CP_CLIENT_ID": IDS["service_client"],
        "SMS_CP_KEY_ID": IDS["service_kid"],
        "SMS_CP_PRIVATE_KEY_PATH": os.environ["E2E_KEY"],
        "SMS_PII_HMAC_KEY": "e2e-hmac-key-0123456789abcdef0123456789abcdef",
        "SMS_SENDER_AUTHORITY": "local",
    }
    r = subprocess.run(
        [sys.executable, "e2e/ops_report.py", scenario],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    check(
        r.returncode == 0 and "report: ok" in r.stdout,
        f"Enterprise reporter ({scenario}) delivered the snapshot: {r.stdout[-200:]} {r.stderr[-300:]}",
    )
    return r.stdout


def age_central_snapshot(seconds):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from apps.central.core.db import make_engine
    from apps.central.models.sender import SenderOpsSnapshot

    with Session(make_engine()) as db:
        db.scalar(select(SenderOpsSnapshot)).received_at = datetime.now(UTC) - timedelta(
            seconds=seconds
        )
        db.commit()


def overall_text(p):
    return p.locator("[aria-label='Overall sender governance health'] .badge").inner_text().lower()


def ops_enterprise_flow(b):
    p = new_page(b)
    login(p, "admin@e2e.test", ADMIN_PW)
    go(p, "#ops-sync", "text=Central → Enterprise sync feed")
    check(
        has("Not connected to Central", text(p)),
        "before any report: Enterprise sync evidence is unavailable",
    )
    run_report("base")
    p.reload()
    p.wait_for_selector("dt:has-text('Enterprise cursor')")
    row = lambda label: p.locator(f"dt:has-text('{label}') + dd").first.inner_text()  # noqa: E731
    check(
        row("Enterprise cursor") == "40"
        and row("Latest Central sequence known") == "45"
        and row("Lag") == "5",
        "sync: real cursor / Central seq / lag from the Enterprise reporter",
    )
    check(
        row("Pending") == "1"
        and row("Failed") == "0"
        and has("None open", row("Oldest open request")) is False,
        "request reporter: real backlog (1 pending, 0 failed)",
    )
    go(p, "#ops-bootstrap", "text=Shadow comparison (reported snapshot)")
    check(
        has("120 of", row("Sample progress")) and row("Critical drift") == "0",
        "shadow: real sample progress, no critical drift",
    )
    check(
        p.locator("li:has-text('Bootstrap evidence agreement') .badge.good").count() == 1,
        "Central and Enterprise bootstrap evidence agree",
    )
    check(has("Identity conflict", text(p)), "Enterprise bootstrap issue category shown")
    go(p, "#ops", "text=Enterprise snapshot")
    p.wait_for_selector("dt:has-text('Snapshot status')")
    check(
        has("fresh", p.locator("dt:has-text('Snapshot status') + dd").inner_text()),
        "snapshot freshness is fresh",
    )
    check(p.locator("a[aria-label='Enterprise snapshot: pass']").count() == 1, "snapshot card PASS")
    # snapshot i vjetruar (sipas serverit)
    age_central_snapshot(400)
    p.click("button:has-text('Refresh')")
    p.wait_for_selector("a[aria-label='Enterprise snapshot: warn']", timeout=8000)
    check(
        has("stale", p.locator("dt:has-text('Snapshot status') + dd").inner_text()),
        "stale snapshot flagged by the server",
    )
    check(
        p.locator("li:has-text('Enterprise sync runtime') .badge.good").count() == 0,
        "stale evidence is not shown as pass",
    )
    # shpërndarje kritike
    run_report("drift")
    p.click("button:has-text('Refresh')")
    p.wait_for_selector("a[aria-label='Enterprise snapshot: pass']", timeout=8000)
    p.wait_for_selector("a[aria-label='Critical drift: fail']", timeout=8000)
    check("fail" in overall_text(p), "critical drift makes the overall health FAIL")
    go(p, "#ops-alerts", "text=Critical shadow drift")
    check(
        p.locator("tbody tr:has-text('Critical shadow drift') td:has-text('Enterprise')").count()
        >= 1,
        "alert is sourced from Enterprise",
    )
    p.set_viewport_size({"width": 390, "height": 800})
    for page_ in ("#ops", "#ops-sync", "#ops-bootstrap", "#ops-alerts"):
        go(p, page_, "text=Generated")
        check(
            not p.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"),
            f"no horizontal scroll at 390px on {page_}",
        )
    p.context.close()
    q = new_page(b)
    login(q, "operator@e2e.test", OP_PW)
    go(q, "#ops", "text=Overall sender governance health")
    q.wait_for_function(
        "document.querySelector(\"[aria-label='Overall sender governance health'] .badge\").textContent.includes('fail')",
        timeout=8000,
    )
    check(
        set(q.locator("main button:visible").all_inner_texts()) == {"Refresh"},
        "operator sees the same data, read-only",
    )
    go(q, "#ops-bootstrap", "text=Shadow comparison (reported snapshot)")
    check(
        q.locator("dt:has-text('Critical drift') + dd").first.inner_text() == "6",
        "operator sees the real drift count",
    )
    q.context.close()


def commercial_flow(b):
    """M11-E: Commercial / Billing, vetëm-lexim, me të dhëna reale nga shërbimet (shih e2e/commercial_seed.py)."""
    p = new_page(b)
    login(p, "admin@e2e.test", ADMIN_PW)
    mutating = []
    p.on(
        "request",
        lambda r: (
            mutating.append(f"{r.method} {r.url}")
            if r.method not in ("GET", "HEAD") and "/auth/" not in r.url
            else None
        ),
    )
    nav = p.locator("nav[aria-label='Main navigation']")
    check(
        nav.get_by_text("Commercial", exact=True).count() == 1, "Commercial group in the navigation"
    )
    for label in (
        "Commercial Overview",
        "Credit & Grants",
        "Payments",
        "Pricing",
        "Subscriptions",
        "Invoices",
        "Reconciliation",
        "Financial Readiness",
    ):
        check(nav.get_by_role("link", name=re.compile(label)).count() == 1, f"nav link {label}")
    # overview
    go(p, "#com", "text=Billing authority")
    p.wait_for_selector("text=Central invoices")
    p.wait_for_selector("text=Pending invoice payments")
    check(
        has("local", text(p)) and has("Central invoices: 28", text(p)),
        "overview shows the billing authority mode and counts from the server",
    )
    link = p.get_by_role("main").get_by_role("link", name="Overdue open invoices").inner_text()
    check(re.search(r"\d+", link) is not None, "overview shows the server's overdue count")
    # credit
    go(p, "#com-credit", "text=Available to grant")
    check(
        has("2,500.50", text(p)) or has("2.500,50", text(p)),
        "funds shown exactly (2500.50 less outstanding grant handled by server)",
    )
    p.locator("tbody tr a:has-text('Details')").first.click()
    p.wait_for_selector("text=Append-only history")
    p.wait_for_selector("text=grant issued")
    check(
        has("payment credit", text(p)) and has("grant issued", text(p)),
        "ledger entries are visible",
    )
    check(has("500.25", text(p)) or has("500,25", text(p)), "grant amount exact")
    check(
        not p.get_by_role(
            "button",
            name=re.compile(r"^(new grant|issue|grant credit|adjust|reverse|approve|reject)", re.I),
        ).count(),
        "no credit write controls",
    )
    # grants tab
    go(p, "#com-credit?tab=grants", "text=Credit grants")
    p.get_by_role("button", name=re.compile("Details for grant")).first.click()
    check(has("welcome credit", p.get_by_role("dialog").inner_text()), "grant detail")
    p.keyboard.press("Escape")
    # payments
    go(p, "#com-payments", "text=Credit payments")
    p.wait_for_selector("text=BANK-2026-001")
    check(has("BANK-2026-001", text(p)) and has("BANK-2026-002", text(p)), "credit payments listed")
    check(has("pending", text(p)) and has("approved", text(p)), "payment statuses")
    p.get_by_role("button", name=re.compile("Details for payment")).first.click()
    d = p.get_by_role("dialog").inner_text()
    check(has("Created by", d) and has("Approved by", d), "payment detail has maker/checker")
    p.keyboard.press("Escape")
    go(p, "#com-payments?tab=invoice", "text=Invoice payments")
    p.wait_for_selector("text=INV-PAY-77")
    check(has("INV-PAY-77", text(p)), "invoice payment listed")
    # pricing
    go(p, "#com-pricing", "text=Price books")
    p.wait_for_selector("text=Standard")
    check(has("std", text(p)), "price book listed")
    p.locator("tbody tr a:has-text('Details')").first.click()
    p.wait_for_selector("text=Selection reason")
    p.get_by_role("button", name=re.compile("Rules of version")).first.click()
    p.wait_for_selector("text=Price rules")
    p.wait_for_selector("text=355")
    check(has("0.045", text(p)) and has("0.1", text(p)), "rule unit prices as sent by the server")
    go(p, "#com-pricing?tab=assignments", "text=Price assignments")
    p.wait_for_selector("tbody tr")
    check(p.locator("tbody tr").count() == 1, "assignment listed")
    go(p, "#com-pricing?tab=preview", "text=Price preview")
    p.fill("input >> nth=0", IDS["enterprise_acme"]) if False else None
    p.get_by_label("Enterprise ID").fill(IDS["enterprise_acme"])
    p.get_by_label("Product ID").fill(IDS["product"])
    p.get_by_label("Number").fill("35569123")
    p.get_by_role("button", name="Ask the server").click()
    p.wait_for_selector("text=EUR")
    check(
        has("0.045", text(p)) and has("EUR", text(p)),
        "server-side price preview (no JS rule matching)",
    )
    # subscriptions
    go(p, "#com-subs", "text=Subscriptions")
    p.wait_for_selector("tbody tr")
    check(p.locator("tbody tr").count() == 28, "all 28 subscriptions fit one page of 50")
    go(p, f"#com-subs/{IDS['enterprise_acme']}", "text=Billing profile")
    p.wait_for_selector("text=Acme Sh.p.k.")
    p.wait_for_selector("text=Monthly fee")
    p.wait_for_selector("td:has-text('invoiced')")
    check(has("1,000.00 EUR", text(p)) or has("1.000,00 EUR", text(p)), "plan version fee exact")
    check(has("invoiced", text(p)), "billing period invoiced")
    # invoices
    go(p, "#com-invoices", "text=Invoices")
    p.wait_for_selector(f"text={IDS['invoice_paid_number']}")
    check(p.locator("tbody tr").count() == 28, "all 28 invoices fit one page of 50")
    go(p, "#com-invoices?status=paid")
    p.wait_for_function("document.querySelectorAll('tbody tr').length === 1")
    check(has(IDS["invoice_paid_number"], text(p)), "status filter applied by the server")
    p.locator("tbody tr a:has-text('View')").first.click()
    p.wait_for_selector("text=Invoice lines")
    p.wait_for_selector("text=goodwill discount")
    body = text(p)
    check(has("1,200.00 EUR", body) or has("1.200,00 EUR", body), "invoice total exact")
    check(has("Settlement", body) and has("INV-PAY-77", body), "settlement panel with the payment")
    check(
        has("goodwill discount", body) and (has("1,150.00 EUR", body) or has("1.150,00 EUR", body)),
        "credit note and server-computed net amount",
    )
    check(has("Platform", body) or has("Billed to", body), "issuer / bill-to snapshots")
    go(p, f"#com-invoices/{IDS['invoice_open']}", "text=No payment allocated")
    go(p, "#com-invoices?tab=credit-notes", "text=Credit notes")
    p.wait_for_selector("td:has-text('goodwill discount')")
    check(has("goodwill discount", text(p)), "credit note listed")
    # reconciliation + readiness
    go(p, "#com-recon", "text=Reconciliation result")
    p.wait_for_selector("text=Invoice settlement summary")
    p.wait_for_selector("text=Reconciled keys")
    go(p, "#com-ready", "text=Financial readiness")
    p.wait_for_selector("text=Billing readiness")
    p.wait_for_function("document.querySelectorAll('.badge').length > 3")
    check(has("Pricing readiness", text(p)), "three readiness sources")
    # zero write controls on every commercial page; only GETs sent
    for h in (
        "#com",
        "#com-credit",
        "#com-payments",
        "#com-pricing",
        "#com-subs",
        "#com-invoices",
        "#com-recon",
        "#com-ready",
    ):
        go(p, h)
        p.wait_for_timeout(400)
        names = " | ".join(p.locator("main button:visible").all_inner_texts())
        check(
            not re.search(
                r"^(approve|reject|void|issue|publish|assign|resume|save|allocate|reverse|adjust|create)",
                names,
                re.I,
            ),
            f"no financial write control on {h}: {names}",
        )
    check(mutating == [], f"no non-GET request: {mutating}")
    # responsive
    p.set_viewport_size({"width": 390, "height": 800})
    for h in (
        "#com",
        "#com-credit",
        "#com-payments",
        "#com-pricing",
        "#com-subs",
        "#com-invoices",
        f"#com-invoices/{IDS['invoice_paid']}",
        "#com-recon",
        "#com-ready",
    ):
        go(p, h)
        p.wait_for_timeout(500)
        check(
            not p.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"),
            f"no horizontal scroll at 390px on {h}",
        )
    p.context.close()
    # operator: i njëjti lexim, pa shkrim
    q = new_page(b)
    login(q, "operator@e2e.test", OP_PW)
    ops_seen = []
    q.on(
        "request",
        lambda r: (
            ops_seen.append(r.method) if r.method != "GET" and "/auth/" not in r.url else None
        ),
    )
    go(q, "#com-invoices", "text=Invoices")
    q.wait_for_selector(f"text={IDS['invoice_paid_number']}")
    go(q, f"#com-invoices/{IDS['invoice_paid']}", "text=Invoice lines")
    check(has("Settlement", text(q)), "operator reads the invoice detail")
    go(q, "#com-payments", "text=Credit payments")
    check(has("Read-only access", q.inner_text("body")), "operator is told it is read-only")
    check(
        not q.get_by_role(
            "button", name=re.compile(r"^(approve|reject|grant credit|new grant|void|issue)", re.I)
        ).count(),
        "operator sees no financial write control",
    )
    check(ops_seen == [], "operator sent no mutation")
    q.context.close()


def commercial_e2_flow(b):
    """M11-E2: filtra faturash (numër, data, faqosje), treguesi credit-note, PDF zyrtar nga serveri; admin + operator, vetëm GET."""
    import io

    from pypdf import PdfReader

    p = new_page(b)
    login(p, "admin@e2e.test", ADMIN_PW)
    writes = []
    p.on(
        "request",
        lambda r: (
            writes.append(f"{r.method} {r.url}")
            if r.method not in ("GET", "HEAD") and "/auth/" not in r.url
            else None
        ),
    )
    pdf_resp = []
    p.on("response", lambda r: pdf_resp.append(r) if r.url.endswith("/pdf") else None)
    go(p, "#com-invoices?limit=25", "text=Invoices")
    p.wait_for_selector("tbody tr")
    check(p.locator("tbody tr").count() == 25, "first page has 25 invoices (28 in total)")
    p.get_by_role("button", name="Next").click()
    p.wait_for_function("document.querySelectorAll('tbody tr').length === 3")
    check(
        "offset=25" in p.evaluate("location.hash"), "next page is a server offset kept in the URL"
    )
    # numër: prefiks dhe numër i plotë
    num = IDS["invoice_paid_number"]
    go(p, f"#com-invoices?number={num}")
    p.wait_for_function("document.querySelectorAll('tbody tr').length === 1")
    check(has(num, text(p)), "exact invoice number filter")
    go(p, "#com-invoices?number=INV-")
    p.wait_for_function("document.querySelectorAll('tbody tr').length === 28")
    go(p, "#com-invoices?number=NOPE")
    p.wait_for_selector("text=Nothing matches your filters")
    # data: dita e acme/globex (të dyja lëshuar në të njëjtën ditë UTC) dhe e vjetra
    day = IDS["acme_issue_day"]
    go(p, f"#com-invoices?issued_from={day}&issued_to={day}")
    p.wait_for_function("document.querySelectorAll('tbody tr').length === 2")
    check(
        has(IDS["invoice_paid_number"], text(p)) and has(IDS["invoice_open_number"], text(p)),
        "single-day range includes both invoices issued that day (inclusive bounds)",
    )
    go(p, f"#com-invoices?issued_from={IDS['bulk_issue_day']}&issued_to={IDS['bulk_issue_day']}")
    p.wait_for_function("document.querySelectorAll('tbody tr').length === 1")
    go(p, f"#com-invoices?issued_from={day}&status=paid")
    p.wait_for_selector(f"td:has-text('{num}')")
    p.wait_for_function("document.querySelectorAll('tbody tr').length === 1")
    check(has(num, text(p)), "filters compose (date + status)")
    # interval i pavlefshëm: gabim në fushë, pa kërkesë
    go(p, "#com-invoices", "text=Invoices")
    p.wait_for_selector("tbody tr")
    p.get_by_label("Issued from").fill("2030-03-10")
    p.get_by_label("Issued to").fill("2030-03-01")
    p.get_by_role("button", name="Apply filters").click()
    p.wait_for_selector("text=The end date must not be before the start date")
    check(
        "issued_from" not in p.evaluate("location.hash"), "inverted range is not applied or swapped"
    )
    # tregues credit-note
    go(p, f"#com-invoices?number={num}")
    p.wait_for_selector(f"td:has-text('{num}')")
    check(has("credited", text(p)) and has("1 ·", text(p)), "credit-note indicator on the list")
    # PDF zyrtar
    p.locator("tbody tr a:has-text('View')").first.click()
    p.wait_for_selector("text=Invoice lines")
    with p.expect_download() as dl:
        p.get_by_role("button", name=re.compile("Download official PDF")).click()
    d = dl.value
    check(
        d.suggested_filename == f"invoice-{num}.pdf",
        f"safe filename from the server: {d.suggested_filename}",
    )
    raw = open(d.path(), "rb").read()
    check(raw.startswith(b"%PDF-"), "downloaded file is a PDF")
    t = "\n".join(pg.extract_text() for pg in PdfReader(io.BytesIO(raw)).pages)
    check(
        num in t
        and "goodwill discount" in t
        and "1,200.00" in t.replace(" ", "")
        or "1,200.00" in t,
        "PDF carries the invoice number, total and credit note",
    )
    check(
        any(r.headers.get("content-type") == "application/pdf" for r in pdf_resp),
        "HTTP response is application/pdf",
    )
    # responsive
    p.set_viewport_size({"width": 390, "height": 800})
    go(p, "#com-invoices")
    p.wait_for_selector("tbody tr")
    check(
        not p.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"),
        "no horizontal scroll at 390px (invoice filters)",
    )
    check(writes == [], f"no non-GET request: {writes}")
    p.context.close()
    # operator
    q = new_page(b)
    login(q, "operator@e2e.test", OP_PW)
    ow = []
    q.on(
        "request",
        lambda r: ow.append(r.method) if r.method != "GET" and "/auth/" not in r.url else None,
    )
    go(q, f"#com-invoices/{IDS['invoice_paid']}", "text=Invoice lines")
    with q.expect_download() as dl2:
        q.get_by_role("button", name=re.compile("Download official PDF")).click()
    check(
        open(dl2.value.path(), "rb").read().startswith(b"%PDF-"),
        "operator downloads the official PDF",
    )
    check(ow == [], "operator sent no mutation")
    q.context.close()


if __name__ == "__main__":
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=CHROME)
        if os.environ.get("E2E_ONLY") == "commercial":
            commercial_flow(b)
            commercial_e2_flow(b)
        else:
            admin_flow(b)
            operator_flow(b)
            ops_flow(b)
            ops_enterprise_flow(b)
            commercial_flow(b)
            commercial_e2_flow(b)
        b.close()
    # skenarët negativë të qëllimshëm: hyrje e gabuar, token i prishur, 404 për sender-in/rrugën e panjohur, shkrim i operatorit
    uniq = sorted(set(re.sub(r"/[0-9a-f-]{36}", "/<id>", e) for e in http_errors))
    expected = [
        "401 GET /auth/me",
        "401 POST /auth/token",
        "403 POST /admin/sender-policies",
        "404 GET /admin/senders/<id>",
        "404 GET /admin/senders/<id>/history",
    ]
    check(uniq == expected, f"only the intended negative responses occurred: {uniq}")
    print(f"{passed} checks passed")
    print("console/page errors:", errors or "none")
    raise SystemExit(1 if errors else 0)
