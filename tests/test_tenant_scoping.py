"""M1c: teste negative Enterprise A → Enterprise B për resurset e migruara në skopim me
`enterprise_id`. Çdo test provon 404 pa metadata të A dhe, veç kësaj, që kërkesat e klientit NUK
kalojnë nga rruga legacy (`scope.LEGACY_READS` mbetet bosh): skopimi është me enterprise_id.

  M1c-b: contacts, contact lists, consent, templates, sender IDs (kërkesa)
  M1c-c: messages, ledger/balance, campaigns, API keys, webhooks, reports, ... (shtohen më poshtë)"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core import scope
from app.main import create_app
from app.models.contacts import Contact, ContactList
from app.models.messaging import Template
from tests.test_pipeline import world  # noqa: F401

BOOT = {"X-Admin-Key": "test-key"}
PA, PB = "+355691230003", "+355691230004"  # numra kontakti (A / B)
NA, NB = PA[1:], PB[1:]  # forma e normalizuar që kthen API


def key_headers(c, owner):
    r = c.post(
        "/v1/admin/api-keys",
        json={"name": owner, "role": "client", "owner_ref": owner},
        headers=BOOT,
    ).json()
    return {"Authorization": f"Bearer {r['key']}", "X-Admin-Key": ""}, r["id"]


@pytest.fixture
def ab(db):
    """Dy tenant-e me të dhëna në çdo burim të M1c-b, të krijuara VETËM përmes API-t."""
    c = TestClient(create_app())
    a, a_key = key_headers(c, "tenA")
    b, b_key = key_headers(c, "tenB")
    ids = {"a_key": a_key, "b_key": b_key}
    for who, h, phone, first in (("a", a, PA, "Alice"), ("b", b, PB, "Bob")):
        ct = c.post("/v1/contacts", json={"phone": phone, "first_name": first}, headers=h).json()
        ls = c.post("/v1/lists", json={"name": f"L-{who}"}, headers=h).json()
        assert (
            c.post(
                f"/v1/lists/{ls['id']}/members", json={"contact_ids": [ct["id"]]}, headers=h
            ).status_code
            == 200
        )
        c.post(
            "/v1/consent",
            json={
                "channel": "sms",
                "address": phone,
                "action": "opt_in",
                "source": "t",
                "evidence": "form v1",
            },
            headers=h,
        )
        t = c.post(
            "/v1/templates",
            json={
                "owner_ref": f"ten{who.upper()}",
                "name": "welcome",
                "body": f"Hi {{{{first_name}}}} from {first}",
            },
            headers=h,
        ).json()
        assert (
            c.post(f"/v1/template-versions/{t['id']}/approve", json={}, headers=BOOT).status_code
            == 200
        )
        ids[who] = {"contact": ct["id"], "list": ls["id"], "template": t["template_id"]}
    scope.LEGACY_READS.clear()
    yield c, a, b, ids
    assert dict(scope.LEGACY_READS) == {}, "kërkesa e klientit përdori rrugën legacy owner_ref"


def leak(r, *secrets):
    """404 (ose lista bosh) dhe asnjë metadatë e A në përgjigje."""
    text = r.text
    return [s for s in secrets if s and str(s) in text]


# --- Contacts ----------------------------------------------------------------------------------


def test_contacts_get_update_delete_export_cross_tenant(ab):
    c, _, b, ids = ab
    cid = ids["a"]["contact"]
    for method, path, body in (
        ("GET", f"/v1/contacts/{cid}", None),
        ("PATCH", f"/v1/contacts/{cid}", {"first_name": "Hacked"}),
        ("DELETE", f"/v1/contacts/{cid}", None),
        ("GET", f"/v1/contacts/{cid}/export", None),
    ):
        r = c.request(method, path, headers=b, json=body)
        assert r.status_code == 404, (method, path, r.status_code)
        assert leak(r, "Alice", NA, "tenA") == []


def test_contacts_a_data_untouched_after_b_attempts(ab, db):
    c, a, b, ids = ab
    cid = ids["a"]["contact"]
    c.patch(f"/v1/contacts/{cid}", json={"first_name": "Hacked"}, headers=b)
    c.delete(f"/v1/contacts/{cid}", headers=b)
    got = c.get(f"/v1/contacts/{cid}", headers=a).json()
    assert got["first_name"] == "Alice" and got["phone"] == NA


def test_contacts_list_filters_search_pagination_never_cross(ab):
    c, _, b, ids = ab
    mine = c.get("/v1/contacts", headers=b).json()
    assert [x["phone"] for x in mine] == [NB]
    assert c.get("/v1/contacts", params={"q": "Alice"}, headers=b).json() == []  # kërkim
    assert c.get("/v1/contacts", params={"q": PA[1:]}, headers=b).json() == []
    assert c.get("/v1/contacts", params={"list_id": ids["a"]["list"]}, headers=b).json() == []
    assert c.get("/v1/contacts", params={"list_id": ids["b"]["list"]}, headers=b).json()
    for after in (0, ids["a"]["contact"] - 1, ids["b"]["contact"] - 1):  # faqosje
        page = c.get("/v1/contacts", params={"after_id": after, "limit": 1}, headers=b).json()
        assert all(x["phone"] == NB for x in page)
    assert c.get("/v1/contacts", params={"owner_ref": "tenA"}, headers=b).status_code == 404


def test_contacts_create_import_cannot_target_other_tenant(ab):
    c, _, b, _ = ab
    assert c.post("/v1/contacts", json={"owner_ref": "tenA", "phone": "+355691230999"},
                  headers=b).status_code == 404  # fmt: skip
    assert c.post("/v1/contacts/import", json={"owner_ref": "tenA", "contacts": [{"phone": PA}]},
                  headers=b).status_code == 404  # fmt: skip


def test_same_phone_in_two_tenants_are_independent_contacts(ab, db):
    c, a, b, _ = ab
    r = c.post("/v1/contacts", json={"phone": PA, "first_name": "Bee"}, headers=b)
    assert r.status_code == 201 and r.json()["first_name"] == "Bee"
    assert c.get(f"/v1/contacts/{r.json()['id']}", headers=a).status_code == 404
    rows = db.scalars(select(Contact).where(Contact.phone == NA)).all()
    assert len(rows) == 2 and len({x.enterprise_id for x in rows}) == 2


# --- Lists (+ nested) ----------------------------------------------------------------------------


def test_lists_cross_tenant(ab):
    c, _, b, ids = ab
    lid, cid = ids["a"]["list"], ids["a"]["contact"]
    assert [x["name"] for x in c.get("/v1/lists", headers=b).json()] == ["L-b"]
    for method, path, body in (
        ("POST", f"/v1/lists/{lid}/members", {"contact_ids": [cid]}),
        ("DELETE", f"/v1/lists/{lid}/members/{cid}", None),
        ("GET", f"/v1/lists/{lid}/audience?channel=sms", None),
    ):
        r = c.request(method, path, headers=b, json=body)
        assert r.status_code == 404 and leak(r, "L-a", "tenA") == [], (method, path)


def test_b_cannot_put_a_contact_into_its_own_list(ab, db):
    c, _, b, ids = ab
    r = c.post(
        f"/v1/lists/{ids['b']['list']}/members",
        json={"contact_ids": [ids["a"]["contact"]]},
        headers=b,
    )
    assert r.status_code == 404
    aud = c.get(f"/v1/lists/{ids['b']['list']}/audience", params={"channel": "sms"}, headers=b)
    assert aud.json()["eligible"] == 1  # vetëm Bob


def test_list_audience_counts_only_own(ab):
    c, a, _, ids = ab
    r = c.get(f"/v1/lists/{ids['a']['list']}/audience", params={"channel": "sms"}, headers=a)
    assert r.json() == {"eligible": 1, "excluded": {}}


# --- Consent --------------------------------------------------------------------------------------


def test_consent_state_is_per_enterprise(ab):
    c, a, b, _ = ab
    ca = c.get("/v1/consent/check", params={"channel": "sms", "address": PA}, headers=a).json()
    cb = c.get("/v1/consent/check", params={"channel": "sms", "address": PA}, headers=b).json()
    assert ca["allowed"] is True and cb["allowed"] is False  # opt-in i A nuk vlen për B
    assert c.get("/v1/consent/check", params={"channel": "sms", "address": PA, "owner_ref": "tenA"},
                 headers=b).status_code == 404  # fmt: skip


# --- Templates (nested) ----------------------------------------------------------------------------


def test_templates_cross_tenant(ab):
    c, a, b, ids = ab
    tid = ids["a"]["template"]
    ok = c.post(
        f"/v1/templates/{tid}/render",
        headers=a,
        json={"owner_ref": "tenA", "values": {"first_name": "Z"}},
    )
    assert ok.status_code == 200 and "Alice" in ok.text
    r = c.post(f"/v1/templates/{tid}/versions", headers=b, json={"body": "evil"})
    assert r.status_code == 404 and leak(r, "Alice", "tenA") == []
    r = c.post(
        f"/v1/templates/{tid}/render",
        headers=b,
        json={"owner_ref": "tenA", "values": {"first_name": "Z"}},
    )
    assert r.status_code == 404 and leak(r, "Alice", "tenA") == []
    # render me owner_ref-in e vet: s'gjendet → përgjigje identike me një ID që s'ekziston
    mine = c.post(
        f"/v1/templates/{tid}/render",
        headers=b,
        json={"owner_ref": "tenB", "values": {"first_name": "Z"}},
    )
    ghost = c.post(
        "/v1/templates/99999/render",
        headers=b,
        json={"owner_ref": "tenB", "values": {"first_name": "Z"}},
    )
    assert (mine.status_code, mine.json()) == (ghost.status_code, ghost.json())
    assert leak(mine, "Alice", "tenA") == []


def test_template_names_are_independent_across_tenants(ab, db):
    rows = db.scalars(select(Template).where(Template.name == "welcome")).all()
    assert len(rows) == 2 and len({t.enterprise_id for t in rows}) == 2


# --- Sender IDs (kërkesa) ---------------------------------------------------------------------------


def test_sender_request_cannot_target_other_tenant(ab):
    c, _, b, _ = ab
    r = c.post(
        "/v1/sender-ids", json={"owner_ref": "tenA", "country": "AL", "value": "EVIL"}, headers=b
    )
    assert r.status_code == 404 and leak(r, "tenA") == []
    ok = c.post(
        "/v1/sender-ids", json={"owner_ref": "tenB", "country": "AL", "value": "GOOD"}, headers=b
    )
    assert ok.status_code == 201


# --- Fail-closed ------------------------------------------------------------------------------------


def test_rows_without_enterprise_id_are_invisible_to_tenants(ab, db):
    c, a, _, ids = ab
    db.execute(Contact.__table__.update().values(enterprise_id=None))
    db.execute(ContactList.__table__.update().values(enterprise_id=None))
    db.commit()
    assert c.get("/v1/contacts", headers=a).json() == []
    assert c.get(f"/v1/contacts/{ids['a']['contact']}", headers=a).status_code == 404
    assert c.get("/v1/lists", headers=a).json() == []
    # rruga e stafit (owner_ref eksplicit) përdor po ashtu skopimin canonical: fail-closed
    scope.LEGACY_READS.clear()


def test_corrupt_row_with_other_tenants_enterprise_id_is_invisible_to_both(ab, db):
    c, a, b, ids = ab
    eb = db.scalar(select(Contact.enterprise_id).where(Contact.id == ids["b"]["contact"]))
    db.execute(
        Contact.__table__.update().where(Contact.id == ids["a"]["contact"]).values(enterprise_id=eb)
    )
    db.commit()
    assert c.get(f"/v1/contacts/{ids['a']['contact']}", headers=a).status_code == 404
    assert c.get(f"/v1/contacts/{ids['a']['contact']}", headers=b).status_code == 404


# ====================================================================================================
# M1c-c: messages, ledger/balance, campaigns, API keys, webhooks, reports, sender IDs, dashboard
# ====================================================================================================

import csv  # noqa: E402
import io  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402

from app.models.sending import AccountPlan, Message  # noqa: E402
from app.services import campaigns, net_guard, rates, sender_ids, webhooks  # noqa: E402
from app.services import contacts as contacts_svc  # noqa: E402
from app.services import messages as msgsvc  # noqa: E402
from app.services import wallet as wallets  # noqa: E402
from app.services.wallet import TopupMethod  # noqa: E402
from tests.test_pipeline import PAST  # noqa: E402

DST = {"a": "+355691230003", "b": "+355691230004"}


@pytest.fixture(autouse=True)
def _public_dns():
    old = net_guard.get_resolver()
    net_guard.set_resolver(lambda host: ["93.184.216.34"])
    yield
    net_guard.set_resolver(old)


def _funded_tenant(db, owner, sender, card_id):
    w = wallets.create_wallet(db, owner, "EUR")
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "10", TopupMethod.CASH).id)
    db.add(AccountPlan(owner_ref=owner, rate_card_id=card_id))
    s = sender_ids.request(db, owner, "AL", sender)
    sender_ids.approve(db, s.id, "admin")
    db.commit()
    return w, s


@pytest.fixture
def abc(db):
    """A: 2 mesazhe, B: 1. Secili me wallet të financuar, sender të miratuar, webhook, campaign, çelës."""
    import app.providers as providers
    from app.models.sending import Route
    from app.providers import FakeProvider

    providers._registry["fake"] = FakeProvider()
    card = rates.create_card(db, "std", "EUR")
    v = rates.new_draft(db, card.id)
    rates.set_rate(db, v.id, "355", "0.05")
    rates.publish(db, v.id, PAST, now=PAST - timedelta(days=1))
    db.add(Route(prefix="355", country="AL", provider="fake"))
    db.commit()
    c = TestClient(create_app())
    out = {"c": c}
    for who, owner, sender in (("a", "tenA", "SNDA"), ("b", "tenB", "SNDB")):
        h, key_id = key_headers(c, owner)
        w, s = _funded_tenant(db, owner, sender, card.id)
        n = 2 if who == "a" else 1
        mids = []
        for i in range(n):
            r = c.post(
                "/v1/messages",
                json={
                    "owner_ref": owner,
                    "to": DST[who].replace("4", "5") if i else DST[who],
                    "sender": sender,
                    "text": f"secret-{who}-{i}",
                },
                headers={**h, "Idempotency-Key": f"k-{who}-{i}"},
            )
            assert r.status_code == 202, r.text
            mids.append(r.json()["id"])
        ep, _ = webhooks.create_endpoint(db, owner, f"https://hooks.example.com/{who}", ["*"])
        lst = contacts_svc.create_list(db, owner, f"list-{who}")
        ct, _ = contacts_svc.upsert(db, owner, phone=DST[who], first_name=who.upper())
        contacts_svc.add_members(db, owner, lst.id, [ct.id])
        camp = campaigns.create(db, owner, f"camp-{who}", lst.id, sender, "tester", text="hello")
        db.commit()
        out[who] = {
            "h": h,
            "key_id": key_id,
            "wallet": w.id,
            "sender_id": s.id,
            "msgs": mids,
            "ep": ep.id,
            "list": lst.id,
            "camp": camp.id,
            "owner": owner,
        }
    while msgsvc.process_one(db) is not None:
        pass
    for m in db.scalars(select(Message)).all():
        msgsvc.apply_dlr(db, "fake", m.provider_message_id, delivered=True)
    db.commit()
    scope.LEGACY_READS.clear()
    yield out
    assert dict(scope.LEGACY_READS) == {}, "kërkesa e klientit përdori rrugën legacy owner_ref"


def _same_as_ghost(c, method, path_a, path_ghost, headers, body=None):
    """Përgjigja për burimin e A duhet të jetë e pandashme nga ajo për një ID që s'ekziston."""
    ra = c.request(method, path_a, headers=headers, json=body)
    rg = c.request(method, path_ghost, headers=headers, json=body)
    return ra.status_code, rg.status_code, ra.json() == rg.json()


