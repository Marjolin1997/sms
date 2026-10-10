# ruff: noqa: F811
"""M9-f — API admin e parave tregtare (Central): RBAC, fluks, maker-checker, idempotencë, input strikt, pa DELETE."""

import uuid
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from apps.central.main import create_app
from apps.central.models import AuditLog, CommercialLedgerEntry, CreditAccount, CreditGrant, Payment
from apps.central.services import credit_accounts as accts
from apps.central.services import enterprises
from apps.central.services import products as prod_svc
from tests.test_central import make_db  # noqa: F401
from tests.test_central_auth import auth_secret, bearer, mk, token_for  # noqa: F401
from tests.test_central_products import cdb  # noqa: F401

AUDIT_PREFIXES = (
    "payment.",
    "credit_grant.",
    "credit_adjustment.",
    "debit_adjustment.",
    "credit_account.",
)


@pytest.fixture
def api(cdb):
    url, eng = cdb
    c = TestClient(create_app(eng))
    a1 = mk(eng, "a1@example.com", role="admin")
    mk(eng, "a2@example.com", role="admin")
    mk(eng, "op@example.com", role="operator")
    with Session(eng, expire_on_commit=False) as s:
        ent = enterprises.create(s, "Acme").id
        sms = prod_svc.create(s, "sms", "SMS", "sms").id
        acct = accts.create(s, ent, sms, "EUR", s.get(type(a1), a1.id)).id
        s.commit()
    c.eng, c.ent, c.sms, c.acct = eng, ent, sms, acct
    c.a1, c.a2 = bearer(token_for(c, "a1@example.com")), bearer(token_for(c, "a2@example.com"))
    c.op = bearer(token_for(c, "op@example.com"))
    return c


def fund(api, amount="100.000000", ref=None):
    """Pagesë e krijuar nga a1, miratuar nga a2 (maker-checker)."""
    body = {"account_id": str(api.acct), "amount": amount}
    if ref:
        body["external_reference"] = ref
    p = api.post("/admin/money/payments", json=body, headers=api.a1)
    assert p.status_code == 201, p.text
    r = api.post(f"/admin/money/payments/{p.json()['id']}/approve", headers=api.a2)
    assert r.status_code == 200, r.text
    return p.json()["id"]


def count(api, model, *where):
    with Session(api.eng) as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def audits(api, like):
    with Session(api.eng) as s:
        return list(
            s.scalars(
                select(AuditLog).where(AuditLog.action.like(like)).order_by(AuditLog.created_at)
            )
        )


# --- RBAC dhe sipërfaqja ---------------------------------------------------------------------------------------------------

ADMIN_PREFIXES = ("/admin/money", "/admin/pricing", "/admin/financial")


def _routes(app):
    out = []
    for path, ops in app.openapi()["paths"].items():
        if path.startswith(ADMIN_PREFIXES):
            out.extend((m.upper(), path) for m in sorted(ops))
    return out


def test_financial_admin_surface_has_no_delete_put_or_patch_anywhere():
    routes = _routes(create_app(create_engine("sqlite://")))
    assert routes, "financial admin routes must exist"
    assert {m for m, _ in routes} <= {"GET", "POST"}, [
        x for x in routes if x[0] not in ("GET", "POST")
    ]


def test_every_financial_admin_route_requires_authentication_and_writes_require_admin(api):
    sample = {"account_id": str(uuid.uuid4()), "payment_id": str(uuid.uuid4()), "grant_id": str(uuid.uuid4()),
              "book_id": str(uuid.uuid4()), "version_id": str(uuid.uuid4()), "report_id": str(uuid.uuid4())}  # fmt: skip
    for method, path in _routes(api.app):
        url = path.format(**sample)
        call = getattr(api, method.lower())
        kw = {"json": {}} if method == "POST" else {}
        assert call(url, **kw).status_code == 401, (method, path)
        r_op = call(url, headers=api.op, **kw)
        if method == "POST":
            assert r_op.status_code == 403, (
                method,
                path,
                r_op.status_code,
            )  # operator = vetëm lexim
        else:
            assert r_op.status_code != 403 and r_op.status_code != 401, (
                method,
                path,
                r_op.status_code,
            )


