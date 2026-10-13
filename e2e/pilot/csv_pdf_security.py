"""M11-H · kalime master: siguria e CSV (formula), PDF (faturë, snapshot, Unicode, ndër-tenant), privatësia/sekretet (përgjigje, log, shfletues).

. $PILOT_DIR/pilot.env && python -m e2e.pilot.csv_pdf_security
"""

import csv
import io
import json
import os
import re
import subprocess
import time

import httpx
import pypdf
from playwright.sync_api import sync_playwright

from e2e.pilot import lib
from e2e.pilot.lib import IDS, check, wait

A, ta = lib.tenant("a")
B, tb = lib.tenant("b")
admin, operator = lib.central("PILOT_ADMIN_PASSWORD"), lib.central("PILOT_OPERATOR_PASSWORD")
finance = lib.staff("finance")
STACK = f"{os.path.dirname(__file__)}/stack.sh"
ENVF = os.path.join(lib.PILOT_DIR, "pilot.env")
LEAD = {"=": "=1+1", "+": "+1+1", "-": "-1+1", "@": "@SUM(1)", "TAB": "\tTAB", "CR": "\rCR"}


def cells(text):
    return list(csv.reader(io.StringIO(text.lstrip("﻿"))))


def formula_cell(c):
    return bool(c) and c[0] in "=+-@\t\r" and not re.match(r"^\+?\d[\d.]*$", c)


print("CSV · formula-leading values (= + - @ TAB CR) vs legitimate phone numbers")
made = {}
for k, lead in LEAD.items():
    name = f"{lead} camp-{k}-{int(time.time()) % 100000}"
    st, c = A.post(
        "/v1/campaigns",
        {
            "owner_ref": "tenant-a",
            "name": name,
            "list_id": ta["list"],
            "sender": "PILOTA",
            "text": "csv test",
        },
    )
    made[k] = (st, name)
check(
    sum(1 for st, _ in made.values() if st == 201) >= 4,
    f"campaigns with formula-leading names created: {[(k, v[0]) for k, v in made.items()]}",
)
r = (
    A.raw("/v1/reports/campaigns.csv", **{"from": "2020-01-01", "to": time.strftime("%Y-%m-%d")})
    if False
    else A.raw("/v1/reports/campaigns.csv")
)
rows = cells(r.text)
check(
    r.status_code == 200 and r.content.startswith(b"\xef\xbb\xbf"), "campaigns.csv: 200, UTF-8 BOM"
)
names = [row[1] for row in rows[1:]]
for k, (st, nm) in made.items():
    if st == 201:
        check(
            ("'" + nm) in names, f"leading {k!r} is neutralised with an apostrophe in campaigns.csv"
        )
check(
    not any(formula_cell(c) for row in rows[1:] for c in row),
    "no cell of campaigns.csv starts with a formula character",
)
m = cells(A.raw("/v1/reports/messages.csv").text)
phones = [row[2] for row in m[1:]]
check(
    phones and all(p.startswith("+355") for p in phones),
    "legitimate phone numbers (+355…) are preserved unchanged in messages.csv",
)
st, camp_list = A.get("/v1/campaigns", limit=50)
done = (
    next(c["id"] for c in camp_list if c["status"] == "completed")
    if any(c["status"] == "completed" for c in camp_list)
    else None
)
if done:
    rc = cells(A.raw(f"/v1/reports/campaigns/{done}/recipients.csv").text)
    check(all(row[1].startswith("+355") for row in rc[1:]), "recipients.csv keeps +355… numbers")
au = cells(A.raw("/v1/reports/audit.csv").text)
check(not any(formula_cell(c) for row in au[1:] for c in row), "audit.csv: no formula-leading cell")
# Central: arsyet e vendimeve (tekst i lirë i adminit)
st, ents = admin.get("/admin/senders", limit=1)
eid = IDS["tenants"]["a"]["enterprise_id"]
reasons = {}
for i, (k, lead) in enumerate(LEAD.items()):
    st, s = admin.post(
        "/admin/senders",
        {"enterprise_id": eid, "country": "AL", "value": f"CSV{i}x{int(time.time()) % 100000}"},
    )
    if st == 201:
        admin.post(f"/admin/senders/{s['id']}/approve", {})
        st2, _ = admin.post(f"/admin/senders/{s['id']}/revoke", {"reason": f"{lead} bad actor"})
        reasons[k] = st2
