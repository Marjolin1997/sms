"""M11-H · matrica e izolimit ndër-tenant (API + eksporte), në të dyja drejtimet, kundër uvicorn REAL + PostgreSQL.

    . $PILOT_DIR/pilot.env && python -m e2e.pilot.iso_matrix

Për çdo burim të viktimës (kontakt, listë, sender, campaign, marrës, mesazh, faturë + PDF, wallet, webhook, çelës API, audit, raporte, CSV)
sulmuesi (tenant tjetër) duhet të marrë 404/403 — kurrë 200 me të dhëna të viktimës — dhe koleksionet e tij nuk përmbajnë shenjat e viktimës.
"""

import json
import re

from e2e.pilot import lib
from e2e.pilot.lib import IDS, check

TENANTS = {"a": lib.tenant("a"), "b": lib.tenant("b")}
DENY = (403, 404)
leaks: list[str] = []


def victim_resources(v_api, v_owner, v):
    """Burimet reale të viktimës, nga API-ja e vetë (jo hamendësuar)."""
    r = {}
    r["contact"] = v_api.get("/v1/contacts", limit=1)[1][0]["id"]
    r["list"] = v_api.get("/v1/lists")[1][0]["id"]
    r["sender"] = v_api.get("/v1/sender-ids")[1][0]["id"]
    camps = v_api.get("/v1/campaigns", limit=5)[1]
    r["campaign"] = camps[0]["id"] if camps else None
    r["recipients_campaign"] = next(
        (c["id"] for c in camps if c["status"] == "completed"), r["campaign"]
    )
    msgs = v_api.get("/v1/messages", limit=5)[1]["items"]
    r["message"] = msgs[0]["id"] if msgs else None
    r["invoice"] = v["invoice"]
    r["wallet"] = v["wallet"]
    st, ep = v_api.post(
        "/v1/webhooks/endpoints", {"owner_ref": v_owner, "url": "https://example.test/hook"}
    )
    r["webhook"] = ep["id"] if st == 201 else None
    st, key = v_api.post("/v1/portal/api-keys", {"name": "iso-probe"})
    r["apikey"] = key.get("id") if st == 201 and isinstance(key, dict) else None
    st, tpl = v_api.post(
        "/v1/templates", {"owner_ref": v_owner, "name": f"iso-{v_owner}", "body": "Hello {{name}}"}
    )
    r["template"] = tpl.get("template_id") if st == 201 else None
    return r


def probe(att, method, path, what, **kw):
    st, body = att.req(method, path, **kw)
    ok = st in DENY
    if not ok:
        leaks.append(f"{method} {path} → {st}")
    check(ok, f"{what}: {method} {path} → {st}")