def test_operator_can_read_but_nothing_it_does_changes_state(api):
    fund(api)
    before = (count(api, Payment), count(api, CommercialLedgerEntry), count(api, AuditLog))
    for path in ("/admin/money/accounts", f"/admin/money/accounts/{api.acct}", "/admin/money/payments",
                 "/admin/money/grants", f"/admin/money/accounts/{api.acct}/ledger", "/admin/money/reconciliation",
                 "/admin/money/usage-reports", "/admin/financial/overview", "/admin/financial/alerts",
                 "/admin/financial/unresolved-reversals", "/admin/financial/readiness"):  # fmt: skip
        r = api.get(path, headers=api.op)
        assert r.status_code == 200, (path, r.text)
    assert before == (count(api, Payment), count(api, CommercialLedgerEntry), count(api, AuditLog))


# --- pagesat ---------------------------------------------------------------------------------------------------


def test_payment_lifecycle_maker_checker_and_idempotent_approval(api):
    r = api.post("/admin/money/payments", json={"account_id": str(api.acct), "amount": "25.5",
                                                "external_reference": "bank-1", "note": "wire"}, headers=api.a1)  # fmt: skip
    assert (
        r.status_code == 201
        and r.json()["status"] == "pending"
        and r.json()["amount"] == "25.500000"
    )
    pid = r.json()["id"]
    assert r.json()["created_by"]["user_id"] and r.json()["approved_by"] is None
    # krijuesi s'e miraton (maker-checker)
    assert api.post(f"/admin/money/payments/{pid}/approve", headers=api.a1).status_code == 409
    assert count(api, CommercialLedgerEntry) == 0
    ok = api.post(f"/admin/money/payments/{pid}/approve", headers=api.a2)
    assert ok.status_code == 200 and ok.json()["status"] == "approved"
    assert ok.json()["approved_by"]["user_id"] != r.json()["created_by"]["user_id"]
    again = api.post(
        f"/admin/money/payments/{pid}/approve", headers=api.a2
    )  # replay ⇒ asnjë kredi e dytë
    assert again.status_code == 200 and count(api, CommercialLedgerEntry) == 1
    assert len(audits(api, "payment.approve")) == 1
    # i miratuar s'refuzohet kurrë
    assert (
        api.post(
            f"/admin/money/payments/{pid}/reject", json={"reason": "oops"}, headers=api.a2
        ).status_code
        == 409
    )
    d = api.get(f"/admin/money/accounts/{api.acct}", headers=api.op).json()
    assert d["totals"]["funds"] == "25.500000" and d["totals"]["available_to_grant"] == "25.500000"


def test_external_reference_is_unique_and_replay_returns_the_same_payment(api):
    body = {
        "account_id": str(api.acct),
        "amount": "10",
        "external_reference": "ref-001",
        "source": "bank",
    }
    a = api.post("/admin/money/payments", json=body, headers=api.a1)
    b = api.post("/admin/money/payments", json=body, headers=api.a2)
    assert a.status_code == b.status_code == 201 and a.json()["id"] == b.json()["id"]
    assert count(api, Payment) == 1
    different = api.post("/admin/money/payments", json={**body, "amount": "11"}, headers=api.a1)
    assert different.status_code == 409 and count(api, Payment) == 1
    other_source = api.post(
        "/admin/money/payments", json={**body, "source": "card"}, headers=api.a1
    )  # çelës i skopuar
    assert other_source.status_code == 201 and count(api, Payment) == 2


