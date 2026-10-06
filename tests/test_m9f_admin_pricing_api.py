# ruff: noqa: F811
"""M9-f — API admin e çmimeve (Central): libra/versione/rregulla/caktime, aktivizim/tërheqje, i pandryshueshëm, input strikt."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.central.main import create_app
from apps.central.models import AuditLog
from apps.central.models.pricing import PriceAssignment, PriceBook, PriceRule, PriceVersion
from apps.central.services import credit_accounts as accts
from apps.central.services import enterprises
from apps.central.services import products as prod_svc
from tests.test_central import IS_PG, make_db  # noqa: F401
from tests.test_central_auth import auth_secret, bearer, mk, token_for  # noqa: F401
from tests.test_central_products import cdb  # noqa: F401


def iso(dt):
    return dt.astimezone(UTC).isoformat()


D1 = datetime.now(UTC) + timedelta(days=1)
D2 = D1 + timedelta(days=10)


@pytest.fixture
def api(cdb):
    url, eng = cdb
    c = TestClient(create_app(eng))
    a1 = mk(eng, "a1@example.com", role="admin")
    mk(eng, "op@example.com", role="operator")
    with Session(eng, expire_on_commit=False) as s:
        ent = enterprises.create(s, "Acme").id
        sms = prod_svc.create(s, "sms", "SMS", "sms").id
        email = prod_svc.create(s, "email", "Email", "email").id
        accts.create(s, ent, sms, "EUR", s.get(type(a1), a1.id))
        s.commit()
    c.eng, c.ent, c.sms, c.email = eng, ent, sms, email
    c.a1, c.op = bearer(token_for(c, "a1@example.com")), bearer(token_for(c, "op@example.com"))
    return c


def book(api, code="retail", cur="EUR"):
    r = api.post(
        "/admin/pricing/books",
        json={"code": code, "name": "Retail", "currency": cur},
        headers=api.a1,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def draft(api, bid):
    r = api.post(f"/admin/pricing/books/{bid}/versions", headers=api.a1)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def rule(api, vid, price="0.050000", prefix="355", operator="", channel="sms"):
    r = api.post(
        f"/admin/pricing/versions/{vid}/rules",
        json={"channel": channel, "prefix": prefix, "operator": operator, "unit_price": price},
        headers=api.a1,
    )
    assert r.status_code == 200, r.text
    return r.json()


def activate(api, vid, at=D1):
    return api.post(
        f"/admin/pricing/versions/{vid}/activate", json={"effective_from": iso(at)}, headers=api.a1
    )


def count(api, model, *where):
    with Session(api.eng) as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def audits(api, like):
    with Session(api.eng) as s:
        return list(s.scalars(select(AuditLog).where(AuditLog.action.like(like))))


def ready(api, price="0.050000"):
    bid = book(api)
    vid = draft(api, bid)
    rule(api, vid, price)
    assert activate(api, vid).status_code == 200
    return bid, vid


# --- fluksi bazë + UI-ready -----------------------------------------------------------------------------------


def test_book_version_rule_activate_flow_returns_ui_ready_documents(api):
    bid = book(api)
    assert (
        api.post(
            "/admin/pricing/books",
            json={"code": "retail", "name": "Retail", "currency": "EUR"},
            headers=api.a1,
        ).json()["id"]
        == bid
    )  # idempotent
    vid = draft(api, bid)
    v = rule(api, vid, "0.05", "355")["version"]
    assert (
        v["status"] == "draft"
        and v["editable"] is True
        and v["rules"]
        == [
            {
                "rule_id": v["rules"][0]["rule_id"],
                "channel": "sms",
                "prefix": "355",
                "operator": "",
                "unit_price": "0.050000",
            }
        ]
    )
    r = activate(api, vid)
    assert r.status_code == 200 and r.json()["status"] == "active" and r.json()["editable"] is False
    assert r.json()["content_hash"] and r.json()["effective_from"].startswith(
        D1.strftime("%Y-%m-%d")
    )
    detail = api.get(f"/admin/pricing/books/{bid}", headers=api.op).json()
    assert [x["status"] for x in detail["versions"]] == ["active"] and detail["versions"][0][
        "rule_count"
    ] == 1
    assert (
        len(audits(api, "price_version.activate")) == 1
        and len(audits(api, "price_rule.set")) == 1
        and len(audits(api, "price_book.create")) == 1
    )


def test_active_version_financial_fields_are_immutable_through_the_api(api):
    bid, vid = ready(api)
    before = api.get(f"/admin/pricing/versions/{vid}", headers=api.op).json()
    r = api.post(
        f"/admin/pricing/versions/{vid}/rules",
        json={"channel": "sms", "prefix": "355", "unit_price": "0.900000"},
        headers=api.a1,
    )
    assert r.status_code == 409
    r = api.post(
        f"/admin/pricing/versions/{vid}/rules",
        json={"channel": "sms", "prefix": "44", "unit_price": "0.100000"},
        headers=api.a1,
    )
    assert r.status_code == 409
    assert (
        api.post(
            f"/admin/pricing/versions/{vid}/rules/remove",
            json={"channel": "sms", "prefix": "355"},
            headers=api.a1,
        ).status_code
        == 409
    )
    assert activate(api, vid, D2).status_code == 409  # as rifaktivizim me datë tjetër
    assert api.get(f"/admin/pricing/versions/{vid}", headers=api.op).json() == before
    # një version i ri nuk e prek të vjetrin; i vjetri mbetet i lexueshëm (historia)
    v2 = draft(api, bid)
    rule(api, v2, "0.070000")
    assert activate(api, v2, D2).status_code == 200
    assert api.get(f"/admin/pricing/versions/{vid}", headers=api.op).json() == before


def test_activation_is_idempotent_for_the_same_instant_and_rejects_empty_past_and_overlapping(api):
    bid = book(api)
    v1 = draft(api, bid)
    assert activate(api, v1).status_code == 409  # bosh
    rule(api, v1)
    assert activate(api, v1, datetime.now(UTC) - timedelta(days=1)).status_code == 409  # e kaluara
    assert activate(api, v1).status_code == 200
    assert (
        activate(api, v1).status_code == 200 and len(audits(api, "price_version.activate")) == 1
    )  # no-op
    v2 = draft(api, bid)
    rule(api, v2, "0.07")
    assert activate(api, v2, D1).status_code == 409  # i njëjti çast ⇒ version efektiv i dyfishtë
    assert (
        activate(api, v2, D1 - timedelta(hours=1)).status_code == 409
    )  # para versionit të mëparshëm
    assert activate(api, v2, D2).status_code == 200


def test_second_draft_is_refused_and_retire_requires_reason_and_is_final(api):
    bid, vid = ready(api)
    d = draft(api, bid)
    assert api.post(f"/admin/pricing/books/{bid}/versions", headers=api.a1).status_code == 409
    assert (
        api.post(
            f"/admin/pricing/versions/{d}/retire", json={"reason": "x"}, headers=api.a1
        ).status_code
        == 409
    )  # draft s'tërhiqet
    assert (
        api.post(f"/admin/pricing/versions/{vid}/retire", json={}, headers=api.a1).status_code
        == 422
    )
    assert (
        api.post(
            f"/admin/pricing/versions/{vid}/retire", json={"reason": ""}, headers=api.a1
        ).status_code
        == 422
    )
    r = api.post(
        f"/admin/pricing/versions/{vid}/retire", json={"reason": "wrong tariff"}, headers=api.a1
    )
    assert (
        r.status_code == 200
        and r.json()["status"] == "retired"
        and r.json()["retire_reason"] == "wrong tariff"
    )
    assert (
        api.post(
            f"/admin/pricing/versions/{vid}/retire", json={"reason": "again"}, headers=api.a1
        ).status_code
        == 200
    )
    assert len(audits(api, "price_version.retire")) == 1
    assert activate(api, vid, D2).status_code == 409  # retired është përfundimtar


def test_rule_set_noop_remove_and_copy_forward_into_the_next_draft(api):
    bid = book(api)
    vid = draft(api, bid)
    assert rule(api, vid)["changed"] is True and rule(api, vid)["changed"] is False
    assert len(audits(api, "price_rule.set")) == 1
    rule(api, vid, "0.100000", "44")
    rm = api.post(
        f"/admin/pricing/versions/{vid}/rules/remove",
        json={"channel": "sms", "prefix": "44"},
        headers=api.a1,
    )
    assert rm.json()["changed"] is True and len(rm.json()["version"]["rules"]) == 1
    assert (
        api.post(
            f"/admin/pricing/versions/{vid}/rules/remove",
            json={"channel": "sms", "prefix": "44"},
            headers=api.a1,
        ).json()["changed"]
        is False
    )
    activate(api, vid)
    nxt = draft(api, bid)
    got = api.get(f"/admin/pricing/versions/{nxt}", headers=api.op).json()
    assert got["rule_count"] == 1 and got["version"] == 2 and got["status"] == "draft"


# --- input strikt ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "price",
    [
        0.05,
        1,
        True,
        None,
        "",
        "abc",
        "-0.01",
        "1e-2",
        "0.0000001",
        "1,5",
        ".5",
        "5.",
        " 0.05",
        "0.05 ",
        "NaN",
        "Infinity",
        "99999999999999.5",
    ],
)
def test_unit_price_must_be_a_plain_decimal_string_with_at_most_six_decimals(api, price):
    vid = draft(api, book(api))
    r = api.post(
        f"/admin/pricing/versions/{vid}/rules",
        json={"channel": "sms", "prefix": "355", "unit_price": price},
        headers=api.a1,
    )
    assert r.status_code == 422, (price, r.text)
    assert count(api, PriceRule) == 0


def test_zero_and_six_decimal_prices_are_accepted_and_stored_exactly(api):
    vid = draft(api, book(api))
    assert rule(api, vid, "0", "1")["version"]["rules"][0]["unit_price"] == "0.000000"
    assert rule(api, vid, "0.000001", "2")["changed"] is True


@pytest.mark.parametrize("prefix", ["+355", "0355", "35 5", "abc", "1234567890123456789", "35\n5"])
def test_prefix_and_operator_validation(api, prefix):
    vid = draft(api, book(api))
    r = api.post(
        f"/admin/pricing/versions/{vid}/rules",
        json={"channel": "sms", "prefix": prefix, "unit_price": "0.05"},
        headers=api.a1,
    )
    assert r.status_code == 422, prefix
    assert (
        api.post(
            f"/admin/pricing/versions/{vid}/rules",
            json={"channel": "sms", "prefix": "355", "operator": "ab", "unit_price": "0.05"},
            headers=api.a1,
        ).status_code
        == 422
    )
    assert (
        api.post(
            f"/admin/pricing/versions/{vid}/rules",
            json={"channel": "sms", "prefix": "355", "operator": "27601", "unit_price": "0.05"},
            headers=api.a1,
        ).status_code
        == 200
    )
    assert count(api, PriceRule) == 1


def test_email_rules_have_empty_scope_and_channels_are_closed(api):
    vid = draft(api, book(api))
    assert (
        api.post(
            f"/admin/pricing/versions/{vid}/rules",
            json={"channel": "email", "prefix": "355", "unit_price": "0.01"},
            headers=api.a1,
        ).status_code
        == 422
    )
    assert (
        api.post(
            f"/admin/pricing/versions/{vid}/rules",
            json={"channel": "email", "unit_price": "0.010000"},
            headers=api.a1,
        ).status_code
        == 200
    )
    assert (
        api.post(
            f"/admin/pricing/versions/{vid}/rules",
            json={"channel": "push", "unit_price": "0.01"},
            headers=api.a1,
        ).status_code
        == 422
    )


def test_overlapping_rule_scope_is_updated_not_duplicated(api):
    vid = draft(api, book(api))
    rule(api, vid, "0.05", "355")
    rule(api, vid, "0.06", "355")  # i njëjti (channel, prefix, operator) ⇒ përditëson, s'dyfishon
    assert count(api, PriceRule) == 1
    rule(
        api, vid, "0.07", "355", "27601"
    )  # operator ≠ ⇒ rregull tjetër (precedencë, jo mbivendosje)
    assert count(api, PriceRule) == 2


def test_book_currency_code_and_bodies_are_strict(api):
    for body in ({"code": "Retail", "name": "n", "currency": "EUR"}, {"code": "r", "name": "n", "currency": "EUR"}, {"code": "ok_code", "name": "n", "currency": "eur"},
                 {"code": "ok_code", "name": "n", "currency": "EURO"}, {"code": "ok_code", "name": "n", "currency": "EUR", "x": 1}, {"code": "ok_code", "currency": "EUR"}):  # fmt: skip
        assert api.post("/admin/pricing/books", json=body, headers=api.a1).status_code == 422, body
    book(api, "ok_code", "EUR")
    assert (
        api.post(
            "/admin/pricing/books",
            json={"code": "ok_code", "name": "Retail", "currency": "USD"},
            headers=api.a1,
        ).status_code
        == 409
    )
    assert count(api, PriceBook) == 1


def test_effective_from_must_be_timezone_aware_and_bodies_reject_unknown_fields(api):
    vid = draft(api, book(api))
    rule(api, vid)
    assert (
        api.post(
            f"/admin/pricing/versions/{vid}/activate",
            json={"effective_from": "2099-01-01T00:00:00"},
            headers=api.a1,
        ).status_code
        == 422
    )
    assert (
        api.post(
            f"/admin/pricing/versions/{vid}/activate",
            json={"effective_from": "garbage"},
            headers=api.a1,
        ).status_code
        == 422
    )
    assert (
        api.post(
            f"/admin/pricing/versions/{vid}/activate",
            json={"effective_from": iso(D1), "status": "active"},
            headers=api.a1,
        ).status_code
        == 422
    )
    assert (
        api.post(f"/admin/pricing/versions/{vid}/activate", json={}, headers=api.a1).status_code
        == 422
    )
    assert (
        api.post(
            f"/admin/pricing/versions/{uuid.uuid4()}/activate",
            json={"effective_from": iso(D1)},
            headers=api.a1,
        ).status_code
        == 404
    )


# --- caktimet, parapamja, gjendja, gatishmëria ----------------------------------------------------------------------


def assign(api, bid, at=D1, ent=None, prod=None):
    return api.post(
        "/admin/pricing/assignments",
        json={
            "enterprise_id": str(ent or api.ent),
            "product_id": str(prod or api.sms),
            "book_id": bid,
            "effective_from": iso(at),
        },
        headers=api.a1,
    )


def test_assignments_are_append_only_history_ordered_and_idempotent(api):
    bid, _ = ready(api)
    a = assign(api, bid)
    assert a.status_code == 201 and assign(api, bid).json()["id"] == a.json()["id"]
    assert len(audits(api, "price_assignment.create")) == 1
    other = book(api, "other")
    assert assign(api, other).status_code == 409  # i njëjti effective_from, libër tjetër
    assert (
        assign(api, other, D1 - timedelta(hours=1)).status_code == 409
    )  # para caktimit të mëparshëm
    assert assign(api, other, D2).status_code == 201
    items = api.get(f"/admin/pricing/assignments?enterprise_id={api.ent}", headers=api.op).json()[
        "items"
    ]
    assert [i["book_id"] for i in items] == [bid, other]
    assert assign(api, bid, D2 + timedelta(days=1), ent=uuid.uuid4()).status_code == 404
    assert assign(api, bid, D2 + timedelta(days=1), prod=uuid.uuid4()).status_code == 404
    assert assign(api, bid, datetime.now(UTC) - timedelta(days=2)).status_code in (
        409,
    )  # asnjë caktim i kaluar (veç import)
    assert count(api, PriceAssignment) == 2


def test_preview_uses_the_same_precedence_and_fails_closed(api):
    bid = book(api)
    vid = draft(api, bid)
    rule(api, vid, "0.050000", "355")
    rule(api, vid, "0.040000", "35569")
    rule(api, vid, "0.030000", "35569", "27601")
    rule(api, vid, "0.010000", "", "", "email")
    activate(api, vid)
    assign(api, bid)
    at = iso(D1 + timedelta(hours=1))

    def prev(**kw):
        q = {"enterprise_id": str(api.ent), "product_id": str(api.sms), "at": at, **kw}
        return api.get("/admin/pricing/preview", params=q, headers=api.op)

    assert prev(number="355691234567").json()["unit_price"] == "0.040000"
    assert prev(number="355691234567", operator="27601").json()["unit_price"] == "0.030000"
    assert prev(number="355671234567").json()["unit_price"] == "0.050000"
    assert prev(number="4412345678901").status_code == 409  # pa rregull ⇒ fail-closed
    assert (
        prev(number="355691234567", at=iso(D1 - timedelta(hours=1))).status_code == 409
    )  # para efektivit
    assert (
        api.get(
            "/admin/pricing/preview",
            params={"enterprise_id": str(api.ent), "product_id": str(api.email), "number": "355"},
            headers=api.op,
        ).status_code
        == 409
    )
    assert prev(number="355691234567", at="2030-01-01T00:00:00").status_code == 422  # pa timezone
    api.post(f"/admin/pricing/versions/{vid}/retire", json={"reason": "stop"}, headers=api.a1)
    assert prev(number="355691234567").status_code == 409  # i tërhequr ⇒ s'ka fallback te i vjetri


def test_state_and_readiness_report_accounts_without_price_currency_mismatch_and_pass_when_priced(
    api,
):
    st0 = api.get("/admin/pricing/state", headers=api.op).json()
    r0 = api.get("/admin/pricing/readiness", headers=api.op).json()
    assert (
        r0["ok"] is False
        and {c["name"]: c["level"] for c in r0["checks"]}["pricing_active_version"] == "FAIL"
    )
    bid, _ = ready(api)
    st1 = api.get("/admin/pricing/state", headers=api.op).json()
    assert st1["revision"] > st0["revision"] and st1["epoch"] == st0["epoch"]
    assert st1["books"][0]["versions"] == 1 and st1["books"][0]["active_versions"] == 1
    # llogaria ekziston por pa caktim ⇒ FAIL; pas caktimit efektiv ⇒ PASS (caktim në të kaluarën vetëm me import: simulohet me `imported`)
    r1 = {
        c["name"]: c["level"]
        for c in api.get("/admin/pricing/readiness", headers=api.op).json()["checks"]
    }
    assert r1["pricing_assignment_present"] == "FAIL"
    from apps.central.core.timeutil import utcnow
    from apps.central.models import CentralUser
    from apps.central.services import pricing as pricing_svc

    with Session(api.eng, expire_on_commit=False) as s:
        admin = s.scalar(select(CentralUser).where(CentralUser.email == "a1@example.com"))
        b2 = pricing_svc.create_book(s, admin, "usd", "USD", "USD")
        past = utcnow() - timedelta(days=3)
        pricing_svc.assign(s, admin, api.ent, api.sms, b2.id, past, imported=True)
        s.commit()
    r2 = {
        c["name"]: c["level"]
        for c in api.get("/admin/pricing/readiness", headers=api.op).json()["checks"]
    }
    assert (
        r2["pricing_assignment_present"] == "PASS"
        and r2["pricing_currency_matches_account"] == "FAIL"
    )  # USD vs EUR
    assert r2["pricing_effective_version"] == "FAIL"  # libri i caktuar s'ka version


def test_pricing_endpoints_require_admin_for_writes_and_never_leak_internal_fields(api):
    bid, vid = ready(api)
    for method, url, body in (("post", "/admin/pricing/books", {"code": "zz", "name": "n", "currency": "EUR"}), ("post", f"/admin/pricing/books/{bid}/versions", None),
                              ("post", f"/admin/pricing/versions/{vid}/retire", {"reason": "x"}), ("post", "/admin/pricing/assignments", {})):  # fmt: skip
        kw = {"json": body} if body is not None else {}
        assert getattr(api, method)(url, headers=api.op, **kw).status_code == 403, url
    v = api.get(f"/admin/pricing/versions/{vid}", headers=api.op).json()
    assert set(v) == {"id", "book_id", "version", "status", "effective_from", "content_hash", "imported", "created_at", "activated_at",
                      "retired_at", "retire_reason", "editable", "rules", "rule_count"}  # fmt: skip
    assert "created_by_id" not in v and "_sa_instance_state" not in str(v)


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_triggers_back_up_the_api_immutability(api):
    if api.eng.dialect.name != "postgresql":
        pytest.skip("needs PostgreSQL triggers")
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    bid, vid = ready(api)
    with Session(api.eng) as s:
        with pytest.raises(DBAPIError):
            s.execute(
                text("UPDATE price_rules SET unit_price = 9 WHERE version_id = :v"), {"v": vid}
            )
        s.rollback()
        with pytest.raises(DBAPIError):
            s.execute(text("DELETE FROM price_rules WHERE version_id = :v"), {"v": vid})
        s.rollback()
        assert (
            s.scalar(
                select(func.count())
                .select_from(PriceVersion)
                .where(PriceVersion.id == uuid.UUID(vid))
            )
            == 1
        )
