import pytest

from app.services import billing, sender_ids, templates
from app.services import contacts as contacts_svc
from app.services import wallet as wallets
from tests.test_email import fake_dns, fake_email_provider, verified  # noqa: F401
from tests.test_pipeline import OK, send, world  # noqa: F401

BOOT = {"X-Admin-Key": "test-key"}


@pytest.fixture
def raw_client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app())


def key(c, role, owner=None):
    r = c.post(
        "/v1/admin/api-keys", json={"name": "k", "role": role, "owner_ref": owner}, headers=BOOT
    )
    return {"Authorization": f"Bearer {r.json()['key']}"}


# --- Mesazhe ------------------------------------------------------------------------


def test_message_history_paging_filter_search_isolation(db, world, raw_client):  # noqa: F811
    c = raw_client
    h1, h2 = key(c, "client", "c1"), key(c, "client", "c2")
    numbers = ["+355691230003", "+355691239999", "+355691230004", "+355691230005", "+355691230006"]
    for i, n in enumerate(numbers):
        send(db, key=f"m{i}", to=n)
    page1 = c.get("/v1/messages", params={"limit": 2}, headers=h1).json()
    assert [m["to"] for m in page1["items"]] == ["355691230006", "355691230005"]
    assert page1["next_before_id"] is not None
    page2 = c.get(
        "/v1/messages", params={"limit": 2, "before_id": page1["next_before_id"]}, headers=h1
    ).json()
    assert [m["to"] for m in page2["items"]] == ["355691230004", "355691239999"]
    last = c.get(
        "/v1/messages", params={"limit": 5, "before_id": page2["next_before_id"]}, headers=h1
    ).json()
    assert len(last["items"]) == 1 and last["next_before_id"] is None
    assert (
        page1["items"][0]["total_price"] == "0.050000" and page1["items"][0]["status"] == "queued"
    )
    hit = c.get("/v1/messages", params={"q": "+3556912399"}, headers=h1).json()["items"]
    assert [m["to"] for m in hit] == ["355691239999"]
    assert (
        c.get("/v1/messages", params={"q": "%"}, headers=h1).json()["items"] == []
    )  # wildcard s'vepron
    assert c.get("/v1/messages", params={"status": "delivered"}, headers=h1).json()["items"] == []
    assert c.get("/v1/messages", params={"status": "nonsense"}, headers=h1).status_code == 422
    assert c.get("/v1/messages", headers=h2).json()["items"] == []
    assert c.get("/v1/messages", params={"owner_ref": "c1"}, headers=h2).status_code == 404


def test_email_history(db, verified, raw_client):  # noqa: F811
    from tests.test_email import send as esend

    c = raw_client
    h1, h2 = key(c, "client", "c1"), key(c, "client", "c2")
    esend(db, key="e1", to="ana@customer.org", subject="Welcome aboard")
    esend(db, key="e2", to="bob@customer.org", subject="Your invoice")
    esend(db, key="e3", to="cy@other.net", subject="Hello")
    page = c.get("/v1/email/messages", params={"limit": 2}, headers=h1).json()
    assert [e["to"] for e in page["items"]] == ["cy@other.net", "bob@customer.org"]
    assert page["next_before_id"] is not None and page["items"][0]["status"] == "queued"
    nxt = c.get(
        "/v1/email/messages", params={"before_id": page["next_before_id"]}, headers=h1
    ).json()
    assert [e["to"] for e in nxt["items"]] == ["ana@customer.org"]
    by_addr = c.get("/v1/email/messages", params={"q": "CUSTOMER.org"}, headers=h1).json()["items"]
    assert len(by_addr) == 2
    by_subject = c.get("/v1/email/messages", params={"q": "invoice"}, headers=h1).json()["items"]
    assert [e["subject"] for e in by_subject] == ["Your invoice"]
    assert c.get("/v1/email/messages", params={"q": "%"}, headers=h1).json()["items"] == []
    assert c.get("/v1/email/messages", headers=h2).json()["items"] == []
    assert (
        c.get("/v1/email/messages", headers=BOOT).status_code == 422
    )  # stafi duhet të zgjedhë llogari


# --- Çmimi live ---------------------------------------------------------------------