def test_rejected_payment_is_final_and_reason_is_mandatory(api):
    p = api.post(
        "/admin/money/payments", json={"account_id": str(api.acct), "amount": "5"}, headers=api.a1
    ).json()
    assert (
        api.post(f"/admin/money/payments/{p['id']}/reject", json={}, headers=api.a2).status_code
        == 422
    )
    assert api.post(
        f"/admin/money/payments/{p['id']}/reject", json={"reason": "   "}, headers=api.a2
    ).status_code in (409, 422)
    r = api.post(
        f"/admin/money/payments/{p['id']}/reject", json={"reason": "duplicate wire"}, headers=api.a2
    )
    assert (
        r.status_code == 200
        and r.json()["status"] == "rejected"
        and r.json()["rejection_reason"] == "duplicate wire"
    )
    assert api.post(f"/admin/money/payments/{p['id']}/approve", headers=api.a2).status_code == 409
    assert count(api, CommercialLedgerEntry) == 0
    assert len(audits(api, "payment.reject")) == 1


def test_payment_list_filters_and_pagination_envelope(api):
    ids = [
        api.post(
            "/admin/money/payments",
            json={"account_id": str(api.acct), "amount": str(i + 1)},
            headers=api.a1,
        ).json()["id"]
        for i in range(5)
    ]
    api.post(f"/admin/money/payments/{ids[0]}/approve", headers=api.a2)
    page1 = api.get("/admin/money/payments?limit=2", headers=api.op).json()
    assert len(page1["items"]) == 2 and page1["has_more"] is True and page1["limit"] == 2
    page3 = api.get("/admin/money/payments?limit=2&offset=4", headers=api.op).json()
    assert len(page3["items"]) == 1 and page3["has_more"] is False
    assert [
        p["id"]
        for p in api.get("/admin/money/payments?status=approved", headers=api.op).json()["items"]
    ] == [ids[0]]
    assert api.get("/admin/money/payments?status=bogus", headers=api.op).status_code == 422
    assert api.get("/admin/money/payments?limit=1000", headers=api.op).status_code == 422
    assert api.get(f"/admin/money/payments/{uuid.uuid4()}", headers=api.op).status_code == 404


# --- grant-et -----------------------------------------------------------------------------------------------------


def test_grant_issue_idempotent_insufficient_and_reverse(api):
    fund(api, "100")
    g = api.post(
        "/admin/money/grants",
        json={"account_id": str(api.acct), "amount": "60", "idempotency_key": "grant-key-0001"},
        headers=api.a1,
    )
    assert (
        g.status_code == 201
        and g.json()["status"] == "active"
        and g.json()["purpose"] == "standard"
    )
    same = api.post(
        "/admin/money/grants",
        json={"account_id": str(api.acct), "amount": "60", "idempotency_key": "grant-key-0001"},
        headers=api.a2,
    )
    assert same.json()["id"] == g.json()["id"] and count(api, CreditGrant) == 1
    diff = api.post(
        "/admin/money/grants",
        json={"account_id": str(api.acct), "amount": "61", "idempotency_key": "grant-key-0001"},
        headers=api.a1,
    )
    assert diff.status_code == 409
    over = api.post(
        "/admin/money/grants",
        json={
            "account_id": str(api.acct),
            "amount": "40.000001",
            "idempotency_key": "grant-key-0002",
        },
        headers=api.a1,
    )
    assert over.status_code == 409 and count(api, CreditGrant) == 1
    exact = api.post(
        "/admin/money/grants",
        json={"account_id": str(api.acct), "amount": "40", "idempotency_key": "grant-key-0003"},
        headers=api.a1,
    )
    assert exact.status_code == 201
    rev = api.post(
        f"/admin/money/grants/{g.json()['id']}/reverse",
        json={"reason": "customer refund"},
        headers=api.a1,
    )
    assert (
        rev.status_code == 200
        and rev.json()["status"] == "reversed"
        and rev.json()["reversal_reason"] == "customer refund"
    )
    assert (
        api.post(
            f"/admin/money/grants/{g.json()['id']}/reverse",
            json={"reason": "again"},
            headers=api.a1,
        ).status_code
        == 200
    )  # no-op
    assert len(audits(api, "credit_grant.reverse")) == 1
    d = api.get(f"/admin/money/accounts/{api.acct}", headers=api.op).json()["totals"]
    assert d["available_to_grant"] == "60.000000" and d["outstanding_grants"] == "40.000000"
    assert [
        x["status"]
        for x in api.get("/admin/money/grants?status=reversed", headers=api.op).json()["items"]
    ] == ["reversed"]


