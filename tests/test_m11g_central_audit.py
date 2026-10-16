# ruff: noqa: F811
"""M11-G — Central: audit READ-ONLY (staff/sistem) dhe historiku i vendimeve të sender-ave; RBAC, filtra, faqosje, eksport CSV."""

import csv
import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from apps.central.api import admin_audit
from apps.central.main import create_app
from apps.central.models import AuditLog
from apps.central.services import audit as audit_svc
from apps.central.services import senders as svc
from tests.test_central import make_db  # noqa: F401
from tests.test_central_auth import auth_secret, bearer, mk, token_for  # noqa: F401
from tests.test_central_products import cdb  # noqa: F401
from tests.test_m9g1_billing import U, b  # noqa: F401

T0 = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
def api(b, auth_secret):
    mk(b.eng, "ro@example.com", role="operator")
    c = TestClient(create_app(b.eng))
    return c, bearer(token_for(c, "a1@example.com")), bearer(token_for(c, "ro@example.com"))


def seed(b):
    """Politikë me miratim automatik (AL) + dy sender-a: një auto-miratuar, një i refuzuar, një i revokuar."""
    with b.F() as s:
        a = U(s, b.a1)
        svc.set_policy(s, a, "AL", "alphanumeric", True, False, "auto for AL", now=T0)
        svc.set_policy(s, a, "XK", "alphanumeric", True, True, "manual for XK", now=T0)
        auto = svc.request_sender(
            s, a, b.e1, "r-auto", "AL", "Acme", now=T0 + timedelta(minutes=1)
        ).sender
        man = svc.request_sender(
            s, a, b.e1, "r-man", "XK", "Manual", now=T0 + timedelta(minutes=2)
        ).sender
        svc.reject(s, a, man.id, "not ok", now=T0 + timedelta(minutes=3))
        other = svc.request_sender(
            s, a, b.e2, "r-o", "XK", "OtherCo", now=T0 + timedelta(minutes=4)
        ).sender
        svc.approve(s, a, other.id, now=T0 + timedelta(minutes=5))
        svc.revoke(s, a, other.id, "abuse", now=T0 + timedelta(minutes=6))
        s.commit()
        return auto.id, man.id, other.id


def test_rbac_and_read_only(api, b):
    c, ad, ro = api
    paths = ["/admin/audit", "/admin/audit/facets", "/admin/audit.csv",
             "/admin/audit/sender-decisions", "/admin/audit/sender-decisions.csv"]  # fmt: skip
    for p in paths:
        assert c.get(p).status_code == 401
        assert c.get(p, headers=ad).status_code == 200, p
        assert c.get(p, headers=ro).status_code == 200, p  # operator lexon
    spec = c.app.openapi()["paths"]
    for p in paths:
        assert set(spec[p]) == {"get"}, p  # asnjë veprim shkrimi


def test_sender_decision_history_filters_actors_and_auto_policy(api, b):
    c, ad, ro = api
    seed(b)
    g = lambda **p: c.get("/admin/audit/sender-decisions", params=p, headers=ro).json()  # noqa: E731
    d = g()
    items = d["items"]
    assert (
        items == sorted(items, key=lambda i: i["decided_at"], reverse=True)
        and d["has_more"] is False
    )
    auto = [i for i in items if i["category"] == "policy_auto_approved"]
    assert auto and auto[0]["actor_kind"] == "system" and auto[0]["decision"] == "approved"
    assert (
        auto[0]["policy_source"] == "explicit"
        and auto[0]["policy_revision"] == 1
        and auto[0]["enterprise"] == "Acme"
    )
    manual = [i for i in items if i["category"] == "manual"]
    assert {m["actor"] for m in manual} == {"a1@example.com"} and all(
        m["actor_kind"] == "user" for m in manual
    )
    assert {i["decision"] for i in g(decision="rejected")["items"]} == {"rejected"}
    assert g(decision="rejected")["items"][0]["reason"] == "not ok"
    assert {i["sender"] for i in g(country="xk")["items"]} == {"Manual", "OtherCo"}
    assert {i["enterprise"] for i in g(enterprise_id=str(b.e2))["items"]} == {"Beta"}
    assert {i["category"] for i in g(category="policy_revoked")["items"]} <= {"policy_revoked"}
    p1 = g(limit=3)
    assert len(p1["items"]) == 3 and p1["has_more"] is True
    assert g(limit=3, offset=3)["items"][0]["id"] != p1["items"][0]["id"]
    assert (
        c.get("/admin/audit/sender-decisions", params={"decision": "nope"}, headers=ro).status_code
        == 422
    )
    assert (
        c.get("/admin/audit/sender-decisions", params={"limit": 999}, headers=ro).status_code == 422
    )
    assert g(**{"from": "2001-01-01", "to": "2001-01-02"})["items"] == []
    assert (
        c.get(
            "/admin/audit/sender-decisions",
            params={"from": "2024-02-02", "to": "2024-02-01"},
            headers=ro,
        ).status_code
        == 422
    )