# --- Messages ------------------------------------------------------------------------------------


def test_messages_get_and_events_cross_tenant_look_like_missing(abc):
    c, b, a = abc["c"], abc["b"]["h"], abc["a"]
    mid = a["msgs"][0]
    for suffix in ("", "/events"):
        sa, sg, same = _same_as_ghost(
            c, "GET", f"/v1/messages/{mid}{suffix}", f"/v1/messages/nope{suffix}", b
        )
        assert (sa, sg, same) == (404, 404, True), suffix
    assert c.get(f"/v1/messages/{mid}", headers=a["h"]).status_code == 200


def test_messages_list_filters_search_pagination_never_cross(abc):
    c, b = abc["c"], abc["b"]["h"]
    listing = c.get("/v1/messages", headers=b).json()
    assert len(listing["items"]) == 1 and listing["items"][0]["to"] == DST["b"][1:]
    assert c.get("/v1/messages", params={"q": DST["a"]}, headers=b).json()["items"] == []
    assert c.get("/v1/messages", params={"status": "delivered"}, headers=b).json()["items"]
    for before in (None, 1_000_000):
        p = {"limit": 1} | ({"before_id": before} if before else {})
        page = c.get("/v1/messages", params=p, headers=b).json()
        assert all("secret-a" not in str(x) for x in page["items"])
    assert c.get("/v1/messages", params={"owner_ref": "tenA"}, headers=b).status_code == 404