def test_bootstrap_grants_and_unknown_fields_cannot_be_created_through_the_api(api):
    fund(api, "100")
    base = {"account_id": str(api.acct), "amount": "1", "idempotency_key": "grant-key-0001"}
    for extra in (
        {"purpose": "bootstrap"},
        {"baseline_ref": "ab" * 32},
        {"enterprise_id": str(api.ent)},
        {"status": "reversed"},
    ):
        assert (
            api.post("/admin/money/grants", json={**base, **extra}, headers=api.a1).status_code
            == 422
        ), extra
    assert count(api, CreditGrant) == 0


def test_grant_from_unapproved_or_foreign_payment_is_refused(api):
    fund(api, "100")
    pend = api.post(
        "/admin/money/payments", json={"account_id": str(api.acct), "amount": "5"}, headers=api.a1
    ).json()["id"]
    r = api.post(
        "/admin/money/grants",
        json={
            "account_id": str(api.acct),
            "amount": "1",
            "idempotency_key": "grant-key-0001",
            "source_payment_id": pend,
        },
        headers=api.a1,
    )
    assert r.status_code == 409
    r = api.post(
        "/admin/money/grants",
        json={
            "account_id": str(api.acct),
            "amount": "1",
            "idempotency_key": "grant-key-0002",
            "source_payment_id": str(uuid.uuid4()),
        },
        headers=api.a1,
    )
    assert r.status_code == 404


# --- rregullimet dhe ledger -------------------------------------------------------------------------------------------


def test_adjustments_are_ledger_entries_with_reason_and_idempotency_and_never_go_negative(api):
    fund(api, "50")
    body = {
        "kind": "credit",
        "amount": "5",
        "reason": "goodwill",
        "idempotency_key": "adj-key-00001",
    }
    a = api.post(f"/admin/money/accounts/{api.acct}/adjustments", json=body, headers=api.a1)
    assert (
        a.status_code == 201
        and a.json()["entry_type"] == "manual_credit_adjustment"
        and a.json()["reason"] == "goodwill"
    )
    again = api.post(f"/admin/money/accounts/{api.acct}/adjustments", json=body, headers=api.a1)
    assert again.json()["seq"] == a.json()["seq"] and count(api, CommercialLedgerEntry) == 2
    clash = api.post(
        f"/admin/money/accounts/{api.acct}/adjustments",
        json={**body, "amount": "6"},
        headers=api.a1,
    )
    assert clash.status_code == 409
    over = api.post(f"/admin/money/accounts/{api.acct}/adjustments",
                    json={"kind": "debit", "amount": "55.000001", "reason": "fix", "idempotency_key": "adj-key-00002"}, headers=api.a1)  # fmt: skip
    assert over.status_code == 409
    ok = api.post(f"/admin/money/accounts/{api.acct}/adjustments",
                  json={"kind": "debit", "amount": "55", "reason": "fix", "idempotency_key": "adj-key-00003"}, headers=api.a1)  # fmt: skip
    assert ok.status_code == 201
    assert (
        api.post(
            f"/admin/money/accounts/{api.acct}/adjustments",
            json={**body, "kind": "mint", "idempotency_key": "adj-key-00004"},
            headers=api.a1,
        ).status_code
        == 422
    )
    t = api.get(f"/admin/money/accounts/{api.acct}", headers=api.op).json()["totals"]
    assert t["available_to_grant"] == "0.000000"
    assert (
        len(audits(api, "credit_adjustment.create")) == 1
        and len(audits(api, "debit_adjustment.create")) == 1
    )


