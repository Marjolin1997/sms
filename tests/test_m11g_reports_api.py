# ruff: noqa: F811
"""M11-G — raporte/audit vetëm-lexim për klientin: overview, campaign report, failures, audit, eksporte CSV."""

import csv
import io
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, select, text

from app.models.admin import AuditLog
from app.models.sending import Message, MessageStatus
from app.services import csv_export
from tests.test_billing import _key, fresh_gateway, raw_client  # noqa: F401
from tests.test_campaigns import audience, campaign, drive, start  # noqa: F401
from tests.test_central import enterprise_alembic, make_db  # noqa: F401
from tests.test_m11f_messaging_api import run_campaign, selects
from tests.test_pipeline import fake, world  # noqa: F401


@pytest.fixture
def env(db, world, raw_client):
    c = raw_client
    return SimpleNamespace(c=c, h1=_key(c, "client", "c1"), h2=_key(c, "client", "c2"), db=db)


def audit_row(db, owner, action="campaign.pause", **kw):
    db.add(AuditLog(actor=kw.pop("actor", "client:k"), role=kw.pop("role", "client"), action=action,
                    target_type=kw.pop("target_type", "campaign"), target_id=kw.pop("target_id", "1"),
                    owner_ref=owner, detail=kw.pop("detail", None)))  # fmt: skip
    db.commit()


def rows_of(text_):
    return list(csv.reader(io.StringIO(text_.lstrip("﻿"))))


# ---------------- overview ----------------


def test_overview_counts_public_statuses_and_unknown_is_sending(env):
    camp, *_ = run_campaign(env.db, "promo", n=3)
    ms = env.db.scalars(select(Message).order_by(Message.id)).all()
    ms[0].status, ms[0].error_code = MessageStatus.DELIVERED, None
    ms[1].status, ms[1].error_code = MessageStatus.FAILED, "no_route"
    ms[2].status = MessageStatus.UNKNOWN
    env.db.commit()
    d = env.c.get("/v1/reports/overview", headers=env.h1).json()
    assert d["timezone"] == "UTC" and d["messages"]["total"] == 3
    assert d["messages"]["by_status"] == {"delivered": 1, "failed": 1, "sending": 1}
    assert "unknown" not in json.dumps(d)
    assert d["messages"]["delivery_rate"] == 50.0  # delivered ÷ (delivered + failed); sending s'hyn
    assert d["top_failures"] == [{"status": "failed", "error_code": "no_route", "count": 1}]
    assert d["campaigns"]["by_status"] == {"completed": 1}
    assert d["recent_campaigns"][0]["id"] == camp.id


def test_overview_rate_is_null_without_terminal_messages(env):
    run_campaign(env.db, "promo", n=2)  # queued
    assert (
        env.c.get("/v1/reports/overview", headers=env.h1).json()["messages"]["delivery_rate"]
        is None
    )


def test_range_validation_and_defaults(env):
    g = lambda **p: env.c.get("/v1/reports/overview", params=p, headers=env.h1)  # noqa: E731
    assert g(**{"from": "2026-02-02", "to": "2026-02-01"}).status_code == 422
    assert g(**{"from": "2024-01-01", "to": "2026-01-01"}).status_code == 422  # > 366 ditë
    assert g(**{"from": "2026-02-01", "to": "2026-02-01"}).status_code == 200
    assert g(**{"from": "not-a-date"}).status_code == 422


def test_range_excludes_rows_outside_the_inclusive_utc_days(env):
    run_campaign(env.db, "promo", n=2)
    old = (datetime.now(UTC) - timedelta(days=5)).date().isoformat()
    d = env.c.get("/v1/reports/overview", params={"from": old, "to": old}, headers=env.h1).json()
    assert d["messages"]["total"] == 0 and d["campaigns"]["by_status"] == {}


# ---------------- campaign report ----------------


def test_campaign_report_filters_pagination_and_delivery_rate(env):
    made = [run_campaign(env.db, n, base=355691231010 + 100 * i)[0]
            for i, n in enumerate(["alpha sale", "beta sale", "gamma club"])]  # fmt: skip
    for m in env.db.scalars(select(Message).order_by(Message.id)).all()[:2]:
        m.status = MessageStatus.DELIVERED
    env.db.commit()
    g = lambda **p: env.c.get("/v1/reports/campaigns", params=p, headers=env.h1)  # noqa: E731
    r = g().json()
    assert [c["name"] for c in r] == ["gamma club", "beta sale", "alpha sale"]
    assert {"recipients_total", "message_counts", "delivery_rate", "created_at", "status"} <= set(
        r[0]
    )
    assert [c["name"] for c in g(q="sale").json()] == ["beta sale", "alpha sale"]
    assert g(q="%").json() == [] and len(g(status="completed").json()) == 3
    assert g(status="draft").json() == [] and g(status="nope").status_code == 422
    p1 = g(limit=2).json()
    assert [c["id"] for c in g(limit=2, before_id=p1[-1]["id"]).json()] == [made[0].id]
    alpha = next(c for c in r if c["name"] == "alpha sale")
    assert alpha["delivery_rate"] == 100.0