def test_audit_scopes_actor_identity_and_secret_scrubbing(api, b):
    c, ad, ro = api
    seed(b)
    with b.F() as s:
        audit_svc.record(s, U(s, b.a1), "payment.approve", "payment", "p-1",
                         {"before": {"status": "pending"}, "after": {"status": "approved", "api_key": "K", "token": "T", "note": "x" * 900}})  # fmt: skip
        audit_svc.record_system(s, label="system:enterprise_product_bootstrap", action="enterprise_product.bootstrap",
                                resource_type="enterprise_product", resource_id="e1")  # fmt: skip
        s.commit()
    g = lambda **p: c.get("/admin/audit", params=p, headers=ro).json()  # noqa: E731
    sender_actions = {i["action"] for i in g(scope="sender")["items"]}
    assert sender_actions and all(a.startswith("sender") for a in sender_actions)
    com = g(scope="commercial")["items"]
    assert [i["action"] for i in com] == ["payment.approve"]
    d = com[0]["detail"]
    assert "api_key" not in json.dumps(d) and "token" not in json.dumps(d)
    assert (
        d["after"]["note"].endswith("…")
        and len(d["after"]["note"]) <= 301
        and d["before"] == {"status": "pending"}
    )
    assert com[0]["actor"] == "a1@example.com" and com[0]["actor_kind"] == "user"
    ident = g(scope="identity")["items"]
    assert (
        ident[0]["actor_kind"] == "system"
        and ident[0]["actor"] == "system:enterprise_product_bootstrap"
    )
    assert {i["id"] for i in g(actor_kind="system")["items"]} >= {ident[0]["id"]}
    assert all(i["actor_kind"] == "system" for i in g(actor_kind="system")["items"])
    assert all(i["action"] == "payment.approve" for i in g(action="payment.approve")["items"])
    assert g(resource_type="payment")["items"][0]["resource_id"] == "p-1"
    assert g(actor_id=str(b.a1))["items"] and g(actor_id=str(b.op))["items"] == []
    assert len(g(scope="all", limit=2)["items"]) == 2
    f = c.get("/admin/audit/facets", headers=ro).json()
    assert "payment.approve" in f["actions"] and f["scopes"] == [
        "all",
        "sender",
        "commercial",
        "identity",
    ]
    assert c.get("/admin/audit", params={"scope": "nope"}, headers=ro).status_code == 422


def test_csv_exports_are_bounded_utf8_and_formula_safe(api, b):
    c, ad, ro = api
    seed(b)
    with b.F() as s:
        audit_svc.record(
            s, U(s, b.a1), "payment.approve", "payment", "=cmd|' /C calc'!A0", {"note": "ëç@x"}
        )
        s.commit()
    r = c.get("/admin/audit.csv", params={"scope": "commercial"}, headers=ro)
    assert r.headers["content-type"].startswith("text/csv; charset=utf-8") and r.content.startswith(
        b"\xef\xbb\xbf"
    )
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8").lstrip("﻿"))))
    assert rows[0][:5] == ["id", "at_utc", "actor_kind", "actor", "action"]
    assert rows[1][6].startswith("'=cmd") and "ëç@x" in rows[1][7]
    assert not any(cell[:1] in "=+-@" for row in rows for cell in row[3:7])
    sd = list(
        csv.reader(
            io.StringIO(c.get("/admin/audit/sender-decisions.csv", headers=ro).text.lstrip("﻿"))
        )
    )
    assert sd[0][0] == "decided_at_utc" and len(sd) > 3
    for p in ("/admin/audit.csv", "/admin/audit/sender-decisions.csv"):
        admin_audit.MAX_EXPORT_ROWS = 2
        try:
            r = c.get(p, headers=ro)
        finally:
            admin_audit.MAX_EXPORT_ROWS = 50_000
        assert r.status_code == 422 and r.json()["detail"]["code"] == "export_too_large"


def test_query_count_does_not_grow_with_rows(api, b):
    c, ad, ro = api
    seed(b)
    sel = []
    listener = lambda conn, cur, stmt, *a: sel.append(stmt)  # noqa: E731
    event.listen(b.eng, "before_cursor_execute", listener)
    try:
        counts = {}
        for path in ("/admin/audit", "/admin/audit/sender-decisions"):
            c.get(path, headers=ro)
            for lim in (2, 50):
                sel.clear()
                c.get(path, params={"limit": lim}, headers=ro)
                counts[(path, lim)] = len(sel)
    finally:
        event.remove(b.eng, "before_cursor_execute", listener)
    for path in ("/admin/audit", "/admin/audit/sender-decisions"):
        assert counts[(path, 2)] == counts[(path, 50)]


def test_audit_reads_write_nothing(api, b):
    c, ad, ro = api
    seed(b)
    with b.F() as s:
        before = s.scalar(select(func.count()).select_from(AuditLog))
    for p in (
        "/admin/audit",
        "/admin/audit/facets",
        "/admin/audit/sender-decisions",
        "/admin/audit.csv",
    ):
        c.get(p, headers=ro)
    with b.F() as s:
        assert s.scalar(select(func.count()).select_from(AuditLog)) == before
