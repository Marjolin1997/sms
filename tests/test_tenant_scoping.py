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
