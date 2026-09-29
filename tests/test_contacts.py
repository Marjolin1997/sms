import hashlib
import hmac
import json

import pytest

from app.core.config import settings
from app.models.contacts import ConsentEvent, ConsentImmutableError, ConsentState, Contact
from app.services import consent
from app.services import contacts as svc
from app.services.wallet import Conflict, NotFound
from tests.test_pipeline import OK, fake, world  # noqa: F401

PHONE = "+355691230003"


def mk(db, owner="c1", **kw):
    c, _ = svc.upsert(db, owner, **({"phone": PHONE} | kw))
    return c


# --- Contacts -----------------------------------------------------------------


def test_upsert_normalizes_and_merges(db):
    a, created = svc.upsert(db, "c1", phone="+355 69 123 0003".replace(" ", ""), first_name="Ana")
    assert created and a.phone == "355691230003"
    b, created = svc.upsert(db, "c1", phone="355691230003", email="Ana@Example.COM")
    assert not created and b.id == a.id and b.email == "ana@example.com"
    assert svc.upsert(db, "c1", email="ana@example.com", attributes={"plan": "pro"})[0].id == a.id
    assert a.attributes == {"plan": "pro"} and a.first_name == "Ana"
    other, created = svc.upsert(db, "c2", phone="355691230003")  # tenant tjetër: kontakt i ri
    assert created and other.id != a.id


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"phone": "0691230003"},
        {"phone": "abc"},
        {"email": "not-an-email"},
        {"email": "a@b"},
        {"phone": PHONE, "attributes": {"k": {"nested": 1}}},
        {"phone": PHONE, "attributes": {"x" * 33: "v"}},
        {"phone": PHONE, "attributes": {"k": "v" * 201}},
        {"phone": PHONE, "attributes": {f"k{i}": "v" for i in range(21)}},
    ],
)
def test_invalid_contacts(db, kw):
    with pytest.raises(svc.InvalidContact):
        svc.upsert(db, "c1", **kw)


def test_identity_conflicts(db):
    a, _ = svc.upsert(db, "c1", phone=PHONE)
    b, _ = svc.upsert(db, "c1", email="b@example.com")
    with pytest.raises(Conflict):  # phone → kontakti A, email → kontakti B
        svc.upsert(db, "c1", phone=PHONE, email="b@example.com")
    # B nuk kishte telefon: shtohet pa problem
    assert svc.upsert(db, "c1", email="b@example.com", phone="355691230004")[0].id == b.id
    with pytest.raises(Conflict):  # por një telefon i dytë do të ndryshonte identitetin
        svc.upsert(db, "c1", email="b@example.com", phone="355691230005")
    assert (a.email, b.phone) == (None, "355691230004")


def test_import_partial_failures(db):
    rows = [{"phone": PHONE}, {"phone": "bad"}, {"email": "x@example.com"}, {"phone": PHONE}]
    r = svc.import_contacts(db, "c1", rows)
    assert (r.created, r.updated) == (2, 1)
    assert [e["row"] for e in r.errors] == [1] and r.errors[0]["code"] == "invalid_contact"
    with pytest.raises(svc.InvalidContact):
        svc.import_contacts(db, "c1", [{"phone": PHONE}] * 1001)


# --- Consent ------------------------------------------------------------------


def test_addresses_never_stored_in_plaintext(db):
    consent.record(
        db, "c1", "sms", PHONE, "opt_in", "opt_in", "web_form", "u", "checkbox 2025-01-01"
    )
    db.commit()
    st = db.query(ConsentState).one()
    expected = hmac.new(b"test-pii-key", b"c1\x00sms\x00355691230003", hashlib.sha256).hexdigest()
    assert st.address_hash == expected
    dump = json.dumps([st.address_hash, st.reason]) + str(db.query(ConsentEvent).one().__dict__)
    assert "355691230003" not in dump