def test_campaign_report_query_count_independent_of_rows(env):
    for i in range(4):
        run_campaign(env.db, f"c{i}", base=355691240000 + 100 * i)
    env.c.get("/v1/reports/campaigns", headers=env.h1)
    one = selects(
        env.db, lambda: env.c.get("/v1/reports/campaigns", params={"limit": 1}, headers=env.h1)
    )
    four = selects(
        env.db, lambda: env.c.get("/v1/reports/campaigns", params={"limit": 4}, headers=env.h1)
    )
    assert one == four
    env.c.get("/v1/reports/overview", headers=env.h1)
    n = selects(env.db, lambda: env.c.get("/v1/reports/overview", headers=env.h1))
    assert n <= 14


# ---------------- failures ----------------


def test_failures_group_by_status_and_code_and_filter_by_campaign(env):
    a, *_ = run_campaign(env.db, "a", n=3, base=355691231010)
    b, *_ = run_campaign(env.db, "b", n=1, base=355691239010)
    ms = env.db.scalars(select(Message).order_by(Message.id)).all()
    for m, code in zip(ms[:3], ["no_route", "no_route", "weird_code_42"], strict=True):
        m.status, m.error_code = MessageStatus.FAILED, code
    ms[3].status = MessageStatus.UNKNOWN
    env.db.commit()
    d = env.c.get("/v1/reports/failures", headers=env.h1).json()
    assert d["total_messages"] == 4 and d["by_status"] == {"failed": 3, "sending": 1}
    assert d["rows"][0] == {"status": "failed", "error_code": "no_route", "count": 2}
    assert {"status": "failed", "error_code": "weird_code_42", "count": 1} in d[
        "rows"
    ]  # kod i panjohur mbetet
    assert all(r["status"] != "unknown" for r in d["rows"]) and d["truncated"] is False
    one = env.c.get("/v1/reports/failures", params={"campaign_id": b.id}, headers=env.h1).json()
    assert one["total_messages"] == 1 and one["rows"] == []  # UNKNOWN → sending, jo failed
    assert (
        env.c.get("/v1/reports/failures", params={"campaign_id": a.id}, headers=env.h2).status_code
        == 404
    )


# ---------------- audit ----------------


def test_audit_is_tenant_scoped_actor_safe_and_whitelists_detail(env):
    audit_row(
        env.db,
        "c1",
        "campaign.pause",
        detail=json.dumps({"at": "x", "url": "https://s", "owner": "c1", "token": "t0k"}),
    )
    audit_row(env.db, "c2", "campaign.cancel", target_id="9")
    audit_row(
        env.db,
        "c1",
        "wallet.adjust",
        role="finance",
        actor="staff-person@example.com",
        target_type="wallet",
    )
    audit_row(env.db, "c1", "cross_tenant.read", role="support", actor="staff-person@example.com")
    audit_row(env.db, None, "legacy.row")  # histori para 0034 / globale
    r = [
        a
        for a in env.c.get("/v1/reports/audit", headers=env.h1).json()
        if a["action"] != "apikey.create"
    ]
    assert [a["action"] for a in r] == [
        "wallet.adjust",
        "campaign.pause",
    ]  # apikey.create = fixture (staff)
    pause = r[1]
    assert pause["by"] == "you" and r[0]["by"] == "staff"
    assert pause["detail"] == {"at": "x"}
    blob = json.dumps(r)
    assert "staff-person" not in blob and "t0k" not in blob and "https://s" not in blob
    r2 = [a["action"] for a in env.c.get("/v1/reports/audit", headers=env.h2).json()]
    assert "campaign.cancel" in r2 and "campaign.pause" not in r2 and "wallet.adjust" not in r2
    f = env.c.get("/v1/reports/audit/facets", headers=env.h1).json()
    assert {"campaign.pause", "wallet.adjust"} <= set(
        f["actions"]
    ) and "cross_tenant.read" not in f["actions"]
    assert "legacy.row" not in f["actions"] and {"campaign", "wallet"} <= set(f["target_types"])