def test_ledger_history_is_read_only_ordered_and_paginated(api):
    fund(api, "10")
    for i in range(3):
        api.post(f"/admin/money/accounts/{api.acct}/adjustments",
                 json={"kind": "credit", "amount": "1", "reason": "r", "idempotency_key": f"adj-key-0000{i}"}, headers=api.a1)  # fmt: skip
    p1 = api.get(f"/admin/money/accounts/{api.acct}/ledger?limit=2", headers=api.op).json()
    assert [e["seq"] for e in p1["items"]] == sorted(e["seq"] for e in p1["items"]) and p1[
        "next_after_seq"
    ] == p1["items"][-1]["seq"]
    p2 = api.get(
        f"/admin/money/accounts/{api.acct}/ledger?limit=2&after_seq={p1['next_after_seq']}",
        headers=api.op,
    ).json()
    assert (
        len(p2["items"]) == 2 and p2["next_after_seq"] is not None or p2["next_after_seq"] is None
    )
    allrows = api.get(f"/admin/money/accounts/{api.acct}/ledger?limit=500", headers=api.op).json()[
        "items"
    ]
    assert [e["entry_type"] for e in allrows] == [
        "payment_credit",
        "manual_credit_adjustment",
        "manual_credit_adjustment",
        "manual_credit_adjustment",
    ]
    assert (
        api.get(f"/admin/money/accounts/{uuid.uuid4()}/ledger", headers=api.op).status_code == 404
    )


def test_suspended_account_blocks_new_money_and_status_changes_are_audited(api):
    fund(api, "10")
    r = api.post(
        f"/admin/money/accounts/{api.acct}/status",
        json={"status": "suspended", "reason": "fraud review"},
        headers=api.a1,
    )
    assert r.status_code == 200 and r.json()["status"] == "suspended"
    assert (
        api.post(
            "/admin/money/payments",
            json={"account_id": str(api.acct), "amount": "1"},
            headers=api.a1,
        ).status_code
        == 409
    )
    assert (
        api.post(
            "/admin/money/grants",
            json={"account_id": str(api.acct), "amount": "1", "idempotency_key": "grant-key-0001"},
            headers=api.a1,
        ).status_code
        == 409
    )
    assert (
        api.post(
            f"/admin/money/accounts/{api.acct}/status",
            json={"status": "suspended", "reason": "again"},
            headers=api.a1,
        ).status_code
        == 200
    )  # no-op
    assert len(audits(api, "credit_account.status_change")) == 1
    r = api.post(
        f"/admin/money/accounts/{api.acct}/status",
        json={"status": "active", "reason": "cleared"},
        headers=api.a2,
    )
    assert r.json()["status"] == "active" and len(audits(api, "credit_account.status_change")) == 2
    assert (
        api.post(
            f"/admin/money/accounts/{api.acct}/status",
            json={"status": "deleted", "reason": "x"},
            headers=api.a1,
        ).status_code
        == 422
    )
    assert (
        api.post(
            f"/admin/money/accounts/{api.acct}/status", json={"status": "active"}, headers=api.a1
        ).status_code
        == 422
    )


# --- input strikt ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("amount", [1.5, 100, True, None, [], {}, "", "abc", "-1", "0", "0.0", "1e3", "1E3", "1.5e1", "+5", " 5", "5 ",
                                    "1,5", "1.1234567", ".5", "5.", "NaN", "Infinity", "-0", "00012", "99999999999999.5", "10000000000000", "٣"])  # fmt: skip
def test_payment_amount_must_be_a_plain_positive_decimal_string(api, amount):
    r = api.post(
        "/admin/money/payments",
        json={"account_id": str(api.acct), "amount": amount},
        headers=api.a1,
    )
    assert r.status_code == 422, (amount, r.text)
    assert count(api, Payment) == 0