def test_marketing_requires_opt_in_transactional_does_not(db):
    assert consent.check(db, "c1", "sms", PHONE, "transactional").allowed
    assert consent.check(db, "c1", "sms", PHONE, "marketing").reason == "no_consent"
    with pytest.raises(Conflict):  # opt-in pa provë
        consent.record(db, "c1", "sms", PHONE, "opt_in", "x", "form", "u", None)
    consent.record(db, "c1", "sms", PHONE, "opt_in", "x", "form", "u", "signup form v3, ip 1.2.3.4")
    assert consent.check(db, "c1", "sms", PHONE, "marketing").allowed
    assert not consent.check(db, "c2", "sms", PHONE, "marketing").allowed  # tenant tjetër
    assert not consent.check(
        db, "c1", "email", "a@example.com", "marketing"
    ).allowed  # kanal tjetër


def test_soft_vs_hard_opt_out(db):
    consent.record(db, "c1", "sms", PHONE, "opt_in", "x", "form", "u", "evidence")
    consent.record(db, "c1", "sms", PHONE, "opt_out", "unsubscribe", "link", "u")
    assert consent.check(db, "c1", "sms", PHONE, "marketing").reason == "opted_out"
    assert consent.check(db, "c1", "sms", PHONE, "transactional").allowed  # soft: OTP kalon
    consent.record(db, "c1", "sms", PHONE, "opt_in", "x", "form", "u", "re-consent")
    consent.record(db, "c1", "sms", PHONE, "opt_out", "complaint", "provider", "sys")
    assert consent.check(db, "c1", "sms", PHONE, "transactional").reason == "blocked:complaint"
    with pytest.raises(Conflict):  # complaint nuk zhbëhet me opt-in
        consent.record(db, "c1", "sms", PHONE, "opt_in", "x", "form", "u", "please")
    # një opt-out "soft" më vonë nuk e zbut bllokimin e ashpër
    consent.record(db, "c1", "sms", PHONE, "opt_out", "unsubscribe", "link", "u")
    assert consent.check(db, "c1", "sms", PHONE, "transactional").reason == "blocked:complaint"


def test_history_is_kept_and_immutable(db):
    consent.record(db, "c1", "sms", PHONE, "opt_in", "x", "form", "u", "evidence")
    consent.record(db, "c1", "sms", PHONE, "opt_out", "unsubscribe", "link", "u")
    db.commit()
    assert [e.action.value for e in db.query(ConsentEvent).order_by(ConsentEvent.id)] == [
        "opt_in", "opt_out"]  # fmt: skip
    ev = db.query(ConsentEvent).first()
    ev.evidence = "forged"
    with pytest.raises(ConsentImmutableError):
        db.flush()
    db.rollback()


def test_inbound_stop_and_start(db):
    consent.record(db, "c1", "sms", PHONE, "opt_in", "x", "form", "u", "evidence")
    assert consent.apply_inbound_keyword(db, "c1", PHONE, " STOP! ") == "opt_out"
    assert consent.check(db, "c1", "sms", PHONE, "transactional").reason == "blocked:stop_keyword"
    assert consent.apply_inbound_keyword(db, "c1", PHONE, "hello") is None
    assert (
        consent.apply_inbound_keyword(db, "c1", PHONE, "START") == "opt_in"
    )  # STOP i vetë personit
    assert consent.check(db, "c1", "sms", PHONE, "marketing").allowed
    consent.record(db, "c1", "sms", PHONE, "opt_out", "bounce_hard", "provider", "sys")
    assert consent.apply_inbound_keyword(db, "c1", PHONE, "START") is None  # bounce nuk zhbëhet


def test_fail_closed_without_hmac_key(db, monkeypatch):
    monkeypatch.setattr(settings, "pii_hmac_key", "")
    with pytest.raises(RuntimeError):
        consent.check(db, "c1", "sms", PHONE, "marketing")


# --- Erasure ------------------------------------------------------------------


def test_erase_removes_pii_but_keeps_suppression(db):
    c = mk(db, email="ana@example.com", first_name="Ana", attributes={"k": "v"})
    lst = svc.create_list(db, "c1", "all")
    svc.add_members(db, "c1", lst.id, [c.id])
    svc.erase(db, "c1", c.id, "dpo")
    db.commit()
    row = db.get(Contact, c.id)
    assert (row.phone, row.email, row.first_name, row.attributes) == (None, None, None, None)
    assert row.status.value == "erased"
    with pytest.raises(NotFound):
        svc.update(db, "c1", c.id, first_name="x")
    # re-import i të njëjtit numër nuk e rikthen në dërgim
    svc.upsert(db, "c1", phone=PHONE)
    assert consent.check(db, "c1", "sms", PHONE, "transactional").reason == "blocked:erasure"
    assert svc.audience_counts(db, "c1", lst.id, "sms", "marketing") == {}