def test_audit_filters_and_keyset(env):
    for i in range(5):
        audit_row(env.db, "c1", "campaign.pause" if i % 2 else "campaign.resume", target_id=str(i))
    g = lambda **p: env.c.get("/v1/reports/audit", params=p, headers=env.h1).json()  # noqa: E731
    assert len(g(action="campaign.pause")) == 2 and g(target_type="nope") == []
    p1 = g(limit=2)
    assert (
        len(p1) == 2
        and [a["id"] for a in g(limit=2, before_id=p1[-1]["id"])]
        == sorted([a["id"] for a in g(limit=10) if a["id"] < p1[-1]["id"]], reverse=True)[:2]
    )
    assert g(**{"from": "2001-01-01", "to": "2001-01-02"}) == []


def test_new_audit_rows_carry_the_owner_from_the_principal(env):
    lst, _ = audience(env.db, 2)
    camp = campaign(env.db, lst, name="draft one")
    assert (
        env.c.post(f"/v1/campaigns/{camp.id}/schedule", json={}, headers=env.h1).status_code == 200
    )
    owners = {
        a.owner_ref
        for a in env.db.scalars(select(AuditLog).where(AuditLog.action.like("campaign.%")))
    }
    assert owners == {"c1"}


# ---------------- CSV ----------------


def test_csv_neutralizes_formulas_keeps_utf8_and_numbers(env):
    run_campaign(env.db, "=HYPERLINK(1)", n=1, base=355691231010)
    run_campaign(env.db, "@cmd ëç", n=1, base=355691239010)
    r = env.c.get("/v1/reports/campaigns.csv", headers=env.h1)
    assert r.headers["content-type"].startswith("text/csv; charset=utf-8")
    assert r.content.startswith(b"\xef\xbb\xbf") and "ëç" in r.content.decode("utf-8")
    names = [row[1] for row in rows_of(r.text)[1:]]
    assert "'=HYPERLINK(1)" in names and "'@cmd ëç" in names
    assert not any(n[:1] in "=+-@" for n in names)
    assert (
        csv_export.safe_cell("+35569123") == "+35569123" and csv_export.safe_cell("-1+1") == "'-1+1"
    )
    assert csv_export.safe_cell("\tx") == "'\tx" and csv_export.safe_cell("\rx") == "'\rx"


def test_messages_csv_never_exports_unknown_and_supports_filters(env):
    camp, *_ = run_campaign(env.db, "promo", n=2)
    ms = env.db.scalars(select(Message).order_by(Message.id)).all()
    ms[0].status = MessageStatus.UNKNOWN
    ms[1].status, ms[1].error_code = MessageStatus.FAILED, "no_route"
    env.db.commit()
    rows = rows_of(env.c.get("/v1/reports/messages.csv", headers=env.h1).text)
    assert {r[4] for r in rows[1:]} == {"sending", "failed"}
    only = rows_of(
        env.c.get("/v1/reports/messages.csv", params={"status": "failed"}, headers=env.h1).text
    )
    assert len(only) == 2 and only[1][9] == "no_route"
    by_c = rows_of(
        env.c.get("/v1/reports/messages.csv", params={"campaign_id": camp.id}, headers=env.h1).text
    )
    assert len(by_c) == 3
    assert (
        env.c.get(
            "/v1/reports/messages.csv", params={"campaign_id": camp.id}, headers=env.h2
        ).status_code
        == 404
    )
    assert (
        env.c.get(
            "/v1/reports/messages.csv", params={"status": "unknown"}, headers=env.h1
        ).status_code
        == 422
    )


def test_export_row_limit_is_an_explicit_422_before_any_body(env, monkeypatch):
    run_campaign(env.db, "promo", n=3)
    monkeypatch.setattr(csv_export.require_bounded, "__defaults__", (2,))
    r = env.c.get("/v1/reports/messages.csv", headers=env.h1)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "export_too_large"
    assert "narrow" in r.json()["detail"]["message"]
    n_before = env.db.scalar(
        select(text("count(*)")).select_from(AuditLog).where(AuditLog.action == "report.export")
    )
    assert n_before == 0  # eksporti i refuzuar nuk audit-ohet si i kryer