def test_quote_endpoint(db, world, raw_client):  # noqa: F811
    c = raw_client
    h = key(c, "client", "c1")
    ok = c.post("/v1/messages/quote", json={"to": OK, "text": "hello"}, headers=h)
    assert ok.status_code == 200
    assert ok.json() == {"country": "AL", "encoding": "gsm7", "segments": 1, "unit_price": "0.050000",
                         "total": "0.050000", "currency": "EUR"}  # fmt: skip
    two = c.post("/v1/messages/quote", json={"to": OK, "text": "ç" * 71}, headers=h).json()
    assert (two["encoding"], two["segments"], two["total"]) == ("ucs2", 2, "0.100000")
    bad = c.post("/v1/messages/quote", json={"to": "0691234567", "text": "hi"}, headers=h)
    assert bad.status_code == 422 and "international format" in bad.json()["detail"]["message"]
    assert (
        c.post("/v1/messages/quote", json={"to": "+4915112345678", "text": "hi"}, headers=h).json()[
            "detail"
        ]["code"]
        == "no_route"
    )
    nobody = key(c, "client", "nobody")
    assert (
        c.post("/v1/messages/quote", json={"to": OK, "text": "hi"}, headers=nobody).status_code
        == 403
    )
    assert c.post("/v1/messages/quote", json={"to": OK, "text": ""}, headers=h).status_code == 422


# --- Sender ID dhe template ---------------------------------------------------------------


def test_sender_ids_own_list_and_staff_queue(db, world, raw_client):  # noqa: F811
    c = raw_client
    h1, h2 = key(c, "client", "c1"), key(c, "client", "c2")
    sender_ids.request(db, "c2", "AL", "NEWCO")
    sender_ids.request(db, "c1", "XK", "ACME")
    db.commit()
    own = c.get("/v1/sender-ids", headers=h1).json()
    assert {(s["value"], s["status"]) for s in own} == {("ACME", "approved"), ("ACME", "pending")}
    assert [s["value"] for s in c.get("/v1/sender-ids", headers=h2).json()] == ["NEWCO"]
    approver = key(c, "approver")
    queue = c.get("/v1/sender-ids", params={"status": "pending"}, headers=approver).json()
    assert {s["owner_ref"] for s in queue} == {"c1", "c2"}
    assert (
        c.get("/v1/sender-ids", headers=key(c, "support")).status_code == 403
    )  # s'ka sender:review


def test_template_list_and_review_queue(db, world, raw_client):  # noqa: F811
    c = raw_client
    h1, h2 = key(c, "client", "c1"), key(c, "client", "c2")
    v = templates.create(db, "c1", "otp", "Code {{code}} for {{name}}")
    templates.review(db, v.id, "approve", "admin")
    templates.new_version(db, v.template_id, "Your code: {{code}}")
    templates.create(db, "c2", "promo", "Hi")
    db.commit()
    mine = c.get("/v1/templates", headers=h1).json()
    assert len(mine) == 1 and mine[0]["name"] == "otp"
    assert [(x["version"], x["status"], x["variables"]) for x in mine[0]["versions"]] == [
        (2, "pending", ["code"]), (1, "approved", ["code", "name"])]  # fmt: skip
    assert [t["name"] for t in c.get("/v1/templates", headers=h2).json()] == ["promo"]
    queue = c.get("/v1/templates", params={"status": "pending"}, headers=key(c, "approver")).json()
    assert {t["owner_ref"] for t in queue} == {"c1", "c2"}
    assert all(x["status"] == "pending" for t in queue for x in t["versions"])


# --- Tarifa, wallet, top-up -------------------------------------------------------------------


def test_rate_cards_and_wallets(db, world, raw_client):  # noqa: F811
    c = raw_client
    pricing, h1, h2, fin = (
        key(c, "pricing"),
        key(c, "client", "c1"),
        key(c, "client", "c2"),
        key(c, "finance"),
    )
    cards = c.get("/v1/rate-cards", headers=pricing).json()
    assert cards[0]["name"] == "std" and cards[0]["versions"][0]["status"] == "published"
    assert cards[0]["versions"][0]["rates"] == 1
    vid = cards[0]["versions"][0]["id"]
    assert (
        c.get(f"/v1/rate-card-versions/{vid}/rates", headers=pricing).json()[0]["prefix"] == "355"
    )
    assert c.get("/v1/rate-cards", headers=h1).status_code == 403  # klienti s'sheh tarifën e listës
    assert c.get("/v1/rate-card-versions/999/rates", headers=pricing).status_code == 404
    w = c.get("/v1/wallets", headers=h1).json()
    assert w[0]["currency"] == "EUR" and w[0]["available"] == "10.000000"
    assert c.get("/v1/wallets", headers=h2).json() == []
    tops = c.get(f"/v1/wallets/{w[0]['id']}/topups", headers=h1).json()
    assert tops[0]["status"] == "confirmed" and tops[0]["amount"] == "10.000000"
    assert c.get(f"/v1/wallets/{w[0]['id']}/topups", headers=h2).status_code == 404
    top = wallets.create_topup(db, w[0]["id"], "20", wallets.TopupMethod.CASH)
    db.commit()
    pend = c.get("/v1/topups", headers=fin).json()
    assert [(p["id"], p["owner_ref"], p["currency"]) for p in pend] == [(top.id, "c1", "EUR")]
    assert c.get("/v1/topups", headers=h1).status_code == 403