check(
    sum(1 for v in reasons.values() if v == 200) >= 4,
    f"Central revoke reasons with formula leaders accepted: {reasons}",
)
raw = admin.c.get("/admin/audit/sender-decisions.csv", headers=admin.h).content.decode()
dec = cells(raw)
hdr = dec[0]
ri = hdr.index("reason")
check(raw.startswith("﻿"), "Central sender-decisions.csv: UTF-8 BOM")
check(
    not any(formula_cell(row[ri]) for row in dec[1:]),
    "Central sender-decisions.csv: no reason cell starts with a formula character",
)
check(
    any(row[ri].startswith("'") for row in dec[1:]),
    "…and the leading characters were neutralised with an apostrophe (not dropped)",
)
ac = cells(admin.c.get("/admin/audit.csv", headers=admin.h).content.decode())
check(
    not any(formula_cell(c) for row in ac[1:] for c in row[3:7]),
    "Central audit.csv: no formula-leading cell",
)

print("PDF · customer invoice, snapshot stability, Unicode, tenant boundary")


def pdf_text(api, path):
    r = api.raw(path)
    assert (
        r.status_code == 200
        and r.headers["content-type"].startswith("application/pdf")
        and r.content[:5] == b"%PDF-"
    ), (path, r.status_code)
    return "\n".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(r.content)).pages)


inv_a = ta["invoice"]
t1 = pdf_text(A, f"/v1/billing/invoices/{inv_a}/pdf")
st, d = A.get(f"/v1/billing/invoices/{inv_a}")
check(
    ta["invoice_number"] in t1 and "Tenant A Sh.p.k." in t1,
    "own invoice PDF: number and bill-to name from the persisted invoice",
)
check(
    re.sub(r"\.0+$", "", str(d["total"])).split(".")[0] in t1.replace(",", "."),
    f"PDF contains the persisted total ({d['total']})",
)
issuer0 = re.search(r"Your Company Ltd", t1)
check(
    issuer0 is not None,
    "issuer identity on the PDF comes from the invoice snapshot (default configuration at issue time)",
)
check(
    A.raw(f"/v1/billing/invoices/{tb['invoice']}/pdf").status_code == 404,
    "tenant B's PDF is 404 for tenant A",
)
env_txt = open(ENVF).read()
try:
    open(ENVF, "a").write("\nexport SMS_ISSUER_NAME='Changed Issuer Sh.a. Çmim'\n")
    subprocess.run(
        [STACK, "restart-ent"], check=True, capture_output=True, stdin=subprocess.DEVNULL
    )
    A2 = lib.Api(lib.ENT, ta["key"])
    t2 = pdf_text(A2, f"/v1/billing/invoices/{inv_a}/pdf")
    check(
        "Your Company Ltd" in t2 and "Changed Issuer" not in t2,
        "snapshot-backed invoice is unchanged after the issuer configuration changed (no drift)",
    )
    st, upd = A2.req(
        "PUT",
        "/v1/billing/profile",
        json={
            "legal_name": "Müller & Söhne – Tiranë Sh.p.k.",
            "address": "Rruga e Durrësit 1, Tiranë",
            "country": "AL",
            "email": "a@example.test",
            "tax_id": "K1",
        },
    )
    check(st == 200, f"billing profile updated with Unicode legal name ({st})")
    code = (
        "from datetime import UTC, datetime, timedelta\n"
        "from app.core.db import SessionLocal\nfrom app.services import billing\nfrom sqlalchemy import text\n"
        "with SessionLocal() as db:\n"
        "    sid = db.execute(text(\"select id from sms_subscriptions where owner_ref='tenant-a' order by id desc limit 1\")).scalar()\n"
        "    inv = billing.generate_invoice(db, sid, datetime.now(UTC) + timedelta(days=75)); db.commit()\n"
        "    print(inv.id if inv else 'none')\n"
    )
    out = subprocess.run(
        [f"{lib.os.path.dirname(__file__)}/../../.venv/bin/python", "-c", code],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "SMS_ISSUER_NAME": "Changed Issuer Sh.a. Çmim"},
        cwd=lib.os.path.dirname(__file__) + "/../..",
    )
    new_id = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else "none"
    check(
        new_id.isdigit(),
        f"a new invoice for the next period was issued by the real billing service with the changed issuer configuration (id {new_id}) {out.stderr[-120:] if not new_id.isdigit() else ''}",
    )
    t3 = pdf_text(A2, f"/v1/billing/invoices/{new_id}/pdf")
    check(
        "Changed Issuer" in t3 and "Çmim" in t3,
        "the NEW invoice snapshots the new issuer (Unicode preserved in the PDF)",
    )
    check(
        "Müller & Söhne" in t3 and "Tiranë" in t3,
        "the NEW invoice carries the Unicode bill-to snapshot",
    )
    t1b = pdf_text(A2, f"/v1/billing/invoices/{inv_a}/pdf")
    check(
        "Tenant A Sh.p.k." in t1b and "Müller" not in t1b and "Your Company Ltd" in t1b,
        "the older invoice keeps its original issuer and bill-to snapshot",
    )