def test_campaign_recipients_audit_and_usage_exports_are_scoped(env):
    camp, *_ = run_campaign(env.db, "promo", n=2)
    audit_row(env.db, "c1", "campaign.pause")
    audit_row(env.db, "c2", "campaign.cancel")
    rec = rows_of(env.c.get(f"/v1/reports/campaigns/{camp.id}/recipients.csv", headers=env.h1).text)
    assert len(rec) == 3 and rec[1][1].startswith("+3556912")
    assert (
        env.c.get(f"/v1/reports/campaigns/{camp.id}/recipients.csv", headers=env.h2).status_code
        == 404
    )
    au = rows_of(env.c.get("/v1/reports/audit.csv", headers=env.h1).text)
    assert au[0][:4] == ["id", "at_utc", "by", "action"]
    assert "campaign.pause" in {r[3] for r in au[1:]} and "campaign.cancel" not in {
        r[3] for r in au[1:]
    }
    us = rows_of(env.c.get("/v1/reports/usage.csv", headers=env.h1).text)
    assert us[0][0] == "date_utc" and len(us) == 31
    # eksportet shkruajnë audit me owner
    exp = env.db.scalars(select(AuditLog).where(AuditLog.action == "report.export")).all()
    assert exp and {a.owner_ref for a in exp} == {"c1"}


def test_requires_auth_and_permission(env):
    for path in (
        "overview",
        "campaigns",
        "failures",
        "audit",
        "audit/facets",
        "campaigns.csv",
        "audit.csv",
        "usage.csv",
    ):
        assert env.c.get(f"/v1/reports/{path}").status_code == 401
    approver = _key(env.c, "approver", None)
    assert env.c.get("/v1/reports/overview", headers=approver).status_code == 403
    support = _key(env.c, "support", None)
    assert (
        env.c.get("/v1/reports/audit", headers=support).status_code == 422
    )  # stafi duhet të emërtojë tenant-in
    assert (
        env.c.get("/v1/reports/audit", params={"owner_ref": "c1"}, headers=support).status_code
        == 200
    )


# ---------------- vetëm lexim / hot-path / migrim ----------------


def test_report_routes_are_read_only_and_hot_paths_do_not_import_reporting(env):
    new = {"/v1/reports/overview", "/v1/reports/campaigns", "/v1/reports/failures", "/v1/reports/audit",
           "/v1/reports/audit/facets", "/v1/reports/campaigns.csv", "/v1/reports/audit.csv",
           "/v1/reports/usage.csv", "/v1/reports/campaigns/{campaign_id}/recipients.csv"}  # fmt: skip
    spec = env.c.app.openapi()["paths"]
    for path in new:
        assert set(spec[path]) == {"get"}, path
    root = Path(__file__).resolve().parent.parent / "app"
    hot = ["services/messages.py", "services/dispatch_outcome.py", "services/wallet.py", "services/sender_authorization.py",
           "services/sender_sync.py", "services/sender_request_outbox.py", "services/money_sync.py", "worker.py"]  # fmt: skip
    for f in hot:
        p = root / f
        if p.exists():
            assert not re.search(r"report_views|csv_export", p.read_text()), f


def test_enterprise_0034_is_additive_reversible_and_keeps_legacy_null(make_db):
    url = make_db("ent")
    enterprise_alembic(url, "upgrade", "0033")
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("INSERT INTO sms_audit_log (actor, role, action, target_type, target_id, created_at) "
                       "VALUES ('a','client','x.y','t','1', :n)"), {"n": datetime(2026, 1, 1, tzinfo=UTC)})  # fmt: skip
    assert "owner_ref" not in {c["name"] for c in inspect(eng).get_columns("sms_audit_log")}
    enterprise_alembic(url, "upgrade", "0034")
    col = next(c for c in inspect(eng).get_columns("sms_audit_log") if c["name"] == "owner_ref")
    assert col["nullable"] is True
    assert "ix_sms_audit_owner" in {i["name"] for i in inspect(eng).get_indexes("sms_audit_log")}
    with eng.connect() as c:
        assert (
            c.execute(text("SELECT owner_ref FROM sms_audit_log")).scalar() is None
        )  # pa backfill
    enterprise_alembic(url, "downgrade", "0033")
    assert "owner_ref" not in {c["name"] for c in inspect(eng).get_columns("sms_audit_log")}
    enterprise_alembic(url, "upgrade", "head")
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    import app.models  # noqa: F401
    from app.core.db import Base

    with eng.connect() as c:
        ctx = MigrationContext.configure(c, opts={"compare_type": True})
        diff = [repr(i) for d in compare_metadata(ctx, Base.metadata) for i in (d if isinstance(d, list) else [d])
                if "sms_audit_log" in repr(i)]  # fmt: skip
        assert diff == []
    eng.dispose()