def run(attacker_tag, victim_tag):
    att, ta = TENANTS[attacker_tag]
    vic, tv = TENANTS[victim_tag]
    V = victim_resources(vic, tv["owner_ref"], tv)
    print(
        f"attacker tenant-{attacker_tag} → victim tenant-{victim_tag}  resources={ {k: v for k, v in V.items() if v is not None} }"
    )
    marks = [
        tv["owner_ref"],
        tv["phone_prefix"],
        f"{victim_tag.upper()} customers",
        f"Tenant {victim_tag.upper()}",
        f"PILOT{victim_tag.upper()}",
        f"Pilot {victim_tag.upper()} campaign",
        tv["invoice_number"],
    ]
    # --- detail routes ---
    probe(att, "GET", f"/v1/contacts/{V['contact']}", "contact")
    probe(att, "GET", f"/v1/contacts/{V['contact']}/export", "contact export")
    probe(att, "DELETE", f"/v1/contacts/{V['contact']}", "contact erase")
    probe(att, "GET", f"/v1/lists/{V['list']}/audience", "list audience", params={"channel": "sms"})
    probe(
        att,
        "POST",
        f"/v1/lists/{V['list']}/members",
        "list add member",
        json={"contact_ids": [V["contact"]]},
    )
    probe(att, "GET", f"/v1/sender-ids/{V['sender']}", "sender")
    if V["campaign"]:
        for suffix, method in (
            ("", "GET"),
            ("/estimate", "GET"),
            ("/recipients", "GET"),
            ("/activity", "GET"),
            ("/schedule", "POST"),
            ("/pause", "POST"),
            ("/cancel", "POST"),
        ):
            probe(
                att,
                method,
                f"/v1/campaigns/{V['campaign']}{suffix}",
                "campaign" + suffix,
                **({"json": {}} if method == "POST" else {}),
            )
        probe(
            att,
            "GET",
            f"/v1/reports/campaigns/{V['recipients_campaign']}/recipients.csv",
            "campaign recipients CSV",
        )
        probe(
            att,
            "GET",
            "/v1/reports/failures",
            "failure report of a foreign campaign",
            params={"campaign_id": V["campaign"]},
        )
        probe(
            att,
            "GET",
            "/v1/reports/messages.csv",
            "messages CSV of a foreign campaign",
            params={"campaign_id": V["campaign"]},
        )
        st, body = att.get("/v1/messages", campaign_id=V["campaign"], limit=50)
        check(
            st in DENY or (st == 200 and body["items"] == []),
            f"messages filtered by a foreign campaign are 404/empty ({st})",
        )
    if V["message"]:
        probe(att, "GET", f"/v1/messages/{V['message']}", "message")
        probe(att, "GET", f"/v1/messages/{V['message']}/events", "message events")
    probe(att, "GET", f"/v1/billing/invoices/{V['invoice']}", "invoice")
    probe(att, "GET", f"/v1/billing/invoices/{V['invoice']}/html", "invoice HTML")
    probe(att, "GET", f"/v1/billing/invoices/{V['invoice']}/pdf", "invoice PDF")
    probe(
        att,
        "POST",
        f"/v1/billing/invoices/{V['invoice']}/pay-from-wallet",
        "invoice pay-from-wallet",
        json={},
    )
    probe(att, "GET", f"/v1/wallets/{V['wallet']}/ledger", "wallet ledger")
    probe(att, "GET", f"/v1/wallets/{V['wallet']}/topups", "wallet top-ups")
    probe(
        att,
        "POST",
        f"/v1/wallets/{V['wallet']}/topups",
        "wallet top-up",
        json={"amount": "5", "method": "cash"},
    )
    probe(att, "GET", f"/v1/wallets/{V['wallet']}", "wallet")
    if V["webhook"]:
        probe(att, "DELETE", f"/v1/webhooks/endpoints/{V['webhook']}", "webhook delete")
        probe(
            att,
            "POST",
            f"/v1/webhooks/endpoints/{V['webhook']}/rotate-secret",
            "webhook rotate",
            json={},
        )
    if V["apikey"]:
        probe(att, "POST", f"/v1/portal/api-keys/{V['apikey']}/revoke", "API key revoke", json={})
        probe(att, "POST", f"/v1/portal/api-keys/{V['apikey']}/rotate", "API key rotate", json={})
    # --- owner_ref parametër / trup ---
    probe(
        att,
        "GET",
        "/v1/contacts",
        "owner_ref query of another tenant",
        params={"owner_ref": tv["owner_ref"]},
    )
    probe(
        att,
        "GET",
        "/v1/reports/overview",
        "reports owner_ref of another tenant",
        params={"owner_ref": tv["owner_ref"]},
    )
    probe(
        att,
        "GET",
        "/v1/reports/audit",
        "audit owner_ref of another tenant",
        params={"owner_ref": tv["owner_ref"]},
    )
    probe(
        att,
        "GET",
        "/v1/reports/messages.csv",
        "CSV owner_ref of another tenant",
        params={"owner_ref": tv["owner_ref"]},
    )
    probe(
        att,
        "POST",
        "/v1/messages",
        "send with the other tenant's owner_ref",
        json={"owner_ref": tv["owner_ref"], "to": "355690000011", "sender": "X"},
        headers={"Idempotency-Key": "iso-1"},
    )
    probe(
        att,
        "POST",
        "/v1/contacts",
        "create contact in the other tenant",
        json={"owner_ref": tv["owner_ref"], "phone": "355690000099"},
    )
    # --- koleksionet e sulmuesit: asnjë shenjë e viktimës ---
    collections = {
        "contacts": att.get("/v1/contacts", limit=500),
        "lists": att.get("/v1/lists"),
        "senders": att.get("/v1/sender-ids"),
        "campaigns": att.get("/v1/campaigns", limit=200),
        "messages": att.get("/v1/messages", limit=200),
        "invoices": att.get("/v1/billing/invoices"),
        "wallets": att.get("/v1/wallets"),
        "webhooks": att.get("/v1/webhooks/endpoints"),
        "api keys": att.get("/v1/portal/api-keys"),
        "audit": att.get("/v1/reports/audit", limit=200),
        "overview": att.get("/v1/reports/overview"),
        "report campaigns": att.get("/v1/reports/campaigns", limit=200),
        "failures": att.get("/v1/reports/failures"),
        "usage": att.get("/v1/reports/usage"),
        "portal overview": att.get("/v1/portal/overview"),
    }
    for name, (st, body) in collections.items():
        text = json.dumps(body)
        hit = [m for m in marks if m in text]
        check(
            st == 200 and not hit,
            f"collection '{name}' has no trace of the other tenant (status {st}{', LEAK ' + str(hit) if hit else ''})",
        )
    for path in (
        "/v1/reports/messages.csv",
        "/v1/reports/emails.csv",
        "/v1/reports/campaigns.csv",
        "/v1/reports/usage.csv",
        "/v1/reports/audit.csv",
    ):
        r = att.raw(path)
        hit = [m for m in marks if m in r.text]
        check(
            r.status_code == 200 and not hit,
            f"export {path} has no trace of the other tenant ({r.status_code})",
        )
    # kontroll i kundërt: ata ende e shohin të vetën
    check(att.get("/v1/contacts", limit=1)[1], "attacker still sees its own contacts")


for a, v in (("a", "b"), ("b", "a")):
    run(a, v)
check(not leaks, f"no cross-tenant leak in any probe: {leaks}")
print(f"{lib.passed} checks")