finally:
    open(ENVF, "w").write(env_txt)
    subprocess.run(
        [STACK, "restart-ent"], check=True, capture_output=True, stdin=subprocess.DEVNULL
    )

print("PDF · Central staff view")
st, invs = admin.get("/admin/billing/invoices", limit=5)
check(st == 200 and invs["items"], "Central lists invoices (commercial seed)")
iid = invs["items"][0]["id"]
r = admin.c.get(f"/admin/billing/invoices/{iid}/pdf", headers=admin.h)
check(r.status_code == 200 and r.content[:5] == b"%PDF-", "Central invoice PDF is a valid PDF")
txt = "\n".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(r.content)).pages)
check(invs["items"][0]["number"] in txt, "Central PDF carries the persisted invoice number")
check(
    operator.c.get(f"/admin/billing/invoices/{iid}/pdf", headers=operator.h).status_code == 200,
    "operator can read the PDF (read-only role)",
)
check(
    httpx.get(f"{lib.CEN}/admin/billing/invoices/{iid}/pdf").status_code == 401,
    "unauthenticated PDF → 401",
)
check(
    A.c.get(f"{lib.CEN}/admin/billing/invoices/{iid}/pdf", headers=A.h).status_code == 401,
    "an Enterprise customer key is not valid on Central",
)
st, miss = admin.get("/admin/billing/invoices/00000000-0000-4000-8000-000000000000/pdf")
check(st == 404, "unknown invoice → clean 404 (no traceback)")

print("Privacy / secrets scan")
SECRETS = {
    "tenant A key": ta["key"],
    "tenant B key": tb["key"],
    **{f"{r} key": k for r, k in IDS["keys"].items()},
    "admin password": os.environ["PILOT_ADMIN_PASSWORD"],
    "operator password": os.environ["PILOT_OPERATOR_PASSWORD"],
    "central auth secret": os.environ["CENTRAL_AUTH_SECRET"],
    "pii hmac key": os.environ["SMS_PII_HMAC_KEY"],
    "secrets key": os.environ["SMS_SECRETS_KEY"],
    "dlr secret": "pilot-dlr-secret",
    "admin api key": os.environ["SMS_ADMIN_API_KEY"],
}
PATTERNS = {
    "api key": r"sms_[0-9a-f]{8}_[A-Za-z0-9_\-]{20,}",
    "jwt": r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
    "pem": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "dsn": r"postgres(ql)?(\+\w+)?://[^\s\"']*:[^\s\"'@]+@",
    "password hash": r"\$(2[aby]|argon2\w*|pbkdf2\S*)\$|password_hash|scrypt\$",
}


def scan(label, text, allow=()):
    bad = [n for n, s in SECRETS.items() if s and s in text and n not in allow]
    bad += [n for n, p in PATTERNS.items() if re.search(p, text) and n not in allow]
    check(not bad, f"{label}: no secrets ({bad})")