# --- Lista dhe audienca ---------------------------------------------------------


def test_lists_are_tenant_scoped(db):
    a = mk(db, "c1")
    b, _ = svc.upsert(db, "c2", phone=PHONE)
    lst = svc.create_list(db, "c1", "vip")
    with pytest.raises(Conflict):
        svc.create_list(db, "c1", "vip")
    assert svc.add_members(db, "c1", lst.id, [a.id]) == 1
    assert svc.add_members(db, "c1", lst.id, [a.id]) == 0  # idempotent
    with pytest.raises(NotFound):  # kontakt i tenant-it tjetër
        svc.add_members(db, "c1", lst.id, [b.id])
    with pytest.raises(NotFound):  # listë e tenant-it tjetër
        svc.add_members(db, "c2", lst.id, [b.id])


def test_audience_decisions(db):
    lst = svc.create_list(db, "c1", "promo")
    ids = {}
    for name, kw in {
        "ok": {"phone": "355691230010"}, "unsub": {"phone": "355691230011"},
        "none": {"phone": "355691230012"}, "email_only": {"email": "e@example.com"},
        "stopped": {"phone": "355691230013"},
    }.items():  # fmt: skip
        ids[name] = svc.upsert(db, "c1", **kw)[0].id
    svc.add_members(db, "c1", lst.id, list(ids.values()))
    for ph in ("355691230010", "355691230011", "355691230013"):
        consent.record(db, "c1", "sms", ph, "opt_in", "x", "form", "u", "evidence")
    consent.record(db, "c1", "sms", "355691230011", "opt_out", "unsubscribe", "l", "u")
    consent.record(db, "c1", "sms", "355691230013", "opt_out", "stop_keyword", "sms", "u")
    assert svc.audience_counts(db, "c1", lst.id, "sms", "marketing") == {
        "ok": 1,
        "opted_out": 1,
        "no_consent": 1,
        "no_address": 1,
        "blocked:stop_keyword": 1,
    }
    assert svc.audience_counts(db, "c1", lst.id, "sms", "transactional") == {
        "ok": 3,
        "no_address": 1,
        "blocked:stop_keyword": 1,
    }
    page1 = svc.audience_batch(db, "c1", lst.id, "sms", "marketing", limit=2)
    page2 = svc.audience_batch(db, "c1", lst.id, "sms", "marketing", after_id=page1[-1].contact_id)
    assert len(page1) == 2 and len(page2) == 3


# --- Integrimi me dërgimin -----------------------------------------------------


def test_send_blocks_marketing_without_consent_and_stop(db, world):  # noqa: F811
    from app.services import messages as m

    with pytest.raises(consent.RecipientSuppressed):
        m.submit(db, "c1", "m1", OK, "ACME", text="promo", category="marketing")
    consent.record(db, "c1", "sms", OK, "opt_in", "x", "form", "u", "checkbox")
    msg = m.submit(db, "c1", "m2", OK, "ACME", text="promo", category="marketing")
    assert msg.category == "marketing"
    consent.apply_inbound_keyword(db, "c1", OK, "STOP")
    with pytest.raises(consent.RecipientSuppressed):  # STOP bllokon edhe transactional
        m.submit(db, "c1", "m3", OK, "ACME", text="otp 123")
    assert m.submit(db, "c1", "m2", OK, "ACME", text="promo", category="marketing").id == msg.id


# --- API ------------------------------------------------------------------------

BOOT = {"X-Admin-Key": "test-key"}


def _key(client, role, owner=None):
    r = client.post(
        "/v1/admin/api-keys", json={"name": "k", "role": role, "owner_ref": owner}, headers=BOOT
    )
    return {"Authorization": f"Bearer {r.json()['key']}"}


@pytest.fixture
def raw_client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app())