def test_messages_send_cannot_spoof_other_tenant(abc):
    c, b = abc["c"], abc["b"]["h"]
    r = c.post("/v1/messages", headers={**b, "Idempotency-Key": "spoof"},
               json={"owner_ref": "tenA", "to": DST["a"], "sender": "SNDA", "text": "x"})  # fmt: skip
    assert r.status_code == 404 and "tenA" not in r.text
    # sender i A nuk përdoret nga B as me owner_ref-in e vet
    r = c.post("/v1/messages", headers={**b, "Idempotency-Key": "spoof2"},
               json={"owner_ref": "tenB", "to": DST["a"], "sender": "SNDA", "text": "x"})  # fmt: skip
    assert r.status_code in (403, 422)


def test_idempotency_keys_are_per_enterprise(abc, db):
    c, a, b = abc["c"], abc["a"]["h"], abc["b"]["h"]
    r = c.post("/v1/messages", headers={**b, "Idempotency-Key": "k-a-0"},
               json={"owner_ref": "tenB", "to": DST["b"], "sender": "SNDB", "text": "mine"})  # fmt: skip
    assert r.status_code == 202 and r.json()["id"] not in abc["a"]["msgs"]  # fmt: skip
    assert c.get(f"/v1/messages/{r.json()['id']}", headers=a).status_code == 404