ent_gets = [
    "/v1/me",
    "/v1/contacts?limit=50",
    "/v1/lists",
    "/v1/sender-ids",
    "/v1/campaigns",
    "/v1/messages?limit=50",
    "/v1/wallets",
    "/v1/billing/invoices",
    "/v1/billing/profile",
    "/v1/billing/subscription",
    "/v1/webhooks/endpoints",
    "/v1/portal/api-keys",
    "/v1/portal/overview",
    "/v1/reports/overview",
    "/v1/reports/audit",
    "/v1/reports/usage",
    "/v1/events",
    "/v1/templates",
    "/v1/inbox",
]
blob = "\n".join(f"### {p}\n{A.c.get(p, headers=A.h).text}" for p in ent_gets)
blob += "\n".join(
    A.raw(p).text
    for p in (
        "/v1/reports/messages.csv",
        "/v1/reports/campaigns.csv",
        "/v1/reports/audit.csv",
        "/v1/reports/usage.csv",
    )
)
scan("tenant A API responses + exports", blob, allow=("api key",)) if False else scan(
    "tenant A API responses + exports", blob
)
check(
    "@pilot.test" not in blob
    and "staff-person" not in blob
    and "finance" not in json.dumps(A.get("/v1/reports/audit")[1]),
    "no staff identity leaks into customer responses",
)
cen_gets = [
    "/admin/senders?limit=200",
    "/admin/sender-policies",
    "/admin/operations/sender",
    "/admin/audit?limit=200",
    "/admin/audit/sender-decisions?limit=200",
    "/admin/billing/invoices",
    "/admin/billing/subscriptions",
    "/admin/money/payments",
    "/admin/money/grants",
    "/admin/money/accounts",
    "/admin/money/reconciliation",
    "/admin/billing/plans",
    "/admin/settlement",
]
cb = "\n".join(f"### {p}\n{admin.c.get(p, headers=admin.h).text}" for p in cen_gets)
scan("Central admin API responses", cb)
logs = ""
for f in os.listdir(os.path.join(lib.PILOT_DIR, "logs")):
    if f.endswith(".log"):
        logs += open(os.path.join(lib.PILOT_DIR, "logs", f), errors="replace").read()
scan("server + worker logs", logs)
check("Bearer" not in logs, "no Authorization header values in logs")
with sync_playwright() as pw:
    b = pw.chromium.launch(executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
    ctx = b.new_context()
    ctx.add_init_script("localStorage.setItem('sms_lang','en')")
    p = ctx.new_page()
    urls = []
    p.on("request", lambda r: urls.append(r.url + (r.post_data or "")))
    p.goto("http://127.0.0.1:5173")
    p.evaluate("sessionStorage.clear()")
    p.reload()
    p.fill("input[type=password]", ta["key"])
    p.click("button:has-text('Sign in')")
    p.wait_for_selector("nav")
    for h in (
        "#dashboard",
        "#keys",
        "#developers",
        "#campaigns",
        "#reports",
        "#activity",
        "#billing",
        "#senders",
    ):
        p.evaluate(f"location.hash = '{h}'")
        p.wait_for_timeout(500)
        html = p.content()
        check(
            ta["key"] not in html and ta["key"] not in p.url,
            f"{h}: the API key is not in the DOM or the URL",
        )
    check(
        ta["key"] not in "".join(urls),
        "the key never travels in a URL or request body (Authorization header only)",
    )
    ls = p.evaluate("JSON.stringify({l: {...localStorage}})")
    check(ta["key"] not in ls, "the key is not persisted in localStorage")
    ctx.close()
    ctx = b.new_context()
    p = ctx.new_page()
    urls = []
    p.on("request", lambda r: urls.append(r.url + (r.post_data or "")))
    p.goto("http://127.0.0.1:5174")
    p.evaluate("sessionStorage.clear(); localStorage.setItem('central_lang','en')")
    p.reload()
    p.fill("input[type=email]", "admin@pilot.test")
    p.fill("input[type=password]", os.environ["PILOT_ADMIN_PASSWORD"])
    p.click("button:has-text('Sign in')")
    p.wait_for_selector("nav[aria-label='Main navigation']")
    tok = p.evaluate("sessionStorage.getItem('central_token')")
    for h in ("#dashboard", "#senders", "#ops", "#com", "#audit-staff"):
        p.evaluate(f"location.hash = '{h}'")
        p.wait_for_timeout(500)
        check(
            tok not in p.content()
            and tok not in p.url
            and os.environ["PILOT_ADMIN_PASSWORD"] not in p.content(),
            f"Central {h}: JWT/password not in the DOM or URL",
        )
    check(
        tok not in "".join(urls) and tok not in p.evaluate("JSON.stringify({...localStorage})"),
        "Central JWT is only in sessionStorage + Authorization header (not in URLs, bodies or localStorage)",
    )
    ctx.close()
    b.close()
print(f"{lib.passed} checks")