def test_api_client_flow_and_isolation(raw_client):
    c = raw_client
    h1, h2 = _key(c, "client", "c1"), _key(c, "client", "c2")
    r = c.post("/v1/contacts", json={"phone": PHONE, "first_name": "Ana"}, headers=h1)
    assert r.status_code == 201
    cid = r.json()["id"]
    assert c.post("/v1/contacts", json={"phone": PHONE}, headers=h1).status_code == 200  # upsert
    assert c.get(f"/v1/contacts/{cid}", headers=h2).status_code == 404
    assert c.get("/v1/contacts", headers=h2).json() == []
    assert c.get("/v1/contacts", params={"owner_ref": "c1"}, headers=h2).status_code == 404
    lst = c.post("/v1/lists", json={"name": "promo"}, headers=h1).json()
    assert c.post(
        f"/v1/lists/{lst['id']}/members", json={"contact_ids": [cid]}, headers=h1
    ).json() == {"added": 1}
    assert (
        c.post(
            f"/v1/lists/{lst['id']}/members", json={"contact_ids": [cid]}, headers=h2
        ).status_code
        == 404
    )
    consent_body = {
        "channel": "sms",
        "address": PHONE,
        "action": "opt_in",
        "source": "web",
        "evidence": "form v1",
    }
    assert c.post("/v1/consent", json=consent_body, headers=h1).json()["opted_in"] is True
    aud = c.get(f"/v1/lists/{lst['id']}/audience", params={"channel": "sms"}, headers=h1).json()
    assert aud == {"eligible": 1, "excluded": {}}
    chk = c.get("/v1/consent/check", params={"channel": "sms", "address": PHONE}, headers=h1).json()
    assert chk == {"allowed": True, "reason": "ok"}
    assert c.delete(f"/v1/contacts/{cid}", headers=h1).status_code == 204
    assert c.get(f"/v1/contacts/{cid}", headers=h1).status_code == 404
    log = c.get("/v1/admin/audit", headers=BOOT).json()
    assert {"consent.opt_in", "contact.erase"} <= {a["action"] for a in log}
    assert "355691230003" not in json.dumps(log)  # audit-i nuk mban adresa


def test_api_staff_needs_owner_and_import(raw_client):
    c = raw_client
    staff = _key(c, "support")
    assert c.get("/v1/contacts", headers=staff).status_code == 422
    assert c.get("/v1/contacts", params={"owner_ref": "c1"}, headers=staff).status_code == 200
    assert (
        c.post("/v1/contacts", json={"phone": PHONE, "owner_ref": "c1"}, headers=staff).status_code
        == 403
    )
    r = c.post("/v1/contacts/import", json={"owner_ref": "c1", "contacts": [{"phone": PHONE}, {"phone": "x"}]},
               headers=BOOT).json()  # fmt: skip
    assert r["created"] == 1 and r["errors"][0]["row"] == 1


def test_inbound_webhook(raw_client, db, monkeypatch, world):  # noqa: F811
    from app.services import sender_ids

    monkeypatch.setattr(settings, "dlr_secrets", {"fake": "sec"})
    s = sender_ids.request(db, "c1", "AL", "+355690000001")
    sender_ids.approve(db, s.id, "admin")
    consent.record(db, "c1", "sms", PHONE, "opt_in", "x", "form", "u", "evidence")
    db.commit()

    def post(body, secret="sec"):
        raw = json.dumps(body).encode()
        sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return raw_client.post("/webhooks/inbound/fake", content=raw, headers={"X-Signature": sig})

    assert (
        post({"to": "+355690000001", "from": PHONE, "text": "STOP"}, secret="bad").status_code
        == 401
    )
    assert post({"to": "+355690000001", "from": PHONE, "text": "STOP"}).json() == {
        "outcome": "opt_out"
    }
    assert post({"to": "+355690000009", "from": PHONE, "text": "STOP"}).json() == {
        "outcome": "unrouted"
    }
    assert post({"to": "+355690000001", "from": PHONE, "text": "thanks"}).json() == {
        "outcome": "ignored"
    }
    db.expire_all()
    assert consent.check(db, "c1", "sms", PHONE, "transactional").reason == "blocked:stop_keyword"