# --- Ledger / balance / wallets --------------------------------------------------------------------


def test_wallet_balance_ledger_topups_alert_cross_tenant(abc):
    c, b, wid = abc["c"], abc["b"]["h"], abc["a"]["wallet"]
    for method, suffix, body in (("GET", "", None), ("GET", "/ledger", None),
                                 ("GET", "/topups", None), ("PUT", "/alert", {"threshold": "1"})):  # fmt: skip
        sa, sg, same = _same_as_ghost(
            c, method, f"/v1/wallets/{wid}{suffix}", f"/v1/wallets/99999{suffix}", b, body
        )
        assert (sa, sg, same) == (404, 404, True), (method, suffix)


def test_wallet_list_and_ledger_pagination_only_own(abc):
    c, b = abc["c"], abc["b"]["h"]
    mine = c.get("/v1/wallets", headers=b).json()
    assert [w["id"] for w in mine] == [abc["b"]["wallet"]]
    led = c.get(f"/v1/wallets/{abc['b']['wallet']}/ledger", params={"limit": 1}, headers=b).json()
    assert len(led) == 1
    assert c.get("/v1/wallets", params={"owner_ref": "tenA"}, headers=b).status_code == 404


def test_balance_of_a_is_unchanged_by_b_activity(abc, db):
    c, a, b = abc["c"], abc["a"], abc["b"]
    before = c.get(f"/v1/wallets/{a['wallet']}", headers=a["h"]).json()
    c.post("/v1/messages", headers={**b["h"], "Idempotency-Key": "more"},
           json={"owner_ref": "tenB", "to": DST["b"], "sender": "SNDB", "text": "y"})  # fmt: skip
    assert c.get(f"/v1/wallets/{a['wallet']}", headers=a["h"]).json() == before


