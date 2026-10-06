# ruff: noqa: F811
"""M9-f — Enterprise: pamja operacionale financiare (stats + alarme), UNKNOWN/hold/reversal, retention i kufizuar."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.models.admin import AuditLog
from app.models.money_authority import G_RECON, MoneyCursor, MoneyGrant
from app.models.money_usage import UsageReport
from app.models.pricing import PricingComparison, PricingState
from app.models.sending import Message
from app.models.wallet import Hold, HoldStatus
from app.services import financial_ops as fo
from scripts import financial_ops as fo_cli
from scripts import financial_retention as fr
from tests.test_m9a_unknown_outcome import Stub, stub, to_unknown  # noqa: F401
from tests.test_pipeline import OK, fake, send, world  # noqa: F401

PG = engine.dialect.name == "postgresql"
NOW = datetime.now(UTC)


def codes(alerts, level=None):
    return sorted(a["code"] for a in alerts if level is None or a["level"] == level)


# --- UNKNOWN + wallet/hold -------------------------------------------------------------------------------------------


def test_unknown_backlog_counts_held_amount_and_ages_into_a_warn_alert(
    db, world, stub, monkeypatch
):
    m = to_unknown(db, stub)
    snap = fo.snapshot(db, NOW + timedelta(hours=3))
    u = snap["unknown"]
    assert u["sms"] == 1 and u["email"] == 0 and u["held_amount_by_currency"] == {"EUR": "0.050000"}
    assert u["oldest_age_seconds"] >= 3 * 3600 - 5
    assert (
        snap["wallets"]["by_currency"]["EUR"]["held"] == "0.050000"
    )  # UNKNOWN mban hold-in ⇒ para e ngrirë dukshme
    assert snap["wallets"]["hold_mismatch_count"] == 0
    al = fo.alerts(snap)
    assert [(a["level"], a["code"]) for a in al] == [("WARN", "unknown_backlog")]
    assert "0.050000" in al[0]["message"]
    assert fo.alerts(fo.snapshot(db, NOW)) == []  # i ri ⇒ pa alarm
    monkeypatch.setattr(settings, "financial_unknown_warn_seconds", 60)
    assert codes(fo.alerts(fo.snapshot(db, NOW + timedelta(minutes=5)))) == ["unknown_backlog"]
    assert m.public_id not in json.dumps(snap)  # asnjë id mesazhi/PII në pamje agregate


def test_snapshot_contains_no_phone_numbers_or_message_ids(db, world, stub):
    to_unknown(db, stub)
    blob = json.dumps(fo.snapshot(db))
    assert "355691230003" not in blob and "ACME" not in blob


def test_an_active_hold_without_a_matching_ledger_balance_is_a_critical_alert(db, world):
    w, _ = world
    db.add(Hold(wallet_id=w.id, amount=D("1"), status=HoldStatus.ACTIVE, reference="orphan-hold"))
    db.commit()
    snap = fo.snapshot(db)
    assert snap["wallets"]["hold_mismatch_count"] == 1
    assert codes(fo.alerts(snap), "CRITICAL") == ["wallet_hold_mismatch"]


def test_healthy_system_has_no_alerts_and_snapshot_is_read_only(db, world, stub):
    send(db)
    stmts = []

    def spy(conn, cursor, statement, *a):
        stmts.append(statement.lstrip().split(None, 1)[0].upper())

    event.listen(engine, "before_cursor_execute", spy)
    try:
        snap = fo.snapshot(db)
        alerts = fo.alerts(snap)
        db.rollback()
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    assert alerts == []
    assert not {s for s in stmts if s in ("INSERT", "UPDATE", "DELETE")}, stmts


# --- alarmet: matrica e pragjeve ----------------------------------------------------------------------------------------


def synth(**over):
    base = {
        "unknown": {"sms": 0, "email": 0, "oldest_age_seconds": None, "held_amount_by_currency": {}},
        "wallets": {"by_currency": {}, "hold_mismatch": [], "hold_mismatch_count": 0, "negative_wallets": [], "negative_wallet_count": 0},
        "money": {"authority": "central", "cursor": {"epoch": "e", "last_seq": 5, "age_seconds": 10, "has_error": False, "last_error": None},
                  "grants_by_status": {}, "unresolved_reversals": [], "baselines_by_status": {}},
        "usage_reports": {"by_status": {}, "oldest_unsent_age_seconds": None, "last_sent_age_seconds": 10},
        "pricing": {"authority": "central", "snapshot": {"snapshot_id": "s", "epoch": "e", "revision": 1, "sync_age_seconds": 10, "has_error": False, "last_error": None},
                    "comparisons": {"total": 0, "mismatches": {}}},
    }  # fmt: skip
    for k, v in over.items():
        base[k] = {**base[k], **v} if isinstance(v, dict) and k != "pricing" else v
    return base


def test_clean_synthetic_snapshot_has_no_alerts():
    assert fo.alerts(synth()) == []


def test_critical_conditions(monkeypatch):
    s = synth(wallets={"negative_wallet_count": 1, "negative_wallets": [3]})
    assert codes(fo.alerts(s), "CRITICAL") == ["negative_balance"]
    rev = {
        "grant_id": "g1",
        "amount": "50.000000",
        "currency": "EUR",
        "available": "1.000000",
        "held": "0",
        "age_seconds": settings.financial_unresolved_reversal_critical_seconds + 1,
    }
    assert codes(fo.alerts(synth(money={"unresolved_reversals": [rev]})), "CRITICAL") == [
        "unresolved_reversal"
    ]
    young = {**rev, "age_seconds": 10}
    assert (
        fo.alerts(synth(money={"unresolved_reversals": [young]})) == []
    )  # brenda pragut: vetëm i dukshëm
    for cur in ({"epoch": None, "last_seq": 0, "age_seconds": None, "has_error": False, "last_error": None},
                {"epoch": "e", "last_seq": 1, "age_seconds": 5, "has_error": True, "last_error": "conflict"},
                {"epoch": "e", "last_seq": 1, "age_seconds": settings.financial_money_cursor_critical_seconds + 1, "has_error": False, "last_error": None}):  # fmt: skip
        assert codes(fo.alerts(synth(money={"cursor": cur})), "CRITICAL") == [
            "money_feed_broken"
        ], cur
    assert (
        fo.alerts(
            synth(
                money={
                    "authority": "shadow",
                    "cursor": {
                        "epoch": None,
                        "last_seq": 0,
                        "age_seconds": None,
                        "has_error": True,
                        "last_error": "x",
                    },
                }
            )
        )
        == []
    )  # jo central ⇒ jo kritike
    p = synth()
    p["pricing"] = {**p["pricing"], "snapshot": None}
    assert codes(fo.alerts(p), "CRITICAL") == ["pricing_missing"]
    p["pricing"]["snapshot"] = {
        "snapshot_id": "s",
        "epoch": "e",
        "revision": 1,
        "sync_age_seconds": settings.pricing_stale_fail_seconds + 1,
        "has_error": False,
        "last_error": None,
    }
    assert "pricing_missing" in codes(fo.alerts(p), "CRITICAL")
    p["pricing"]["snapshot"] = {
        **p["pricing"]["snapshot"],
        "sync_age_seconds": 1,
        "has_error": True,
    }
    assert codes(fo.alerts(p), "CRITICAL") == ["pricing_missing"]
    p["pricing"]["authority"] = "local"
    assert fo.alerts(p) == []  # pricing local ⇒ s'është kritik


def test_warn_conditions(monkeypatch):
    mid = (
        settings.financial_money_cursor_warn_seconds
        + settings.financial_money_cursor_critical_seconds
    ) // 2
    cur = {"epoch": "e", "last_seq": 1, "age_seconds": mid, "has_error": False, "last_error": None}
    assert [(a["level"], a["code"]) for a in fo.alerts(synth(money={"cursor": cur}))] == [
        ("WARN", "money_cursor_lag")
    ]
    p = synth()
    p["pricing"] = {
        **p["pricing"],
        "authority": "shadow",
        "comparisons": {"total": 30, "mismatches": {"sms:unit_price_mismatch": 2}},
    }
    assert [(a["level"], a["code"]) for a in fo.alerts(p)] == [("WARN", "shadow_pricing_mismatch")]
    p["pricing"] = {
        **synth()["pricing"],
        "snapshot": {
            **synth()["pricing"]["snapshot"],
            "sync_age_seconds": settings.pricing_stale_warn_seconds + 1,
        },
    }
    assert [(a["level"], a["code"]) for a in fo.alerts(p)] == [("WARN", "pricing_snapshot_stale")]
    monkeypatch.setattr(settings, "money_reporting", True)
    u = {
        "by_status": {"pending": 1},
        "oldest_unsent_age_seconds": settings.money_report_stale_seconds + 1,
        "last_sent_age_seconds": None,
    }
    assert [(a["level"], a["code"]) for a in fo.alerts(synth(usage_reports=u))] == [
        ("WARN", "usage_reports_stale")
    ]
    monkeypatch.setattr(settings, "money_reporting", False)
    assert fo.alerts(synth(usage_reports=u)) == []  # raportimi i fikur ⇒ jo alarm
    k = {
        "sms": 2,
        "email": 0,
        "oldest_age_seconds": settings.financial_unknown_warn_seconds + 1,
        "held_amount_by_currency": {"EUR": "1.000000"},
    }
    assert [(a["level"], a["code"]) for a in fo.alerts(synth(unknown=k))] == [
        ("WARN", "unknown_backlog")
    ]


def test_alert_ordering_is_critical_first_and_deterministic():
    s = synth(wallets={"negative_wallet_count": 1, "negative_wallets": [1]},
              unknown={"sms": 1, "email": 0, "oldest_age_seconds": 10**6, "held_amount_by_currency": {"EUR": "1.000000"}})  # fmt: skip
    a = fo.alerts(s)
    assert [x["level"] for x in a] == ["CRITICAL", "WARN"] and a == fo.alerts(s)


# --- reversal-et e pazgjidhura (Enterprise) ---------------------------------------------------------------------------------


def test_unresolved_reversal_is_visible_with_grant_amount_balance_age_reason_and_state(db, world):
    w, _ = world
    db.add(MoneyGrant(grant_id=uuid.uuid4(), enterprise_id=uuid.uuid4(), account_id=uuid.uuid4(), product_id=uuid.uuid4(), currency="EUR",
                      amount=D("50"), purpose="standard", wallet_id=w.id, status=G_RECON, detail="insufficient available funds for reversal",
                      issued_seq=1, issued_event_id=uuid.uuid4(), issued_payload_hash="a" * 64, reversed_seq=2, reversed_event_id=uuid.uuid4(),
                      updated_at=NOW - timedelta(hours=2)))  # fmt: skip
    db.commit()
    snap = fo.snapshot(db, NOW)
    (r,) = snap["money"]["unresolved_reversals"]
    assert (
        r["amount"],
        r["currency"],
        r["available"],
        r["held"],
        r["enterprise_state"],
        r["reversed_seq"],
    ) == ("50.000000", "EUR", "10.000000", "0.000000", "reconciliation_required", 2)
    assert r["age_seconds"] >= 7190 and "insufficient" in r["reason"] and r["grant_id"]
    assert snap["money"]["grants_by_status"] == {"reconciliation_required": 1}
    assert codes(fo.alerts(snap), "CRITICAL") == ["unresolved_reversal"]  # > prag (1h) ⇒ kritik
    # asnjë "shëno si zgjidhur": rreshti mbetet derisa të ketë veprim financiar real
    assert (
        db.scalar(select(func.count()).select_from(MoneyGrant).where(MoneyGrant.status == G_RECON))
        == 1
    )


def test_money_cursor_and_pricing_state_are_summarised_without_secrets(db, world):
    cur = db.get(MoneyCursor, 1) or MoneyCursor(id=1)
    cur.epoch, cur.last_seq, cur.last_success_at, cur.last_error = (
        uuid.uuid4(),
        9,
        NOW - timedelta(seconds=30),
        None,
    )
    db.add(cur)
    st = db.get(PricingState, 1) or PricingState(id=1)
    st.active_snapshot_id, st.epoch, st.revision, st.last_success_at = (
        uuid.uuid4(),
        uuid.uuid4(),
        4,
        NOW - timedelta(seconds=40),
    )
    db.add(st)
    db.commit()
    s = fo.snapshot(db, NOW)
    assert s["money"]["cursor"]["last_seq"] == 9 and 29 <= s["money"]["cursor"]["age_seconds"] <= 31
    assert s["pricing"]["snapshot"]["revision"] == 4 and s["pricing"]["authority"] == "local"


# --- API + CLI ------------------------------------------------------------------------------------------------------------


def test_admin_endpoint_requires_monitor_read_and_returns_snapshot_and_alerts(
    client, db, world, stub
):
    to_unknown(db, stub)
    r = client.get("/v1/admin/financial")
    assert r.status_code == 200 and set(r.json()) == {"snapshot", "alerts"}
    assert r.json()["snapshot"]["unknown"]["sms"] == 1
    from fastapi.testclient import TestClient

    from app.main import create_app

    anon = TestClient(create_app())
    assert anon.get("/v1/admin/financial").status_code in (401, 403)


def test_cli_prints_json_and_exits_1_only_on_critical(db, world, capsys, monkeypatch):
    assert fo_cli.main(["--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["alerts"] == [] and "snapshot" in out
    monkeypatch.setattr(
        fo,
        "alerts",
        lambda snap: [{"level": "CRITICAL", "code": "x", "subject": "s", "message": "m"}],
    )
    assert fo_cli.main([]) == 1
    monkeypatch.setattr(
        fo, "alerts", lambda snap: [{"level": "WARN", "code": "x", "subject": "s", "message": "m"}]
    )
    assert fo_cli.main([]) == 0


# --- retention ------------------------------------------------------------------------------------------------------------


def cmp_row(db, *, ok, age_days, kind="sms"):
    r = PricingComparison(kind=kind, ref=uuid.uuid4().hex, classification="match" if ok else "unit_price_mismatch", ok=ok,
                          created_at=NOW - timedelta(days=age_days))  # fmt: skip
    db.add(r)
    db.flush()
    return r.id


def run_retention(apply, **kw):
    with SessionLocal() as s:
        p = fr.plan(s, NOW, **kw)
        done = fr.apply(s, p, NOW) if apply else None
        s.commit()
        return p, done


def test_comparison_retention_is_dry_run_first_keeps_recent_and_old_mismatches_longer(
    db, monkeypatch
):
    monkeypatch.setattr(settings, "pricing_comparison_ok_days", 30)
    monkeypatch.setattr(settings, "pricing_comparison_mismatch_days", 200)
    old_ok, new_ok = cmp_row(db, ok=True, age_days=40), cmp_row(db, ok=True, age_days=5)
    old_bad, mid_bad, very_old_bad = (
        cmp_row(db, ok=False, age_days=100),
        cmp_row(db, ok=False, age_days=150),
        cmp_row(db, ok=False, age_days=250),
    )
    db.commit()
    p, done = run_retention(False)
    assert (
        done is None
        and p["comparisons"]["ok"] == [old_ok]
        and p["comparisons"]["mismatch"] == [very_old_bad]
    )
    assert (
        db.scalar(select(func.count()).select_from(PricingComparison)) == 5
    )  # dry-run: asgjë s'u fshi
    p, done = run_retention(True)
    assert done == {"comparisons": 2, "usage_outbox": 0}
    db.expire_all()
    left = set(db.scalars(select(PricingComparison.id)))
    assert left == {new_ok, old_bad, mid_bad}
    (a,) = list(db.scalars(select(AuditLog).where(AuditLog.action == "financial.retention")))
    assert a.actor == "system:retention" and json.loads(a.detail)["comparisons"] == 2
    run_retention(True)  # no-op ⇒ pa audit të ri
    assert (
        db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "financial.retention")
        )
        == 1
    )


def test_comparison_retention_is_skipped_during_the_shadow_window_and_disabled_by_zero(
    db, monkeypatch
):
    monkeypatch.setattr(settings, "pricing_comparison_ok_days", 30)
    cmp_row(db, ok=True, age_days=400)
    db.commit()
    monkeypatch.setattr(settings, "pricing_authority", "shadow")
    p, _ = run_retention(False)
    assert p["comparisons"]["skipped"] and p["comparisons"]["ok"] == []
    p, _ = run_retention(False, include_shadow=True)
    assert len(p["comparisons"]["ok"]) == 1
    monkeypatch.setattr(settings, "pricing_authority", "local")
    monkeypatch.setattr(settings, "pricing_comparison_ok_days", 0)
    p, _ = run_retention(False)
    assert p["comparisons"]["ok"] == []


def outbox(db, seq, status, age_days, eid=None):
    rid = uuid.uuid4()
    db.add(UsageReport(report_id=rid, enterprise_id=eid or uuid.UUID(int=1), product_id=uuid.UUID(int=2), currency="EUR", report_seq=seq,
                       authority_mode="central", ledger_max_id=seq, generated_at=NOW - timedelta(days=age_days), payload={}, payload_hash="a" * 64,
                       content_hash="b" * 64, status=status, created_at=NOW - timedelta(days=age_days)))  # fmt: skip
    db.flush()
    return rid


def test_usage_outbox_retention_never_touches_unsent_or_the_latest_and_is_off_by_default(
    db, monkeypatch
):
    p, _ = run_retention(False)
    assert p["usage_outbox"] == []  # parazgjedhja: pa fshirje
    monkeypatch.setattr(settings, "usage_outbox_retention_days", 30)
    monkeypatch.setattr(settings, "usage_outbox_keep_last", 2)
    ids = {
        n: outbox(db, n, st, age)
        for n, st, age in (
            (1, "sent", 100),
            (2, "superseded", 90),
            (3, "pending", 80),
            (4, "failed", 70),
            (5, "sent", 60),
            (6, "sent", 50),
            (7, "sent", 1),
        )
    }
    db.commit()
    p, _ = run_retention(False)
    # rendi seq zbritës: 7,6 mbahen (keep_last=2); 5 (sent, e vjetër) fshihet; 4 failed + 3 pending mbahen; 2,1 fshihen
    assert set(p["usage_outbox"]) == {ids[5], ids[2], ids[1]}
    p, done = run_retention(True)
    assert done["usage_outbox"] == 3
    db.expire_all()
    assert set(db.scalars(select(UsageReport.report_id))) == {ids[3], ids[4], ids[6], ids[7]}


@pytest.mark.skipif(not PG, reason="needs PostgreSQL")
def test_pg_deletes_are_blocked_outside_retention_and_updates_never_allowed(db):
    cid = cmp_row(db, ok=True, age_days=1)
    rid = outbox(db, 1, "sent", 1)
    db.commit()
    for sql, params in (("DELETE FROM sms_pricing_comparisons WHERE id = :i", {"i": cid}), ("UPDATE sms_pricing_comparisons SET ok = false WHERE id = :i", {"i": cid}),
                        ("DELETE FROM sms_usage_reports WHERE report_id = :i", {"i": rid})):  # fmt: skip
        with pytest.raises(DBAPIError):
            db.execute(text(sql), params)
        db.rollback()
    db.execute(text("SELECT set_config('sms.retention_delete', 'on', true)"))
    with pytest.raises(DBAPIError):  # edhe me GUC, UPDATE mbetet i ndaluar
        db.execute(text("UPDATE sms_pricing_comparisons SET ok = false WHERE id = :i"), {"i": cid})
    db.rollback()
    db.execute(text("SELECT set_config('sms.retention_delete', 'on', true)"))
    assert (
        db.execute(text("DELETE FROM sms_pricing_comparisons WHERE id = :i"), {"i": cid}).rowcount
        == 1
    )
    assert (
        db.execute(text("DELETE FROM sms_usage_reports WHERE report_id = :i"), {"i": rid}).rowcount
        == 1
    )
    db.commit()
    cid2 = cmp_row(db, ok=True, age_days=1)
    db.commit()
    with pytest.raises(DBAPIError):  # GUC ishte vetëm brenda transaksionit të mëparshëm
        db.execute(text("DELETE FROM sms_pricing_comparisons WHERE id = :i"), {"i": cid2})
    db.rollback()


def test_retention_only_deletes_operational_tables_never_money_or_price_history():
    import inspect

    src = inspect.getsource(fr)
    for forbidden in (
        "LedgerEntry",
        "Hold",
        "MoneyGrant",
        "MoneyBaseline",
        "Message",
        "Invoice",
        "PricingSnapshot",
        "PricingVersion",
        "PricingRule",
        "PricingBook",
    ):
        assert f"delete({forbidden}" not in src
    assert src.count("delete(") == 2  # vetëm comparisons + outbox
    assert func is not None and Message is not None