@pytest.mark.parametrize(
    "amount,expected",
    [
        ("1", "1.000000"),
        ("0.000001", "0.000001"),
        ("9999999999999.999999", "9999999999999.999999"),
        ("12.34", "12.340000"),
    ],
)
def test_valid_amounts_are_stored_exactly(api, amount, expected):
    r = api.post(
        "/admin/money/payments",
        json={"account_id": str(api.acct), "amount": amount},
        headers=api.a1,
    )
    assert r.status_code == 201 and r.json()["amount"] == expected
    if (
        api.eng.dialect.name == "postgresql"
    ):  # SQLite ruan NUMERIC si float: vetëm PostgreSQL e provon saktësinë e kolonës
        with Session(api.eng) as s:
            assert s.scalar(select(Payment.amount)) == D(expected)


def test_payment_body_is_strict_about_fields_types_and_lengths(api):
    ok = {"account_id": str(api.acct), "amount": "1"}
    bad = [
        {**ok, "extra": 1},
        {**ok, "status": "approved"},
        {**ok, "created_by_id": str(uuid.uuid4())},
        {**ok, "enterprise_id": str(api.ent)},
        {**ok, "account_id": "not-a-uuid"},
        {**ok, "account_id": 5},
        {**ok, "currency": "eur"},
        {**ok, "currency": "EURO"},
        {**ok, "currency": 5},
        {**ok, "note": "x" * 501},
        {**ok, "source": 5},
        {**ok, "source": "Bad Source!"},
        {**ok, "external_reference": "x" * 129},
        {**ok, "external_reference": "bad\nref"},
        {"amount": "1"},
        {"account_id": str(api.acct)},
        [],
        "x",
    ]
    for b in bad:
        assert api.post("/admin/money/payments", json=b, headers=api.a1).status_code == 422, b
    wrong_cur = api.post("/admin/money/payments", json={**ok, "currency": "USD"}, headers=api.a1)
    assert wrong_cur.status_code == 409  # monedha duhet të përputhet me llogarinë (V1)
    assert (
        api.post(
            "/admin/money/payments", json={**ok, "account_id": str(uuid.uuid4())}, headers=api.a1
        ).status_code
        == 404
    )
    assert count(api, Payment) == 0


def test_grant_adjustment_and_status_bodies_are_strict(api):
    fund(api, "10")
    g = {"account_id": str(api.acct), "amount": "1", "idempotency_key": "grant-key-0001"}
    for b in ({**g, "idempotency_key": "short"}, {**g, "idempotency_key": "has space in it"}, {**g, "idempotency_key": "x" * 129},
              {**g, "idempotency_key": None}, {**g, "amount": 1}, {**g, "note": "n" * 501}, {**g, "source_payment_id": "zzz"}):  # fmt: skip
        assert api.post("/admin/money/grants", json=b, headers=api.a1).status_code == 422, b
    a = {"kind": "credit", "amount": "1", "reason": "r", "idempotency_key": "adj-key-00001"}
    for b in ({**a, "reason": ""}, {**a, "reason": "r" * 501}, {**a, "reason": None}, {**a, "kind": 1}, {**a, "amount": 1.0},
              {**a, "idempotency_key": "x"}, {**a, "x": 1}):  # fmt: skip
        assert (
            api.post(
                f"/admin/money/accounts/{api.acct}/adjustments", json=b, headers=api.a1
            ).status_code
            == 422
        ), b
    assert api.post(
        f"/admin/money/accounts/{api.acct}/adjustments",
        json={**a, "reason": "bad\x00char"},
        headers=api.a1,
    ).status_code in (409, 422)
    assert count(api, CommercialLedgerEntry) == 1  # vetëm fondimi