# --- Campaigns -------------------------------------------------------------------------------------


def test_campaign_all_operations_cross_tenant_and_list(abc):
    c, b, camp = abc["c"], abc["b"]["h"], abc["a"]["camp"]
    for method, suffix, body in (
        ("GET", "", None), ("GET", "/estimate", None), ("GET", "/recipients", None),
        ("POST", "/pause", None), ("POST", "/resume", None), ("POST", "/cancel", None),
        ("POST", "/schedule", {"scheduled_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()}),
    ):  # fmt: skip
        sa, sg, same = _same_as_ghost(
            c, method, f"/v1/campaigns/{camp}{suffix}", f"/v1/campaigns/99999{suffix}", b, body
        )
        assert (sa, sg, same) == (404, 404, True), (method, suffix)
    assert [x["id"] for x in c.get("/v1/campaigns", headers=b).json()] == [abc["b"]["camp"]]


def test_campaign_cannot_use_other_tenants_list_or_sender(abc):
    c, b, a = abc["c"], abc["b"]["h"], abc["a"]
    base = {"owner_ref": "tenB", "name": "steal", "text": "x", "channel": "sms"}
    r = c.post("/v1/campaigns", headers=b, json=base | {"list_id": a["list"], "sender": "SNDB"})
    assert r.status_code == 404 and "list-a" not in r.text  # lista e A
    r = c.post(
        "/v1/campaigns", headers=b, json=base | {"list_id": abc["b"]["list"], "sender": "SNDA"}
    )
    assert r.status_code == 201  # sender-i kontrollohet kur planifikohet
    when = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    sched = c.post(
        f"/v1/campaigns/{r.json()['id']}/schedule", headers=b, json={"scheduled_at": when}
    )
    assert sched.status_code in (403, 422) and "tenA" not in sched.text  # sender i A nuk vlen


# --- API keys --------------------------------------------------------------------------------------


def test_api_keys_cross_tenant(abc):
    c, b, kid = abc["c"], abc["b"]["h"], abc["a"]["key_id"]
    for action in ("rotate", "revoke"):
        sa, sg, same = _same_as_ghost(
            c, "POST", f"/v1/portal/api-keys/{kid}/{action}",
            f"/v1/portal/api-keys/99999/{action}", b,
        )  # fmt: skip
        assert (sa, sg, same) == (404, 404, True), action
    mine = c.get("/v1/portal/api-keys", headers=b).json()
    assert {k["id"] for k in mine} == {abc["b"]["key_id"]}
    assert (
        c.get(f"/v1/messages/{abc['a']['msgs'][0]}", headers=abc["a"]["h"]).status_code == 200
    )  # A ok


def test_client_cannot_create_key_for_other_tenant(abc, db):
    c, b = abc["c"], abc["b"]["h"]
    r = c.post("/v1/portal/api-keys", headers=b, json={"name": "mine"})
    assert r.status_code == 201
    from app.models.admin import ApiKey

    k = db.scalar(select(ApiKey).where(ApiKey.id == r.json()["id"]))
    assert k.owner_ref == "tenB" and k.enterprise_id == db.scalar(
        select(ApiKey.enterprise_id).where(ApiKey.id == abc["b"]["key_id"])
    )


# --- Webhooks --------------------------------------------------------------------------------------


def test_webhook_endpoints_cross_tenant(abc):
    c, b, ep = abc["c"], abc["b"]["h"], abc["a"]["ep"]
    for method, suffix, body in (("PATCH", "", {"description": "x"}), ("DELETE", "", None),
                                 ("POST", "/rotate-secret", None), ("POST", "/test", None)):  # fmt: skip
        sa, sg, same = _same_as_ghost(
            c, method, f"/v1/webhooks/endpoints/{ep}{suffix}",
            f"/v1/webhooks/endpoints/99999{suffix}", b, body,
        )  # fmt: skip
        assert (sa, sg) == (404, 404), (method, suffix)
        assert same, (method, suffix)
    assert c.get("/v1/webhooks/endpoints", headers=b).json()[0]["id"] == abc["b"]["ep"]
    assert len(c.get("/v1/webhooks/endpoints", headers=b).json()) == 1


def test_webhook_deliveries_events_and_redeliver_only_own(abc, db):
    from app.models.events import WebhookDelivery

    c, b = abc["c"], abc["b"]["h"]
    a_delivery = db.scalar(
        select(WebhookDelivery.id).where(WebhookDelivery.endpoint_id == abc["a"]["ep"])
    )
    assert a_delivery is not None
    dl = c.get("/v1/webhooks/deliveries", headers=b).json()
    assert dl and all(d["endpoint_id"] == abc["b"]["ep"] for d in dl)
    assert c.post(f"/v1/webhooks/deliveries/{a_delivery}/redeliver", headers=b).status_code == 404
    ev = c.get("/v1/events", headers=b).json()
    assert ev and all("secret-a" not in str(e) and "tenA" not in str(e) for e in ev)


# --- Reports / exports / aggregates / dashboard -------------------------------------------------------


def test_reports_usage_and_csv_only_own(abc):
    c, b = abc["c"], abc["b"]["h"]
    today = datetime.now(UTC).date()
    u = c.get("/v1/reports/usage", params={"from": str(today)}, headers=b).json()
    assert u["totals"]["sms"]["count"] == 1  # A ka 2
    csv_a = c.get("/v1/reports/messages.csv", headers=abc["a"]["h"]).text
    csv_b = c.get("/v1/reports/messages.csv", headers=b).text
    rows_a = list(csv.reader(io.StringIO(csv_a.lstrip("﻿"))))
    rows_b = list(csv.reader(io.StringIO(csv_b.lstrip("﻿"))))
    assert len(rows_a) == 3 and len(rows_b) == 2
    a_ids = {r[0] for r in rows_a[1:]}
    assert a_ids.isdisjoint({r[0] for r in rows_b[1:]})
    assert "SNDA" not in csv_b
    for path in ("/v1/reports/usage", "/v1/reports/messages.csv", "/v1/reports/emails.csv"):
        assert c.get(path, params={"owner_ref": "tenA"}, headers=b).status_code == 404


def test_dashboard_metrics_and_onboarding_only_own(abc):
    c, b = abc["c"], abc["b"]["h"]
    o = c.get("/v1/portal/overview", headers=b).json()
    assert o["sms_last_30d"] == {"delivered": 1}
    assert [w["id"] for w in o["wallets"]] == [abc["b"]["wallet"]]
    assert o["webhooks"]["active_endpoints"] == 1 and len(o["campaigns"]) == 1
    assert c.get("/v1/portal/overview", params={"owner_ref": "tenA"}, headers=b).status_code == 404
    onb = c.get("/v1/portal/onboarding", headers=b).json()
    assert {s["id"]: s["done"] for s in onb["steps"]}["message"] is True
    a_over = c.get("/v1/portal/overview", headers=abc["a"]["h"]).json()
    assert a_over["sms_last_30d"] == {"delivered": 2}


# --- Sender IDs -------------------------------------------------------------------------------------


def test_sender_ids_list_only_own_and_staff_queue_is_audited(abc, db):
    from app.models.admin import AuditLog

    c, b = abc["c"], abc["b"]["h"]
    mine = c.get("/v1/sender-ids", headers=b).json()
    assert {s["value"] for s in mine} == {"SNDB"} and all(s["owner_ref"] == "tenB" for s in mine)
    boot = c.get("/v1/sender-ids", headers=BOOT)  # SYSTEM: ndër-tenant, i shprehur
    assert {s["owner_ref"] for s in boot.json()} == {"tenA", "tenB"}
    row = db.scalar(select(AuditLog).where(AuditLog.action == "cross_tenant.list"))
    assert row is not None and row.target_type == "sender_ids" and row.actor == "bootstrap"
    only_a = c.get("/v1/sender-ids", params={"owner_ref": "tenA"}, headers=BOOT).json()
    assert {s["owner_ref"] for s in only_a} == {"tenA"}


def test_client_cannot_review_or_touch_other_tenants_sender(abc):
    c, b, sid = abc["c"], abc["b"]["h"], abc["a"]["sender_id"]
    for action in ("approve", "reject", "revoke"):
        assert c.post(f"/v1/sender-ids/{sid}/{action}", headers=b, json={}).status_code in (
            401,
            403,
        )


# --- Billing profile ----------------------------------------------------------------------------------


def test_billing_profile_is_per_enterprise(abc):
    c, a, b = abc["c"], abc["a"]["h"], abc["b"]["h"]
    body = {"legal_name": "A Ltd", "address": "Tirana", "country": "AL", "email": "a@a.al"}
    assert c.put("/v1/billing/profile", json=body, headers=a).status_code == 200
    got_b = c.get("/v1/billing/profile", headers=b)
    assert got_b.status_code == 200 and got_b.json() is None
    assert "A Ltd" not in c.get("/v1/billing/invoices", headers=b).text