def test_accounts_and_vat(db, world, raw_client):  # noqa: F811
    c = raw_client
    support, fin = key(c, "support"), key(c, "finance")
    accs = c.get("/v1/admin/accounts", headers=support).json()
    assert accs[0]["owner_ref"] == "c1" and accs[0]["wallets"][0]["available"] == "10.000000"
    assert accs[0]["sending_enabled"] is True and accs[0]["has_rate_card"] is True
    assert (
        accs[0]["rate_card_id"]
        and accs[0]["wallets"][0]["id"]
        and accs[0]["rate_limit_per_min"] is None
    )
    assert c.get("/v1/admin/accounts", headers=key(c, "client", "c1")).status_code == 403
    r = c.put("/v1/admin/billing/c1/vat", json={"vat_rate": "0.2"}, headers=fin)
    assert r.status_code == 422 and "billing details" in r.json()["detail"]["message"]
    billing.set_profile(db, "c1", "Acme Ltd", "Main 1", "AL", "a@acme.example")
    db.commit()
    assert (
        c.put("/v1/admin/billing/c1/vat", json={"vat_rate": "0.2"}, headers=fin).status_code == 200
    )
    assert (
        c.put("/v1/admin/billing/c1/vat", json={"vat_rate": "1.5"}, headers=fin).status_code == 422
    )
    assert (
        c.put(
            "/v1/admin/billing/c1/vat", json={"vat_rate": "0.2"}, headers=key(c, "client", "c1")
        ).status_code
        == 403
    )
    assert "billing.vat" in {a["action"] for a in c.get("/v1/admin/audit", headers=BOOT).json()}


# --- Nisja dhe kontaktet --------------------------------------------------------------------


def test_onboarding_checklist_tracks_progress(db, world, raw_client):  # noqa: F811
    c = raw_client
    h = key(c, "client", "c1")
    o = c.get("/v1/portal/onboarding", headers=h).json()
    done = {s["id"]: s["done"] for s in o["steps"]}
    assert done == {"wallet": True, "sender": True, "contacts": False, "message": False,
                    "domain": False, "webhook": False, "billing": False}  # fmt: skip
    assert (o["required_done"], o["required_total"]) == (2, 4)
    assert all(s["link"] and s["hint"] for s in o["steps"])
    contacts_svc.upsert(db, "c1", phone="355691230009")
    send(db, key="ob1")
    db.commit()
    o = c.get("/v1/portal/onboarding", headers=h).json()
    assert (o["required_done"], o["required_total"]) == (4, 4)
    assert (
        c.get("/v1/portal/onboarding", headers=key(c, "client", "c2")).json()["required_done"] == 0
    )


def test_contact_search(db, raw_client):
    c = raw_client
    h = key(c, "client", "c1")
    contacts_svc.upsert(
        db,
        "c1",
        phone="355691110001",
        email="ana.hoxha@example.com",
        first_name="Ana",
        last_name="Hoxha",
    )
    contacts_svc.upsert(db, "c1", phone="38344210002", first_name="Besa", last_name="Krasniqi")
    contacts_svc.upsert(db, "c1", email="Dritan@Example.com")
    db.commit()

    def names(q):
        return sorted(
            (x["first_name"] or x["email"])
            for x in c.get("/v1/contacts", params={"q": q}, headers=h).json()
        )

    assert names("ana") == ["Ana"]
    assert names("HOXHA") == ["Ana"]
    assert names("+38344") == ["Besa"]
    assert names("example.com") == ["Ana", "dritan@example.com"]
    assert names("%") == [] and names("_") == []  # wildcards s'vepron
    assert len(c.get("/v1/contacts", headers=h).json()) == 3