def test_malformed_json_and_wrong_content_types_are_rejected_without_side_effects(api):
    r = api.post(
        "/admin/money/payments",
        content="{not json",
        headers={**api.a1, "Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert (
        api.post(
            "/admin/money/payments",
            content="amount=1",
            headers={**api.a1, "Content-Type": "text/plain"},
        ).status_code
        == 422
    )
    assert api.post("/admin/money/payments", headers=api.a1).status_code == 422
    assert count(api, Payment) == 0


# --- audit: asnjë sekret/PII e panevojshme, çdo mutacion një rresht ----------------------------------------------------------


def test_every_money_mutation_writes_exactly_one_audit_row_with_a_human_actor_and_no_secrets(api):
    fund(api, "100", ref="bank-xyz")
    g = api.post(
        "/admin/money/grants",
        json={"account_id": str(api.acct), "amount": "10", "idempotency_key": "grant-key-0001"},
        headers=api.a1,
    ).json()
    api.post(f"/admin/money/grants/{g['id']}/reverse", json={"reason": "refund"}, headers=api.a1)
    api.post(
        f"/admin/money/accounts/{api.acct}/adjustments",
        json={"kind": "credit", "amount": "1", "reason": "gw", "idempotency_key": "adj-key-00001"},
        headers=api.a1,
    )
    api.post(
        f"/admin/money/accounts/{api.acct}/status",
        json={"status": "suspended", "reason": "r"},
        headers=api.a1,
    )
    rows = audits(api, "%")
    actions = [r.action for r in rows if r.action.startswith(AUDIT_PREFIXES)]
    assert sorted(actions) == sorted(["credit_account.create", "payment.create", "payment.approve", "credit_grant.create",
                                      "credit_grant.reverse", "credit_adjustment.create", "credit_account.status_change"])  # fmt: skip
    for r in rows:
        assert r.actor_kind == "user" and r.actor_id is not None
        blob = str(r.detail).lower()
        assert not any(
            w in blob for w in ("password", "secret", "token", "bearer", "authorization")
        )


def test_reconciliation_and_usage_report_endpoints_shape_with_no_data(api):
    r = api.get("/admin/money/reconciliation?min_severity=INFO", headers=api.op).json()
    assert r["status"] == "PASS" and r["discrepancies"] == [] and r["min_severity"] == "INFO"
    assert (
        api.get("/admin/money/reconciliation?min_severity=bogus", headers=api.op).status_code == 422
    )
    assert api.get("/admin/money/usage-reports", headers=api.op).json() == {"items": []}
    assert api.get(f"/admin/money/usage-reports/{uuid.uuid4()}", headers=api.op).status_code == 404
    h = api.get(
        f"/admin/money/usage-reports/history?enterprise_id={api.ent}&product_id={api.sms}&currency=EUR",
        headers=api.op,
    )
    assert h.status_code == 200 and h.json() == {"items": []}
    assert (
        api.get(
            f"/admin/money/usage-reports/history?enterprise_id={api.ent}&product_id={api.sms}&currency=eur",
            headers=api.op,
        ).status_code
        == 422
    )


def test_responses_never_expose_orm_internals_or_idempotency_hashes(api):
    fund(api, "10")
    g = api.post(
        "/admin/money/grants",
        json={"account_id": str(api.acct), "amount": "1", "idempotency_key": "grant-key-0001"},
        headers=api.a1,
    ).json()
    assert (
        "request_hash" not in g
        and "idempotency_key" not in g
        and "_sa_instance_state" not in str(g)
    )
    p = api.get("/admin/money/payments", headers=api.op).json()["items"][0]
    assert set(p) == {"id", "account_id", "enterprise_id", "currency", "amount", "source", "external_reference", "note", "status",
                      "created_by", "created_at", "approved_by", "approved_at", "rejected_by", "rejected_at", "rejection_reason"}  # fmt: skip
    a = api.get("/admin/money/accounts", headers=api.op).json()["items"][0]
    assert set(a) == {
        "id",
        "enterprise_id",
        "product_id",
        "currency",
        "status",
        "created_at",
        "updated_at",
        "totals",
    }
    assert isinstance(accts.get(Session(api.eng), api.acct), CreditAccount)
