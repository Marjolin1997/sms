"""Izolimi mes klientëve: çdo burim i c1 që adresohet me ID kthen 404 (jo 403, jo të dhëna)
për një çelës të c2. Rreshtat e krijuar këtu mbulojnë endpoint-et me {id} në rrugë."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models.sending import Message
from app.services import apikeys, campaigns, inbox, webhooks
from app.services import contacts as contacts_svc
from app.services import wallet as wallets
from tests.test_email import fake_dns, fake_email_provider, verified  # noqa: F401
from tests.test_email import send as esend
from tests.test_pipeline import send, world  # noqa: F401

BOOT = {"X-Admin-Key": "test-key"}


@pytest.fixture
def tenants(db, world, verified):  # noqa: F811
    c = TestClient(create_app())

    def bearer(owner):
        r = c.post("/v1/admin/api-keys", json={"name": owner, "role": "client", "owner_ref": owner},
                   headers=BOOT).json()  # fmt: skip
        return {"Authorization": f"Bearer {r['key']}", "X-Admin-Key": ""}, r["id"]

    a_h, a_key_id = bearer("c1")
    b_h, _ = bearer("c2")
    msg = send(db, key="m1")
    email = esend(db, key="e1")
    contact, _ = contacts_svc.upsert(db, "c1", phone="+355691230003", email="ana@example.com")
    lst = contacts_svc.create_list(db, "c1", "L")
    contacts_svc.add_members(db, "c1", lst.id, [contact.id])
    camp = campaigns.create(db, "c1", "Camp", lst.id, "ACME", "tester", text="hi {{first_name}}")
    ep, _ = (
        webhooks.create_endpoint(db, "c1", "https://example.com/h", ["*"])
        if hasattr(webhooks, "create_endpoint")
        else (None, None)
    )
    kw = inbox.set_keyword(db, "c1", "help", "call us")
    wallet = wallets.create_wallet(db, "c1", "EUR")
    db.commit()
    ids = {
        "msg": msg.public_id, "email": email.public_id, "contact": contact.id, "list": lst.id,
        "camp": camp.id, "ep": ep.id if ep else 0, "kw": kw.id, "wallet": wallet.id,
        "domain": verified.id, "key": a_key_id,
    }  # fmt: skip
    return c, a_h, b_h, ids


def test_owner_can_reach_their_own_resources(tenants):
    c, a, _, ids = tenants
    for path in [f"/v1/messages/{ids['msg']}", f"/v1/email/messages/{ids['email']}",
                 f"/v1/contacts/{ids['contact']}", f"/v1/campaigns/{ids['camp']}",
                 f"/v1/wallets/{ids['wallet']}"]:  # fmt: skip
        assert c.get(path, headers=a).status_code == 200, path


def test_other_tenant_gets_404_on_every_id_addressed_resource(tenants):
    c, _, b, ids = tenants
    m, e, ct, ls, cp, ep, kw, w, dm, key = (ids[k] for k in
        ("msg", "email", "contact", "list", "camp", "ep", "kw", "wallet", "domain", "key"))  # fmt: skip
    cases = [
        ("GET", f"/v1/messages/{m}"),
        ("GET", f"/v1/messages/{m}/events"),
        ("GET", f"/v1/email/messages/{e}"),
        ("GET", f"/v1/email/messages/{e}/events"),
        ("POST", f"/v1/email/domains/{dm}/verify"),
        ("GET", f"/v1/contacts/{ct}"),
        ("PATCH", f"/v1/contacts/{ct}"),
        ("DELETE", f"/v1/contacts/{ct}"),
        ("GET", f"/v1/contacts/{ct}/export"),
        ("GET", f"/v1/lists/{ls}/audience?channel=sms"),
        ("POST", f"/v1/lists/{ls}/members"),
        ("DELETE", f"/v1/lists/{ls}/members/{ct}"),
        ("GET", f"/v1/campaigns/{cp}"),
        ("GET", f"/v1/campaigns/{cp}/estimate"),
        ("GET", f"/v1/campaigns/{cp}/recipients"),
        ("POST", f"/v1/campaigns/{cp}/schedule"),
        ("POST", f"/v1/campaigns/{cp}/pause"),
        ("POST", f"/v1/campaigns/{cp}/resume"),
        ("POST", f"/v1/campaigns/{cp}/cancel"),
        ("GET", f"/v1/wallets/{w}"),
        ("GET", f"/v1/wallets/{w}/ledger"),
        ("GET", f"/v1/wallets/{w}/topups"),
        ("PUT", f"/v1/wallets/{w}/alert"),
        ("DELETE", f"/v1/keywords/{kw}"),
        ("POST", f"/v1/portal/api-keys/{key}/revoke"),
        ("POST", f"/v1/portal/api-keys/{key}/rotate"),
    ]
    if ep:
        cases += [("PATCH", f"/v1/webhooks/endpoints/{ep}"), ("DELETE", f"/v1/webhooks/endpoints/{ep}"),
                  ("POST", f"/v1/webhooks/endpoints/{ep}/rotate-secret"),
                  ("POST", f"/v1/webhooks/endpoints/{ep}/test")]  # fmt: skip
    bodies = {
        "POST": {"contact_ids": [ct]},
        "PUT": {"threshold": "1"},
        "PATCH": {"first_name": "X"},
    }
    leaks = []
    for method, path in cases:
        r = c.request(method, path, headers=b, json=bodies.get(method))
        if r.status_code != 404:
            leaks.append(f"{method} {path} -> {r.status_code}")
    assert leaks == []


def test_other_tenant_cannot_use_owner_ref_to_read_or_write(tenants):
    c, _, b, _ = tenants
    assert c.get("/v1/contacts", params={"owner_ref": "c1"}, headers=b).status_code == 404
    assert c.get("/v1/messages", params={"owner_ref": "c1"}, headers=b).status_code == 404
    assert (
        c.post(
            "/v1/contacts", json={"owner_ref": "c1", "phone": "+355691230777"}, headers=b
        ).status_code
        == 404
    )
    assert c.post("/v1/messages", json={"owner_ref": "c1", "to": "+355691230003", "sender": "ACME",
                  "text": "spoof"}, headers=b, ).status_code in (404, 422)  # fmt: skip


def test_lists_only_show_own_rows(tenants, db):
    c, a, b, _ = tenants
    assert len(c.get("/v1/contacts", headers=a).json()) == 1
    assert c.get("/v1/contacts", headers=b).json() == []
    assert c.get("/v1/messages", headers=b).json()["items"] == []
    assert c.get("/v1/email/messages", headers=b).json()["items"] == []
    assert c.get("/v1/campaigns", headers=b).json() == []
    assert db.query(Message).count() == 1


def test_apikeys_service_scopes_rotation(db):
    k, _ = apikeys.create_key(db, "a", "client", "c1", "t")
    db.commit()
    from app.services.wallet import NotFound

    with pytest.raises(NotFound):
        apikeys.rotate_own_key(db, "c2", k.id, "t")
