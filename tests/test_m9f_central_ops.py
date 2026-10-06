# ruff: noqa: F811
"""M9-f — Central: pamja financiare, alarme, reversal-et e pazgjidhura, readiness i agreguar (vetëm lexim)."""

import json
import subprocess
import uuid
from datetime import timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from apps.central.models import AuditLog, CentralUser, UsageReport
from apps.central.services import financial_ops as fo
from apps.central.services import money_reconciliation as mr
from apps.central.services import payments, pricing, retention
from apps.central.tools import financial_readiness as tool
from apps.central.tools import retention as retention_cli
from tests.test_central import IS_PG, make_db  # noqa: F401
from tests.test_central_auth import auth_secret  # noqa: F401
from tests.test_m9d_central_reports import env  # noqa: F401
from tests.test_m9d_reconciliation import (
    MIN,
    NOW,
    T,
    admins,
    grow,
    ingest,
    issue,
    new_account,
    ready,
    report,
    reverse,
    seq_of,
)


def price_it(env, *, currency="EUR", which="e1"):
    """Çmim efektiv sot për (enterprise, sms): libër → version → rregull → caktim (import ⇒ efektiv në të kaluarën)."""
    a, _ = admins(env)
    with Session(env.eng, expire_on_commit=False) as s:
        u = s.get(CentralUser, a)
        b = pricing.create_book(s, u, f"book_{currency.lower()}_{which}", "Retail", currency)
        v = pricing.new_draft(s, u, b.id)
        pricing.set_rule(s, u, v.id, "sms", "0.05", prefix="355")
        pricing.activate(s, u, v.id, NOW - timedelta(days=30), imported=True)
        pricing.assign(
            s, u, env.ids[which], env.ids["sms"], b.id, NOW - timedelta(days=30), imported=True
        )
        s.commit()


def snap(env, *, now=NOW, thresholds=T):
    with Session(env.eng) as s:
        res = mr.reconcile(s, now=now, thresholds=thresholds)
        out = fo.snapshot(s, now, res)
        s.rollback()
        return out


def checks(env, *, now=NOW, thresholds=T):
    with Session(env.eng) as s:
        res = mr.reconcile(s, now=now, thresholds=thresholds)
        out = {c.name: c for c in fo.readiness_checks(s, now, res)}
        s.rollback()
        return out


def alert_codes(s, level=None):
    return sorted(a["code"] for a in s["alerts"] if level is None or a["level"] == level)


# --- pamja ---------------------------------------------------------------------------------------------------------------


def test_snapshot_summarises_payments_accounts_grants_reports_and_pricing(env):
    acct, gids, rows = ready(env)
    a, _ = admins(env)
    with Session(env.eng, expire_on_commit=False) as s:
        payments.create(s, acct, "7", actor=s.get(CentralUser, a), now=NOW)
        s.commit()
    ingest(env, report(env, rows))
    price_it(env)
    sn = snap(env)
    assert (
        sn["payments"]["pending"] == 1
        and sn["payments"]["approved"] == 1
        and sn["payments"]["rejected"] == 0
    )
    (ac,) = sn["accounts"]
    assert (ac["available_to_grant"], ac["outstanding_grants"], ac["funds"]) == (
        "9920.000000",
        "80.000000",
        "10000.000000",
    )
    assert (
        sn["grants"]["active"] == 2
        and sn["grants"]["reversed"] == 0
        and D(sn["grants"]["active_amount"]) == D("80")
    )
    (rep,) = sn["usage_reports"]
    assert (
        rep["freshness"] == "PASS"
        and rep["authority_mode"] == "central"
        and rep["age_seconds"] == 60
    )
    assert (
        sn["pricing"]["active_versions"] == 1
        and sn["pricing"]["accounts_without_price_assignment"] == []
    )
    assert sn["reconciliation"]["status"] == "PASS" and sn["alerts"] == []
    assert "password" not in json.dumps(sn).lower()


def test_pricing_gap_and_stale_pending_payment_are_warn_alerts(env):
    acct, gids, rows = ready(env)
    ingest(env, report(env, rows))
    a, _ = admins(env)
    with Session(env.eng, expire_on_commit=False) as s:
        payments.create(s, acct, "7", actor=s.get(CentralUser, a), now=NOW - timedelta(days=5))
        s.commit()
    sn = snap(env)
    assert (
        alert_codes(sn, "WARN") == ["price_assignment_missing", "stale_pending_payments"]
        and alert_codes(sn, "CRITICAL") == []
    )
    assert (
        sn["payments"]["stale_pending"] == 1
        and sn["payments"]["oldest_pending_age_seconds"] >= 5 * 86400 - 5
    )


# --- alarmet CRITICAL / WARN nga rakordimi ------------------------------------------------------------------------------------


def test_unexplained_positive_credit_negative_hold_and_formula_are_critical_alerts(env):
    acct, gids, rows = ready(env)
    ingest(env, report(env, rows, gross="85.000000", flows={"positive_local_credit": "5.000000"}))
    assert "unexplained_positive_credit" in alert_codes(snap(env), "CRITICAL")
    ingest(
        env,
        report(
            env,
            rows,
            seq=2,
            ledger_max_id=11,
            gross="-1.000000",
            flows={"grants_applied": "0.000000"},
        ),
    )
    assert "negative_invariant" in alert_codes(snap(env), "CRITICAL")
    d = report(
        env,
        rows,
        seq=3,
        ledger_max_id=12,
        gross="80.000000",
        held="5.000000",
        hold_total="4.000000",
    )
    d["flows"]["grants_applied"] = "80.000000"
    ingest(env, d)
    assert "wallet_hold_mismatch" in alert_codes(snap(env), "CRITICAL")


def test_money_feed_broken_is_critical_only_in_central_mode(env):
    acct, gids, rows = ready(env)
    ingest(
        env, report(env, rows[:1])
    )  # kursori e ka kaluar grant-in e dytë por mungon ⇒ missing_grant FAIL
    assert "money_feed_broken" in alert_codes(snap(env), "CRITICAL")


def test_stale_report_cursor_lag_and_drift_are_warn_not_critical(env):
    acct, gids, rows = ready(env)
    ingest(
        env, report(env, rows), at=NOW - 20 * MIN
    )  # marrë para 20 min ⇒ mes fresh (10) dhe stale (30)
    sn = snap(env)
    assert "stale_usage_report" in alert_codes(sn, "WARN") and alert_codes(sn, "CRITICAL") == []
    assert sn["usage_reports"][0]["freshness"] == "WARN"
    late = issue(env, acct, "5", at=NOW - 5 * MIN)
    ingest(
        env, report(env, rows, seq=2, ledger_max_id=11, cursor=seq_of(env, late) - 1), at=NOW - MIN
    )
    assert "cursor_lag" in alert_codes(snap(env), "WARN")


def test_unresolved_reversal_view_is_complete_and_the_original_discrepancy_stays_visible(env):
    acct, gids, rows = ready(env)
    reverse(env, gids[0])
    stuck = grow(env, gids[0], "reconciliation_required", detail="insufficient available funds for reversal: available=1 amount=50",
                 updated_at="2030-01-01T11:50:00.000000+00:00")  # fmt: skip
    d = report(
        env,
        [stuck, rows[1]],
        gross="80.000000",
        held="29.000000",
        flows={"grants_applied": "80.000000", "grant_reversals": "0.000000"},
    )
    d["wallet"]["active_hold_total"] = "29.000000"
    ingest(env, d)
    sn = snap(env)
    (u,) = sn["unresolved_reversals"]
    assert u["grant_id"] == str(gids[0]) and u["amount"] == "50.000000" and u["currency"] == "EUR"
    assert (u["available"], u["held"], u["age_seconds"], u["severity"]) == (
        "51.000000",
        "29.000000",
        600,
        "WARN",
    )
    assert u["reason"] == "customer refund"
    assert u["central_state"]["status"] == "reversed" and u["central_state"]["reversed_at"]
    assert (
        u["enterprise_state"]["status"] == "reconciliation_required"
        and "insufficient" in u["enterprise_state"]["detail"]
    )
    assert alert_codes(sn, "CRITICAL") == [] and "reconciliation_drift" in alert_codes(sn, "WARN")
    # pas pragut ⇒ FAIL ⇒ alarm CRITICAL; ende i dukshëm (asnjë "zgjidh" nuk ekziston)
    late = snap(env, now=NOW + timedelta(hours=2))
    assert (
        "unresolved_reversal" in alert_codes(late, "CRITICAL")
        and late["unresolved_reversals"][0]["severity"] == "FAIL"
    )
    # vetëm një veprim REAL e heq: Enterprise e aplikon reversal-in dhe e raporton `reversed`
    ingest(env, report(env, [grow(env, gids[0], "reversed"), rows[1]], seq=2, ledger_max_id=11))
    assert snap(env)["unresolved_reversals"] == []


# --- readiness ----------------------------------------------------------------------------------------------------------------


def test_central_readiness_passes_on_a_clean_priced_funded_fresh_system(env):
    acct, gids, rows = ready(env)
    ingest(env, report(env, rows))
    price_it(env)
    c = checks(env)
    assert {n: x.level for n, x in c.items() if x.level != "PASS"} == {}, {
        n: x.reason for n, x in c.items() if x.level != "PASS"
    }
    assert {"reconciliation_no_critical", "no_unexplained_positive_mint", "no_unresolved_unsafe_reversal", "usage_reports_fresh",
            "pricing_assignment_present", "pricing_currency_matches_account", "baseline_cutover_status", "financial_service_credentials"} <= set(c)  # fmt: skip


@pytest.mark.parametrize(
    "case", ["no_reports", "critical", "mint", "stale_report", "no_price", "no_credentials"]
)
def test_central_readiness_fails_closed(env, case):
    acct, gids, rows = ready(env)
    price_it(env)
    if case == "no_price":
        pass
    if case != "no_reports":
        d = report(env, rows)
        if case == "critical":
            d = report(env, rows, gross="-1.000000", flows={"grants_applied": "0.000000"})
        if case == "mint":
            d = report(env, rows, gross="85.000000", flows={"positive_local_credit": "5.000000"})
        ingest(env, d, at=NOW - (45 * MIN if case == "stale_report" else MIN))
    if case == "no_credentials":
        from apps.central.models.service_auth import ServiceClient

        with Session(env.eng) as s:
            for c in s.scalars(select(ServiceClient)):
                c.status = "disabled"
            s.commit()
    if case == "no_price":
        with Session(env.eng) as s:  # llogari e dytë (USD) pa caktim
            pass
        new_account(env, "e2")  # enterprise 2 pa çmim
    c = checks(env)
    failing = {n for n, x in c.items() if x.level == "FAIL"}
    expected = {"no_reports": {"usage_reports_fresh"}, "critical": {"reconciliation_no_critical"}, "mint": {"no_unexplained_positive_mint"},
                "stale_report": {"usage_reports_fresh"}, "no_price": {"pricing_assignment_present"}, "no_credentials": {"financial_service_credentials"}}[case]  # fmt: skip
    assert expected <= failing, (case, {n: (x.level, x.reason) for n, x in c.items()})


def test_unsafe_unresolved_reversal_blocks_readiness_while_a_young_one_only_warns(env):
    acct, gids, rows = ready(env)
    price_it(env)
    reverse(env, gids[0])
    stuck = grow(
        env,
        gids[0],
        "reconciliation_required",
        detail="x",
        updated_at="2030-01-01T11:50:00.000000+00:00",
    )
    d = report(
        env,
        [stuck, rows[1]],
        gross="80.000000",
        held="29.000000",
        flows={"grants_applied": "80.000000", "grant_reversals": "0.000000"},
    )
    d["wallet"]["active_hold_total"] = "29.000000"
    ingest(env, d)
    assert checks(env)["no_unresolved_unsafe_reversal"].level == "WARN"
    assert (
        checks(env, now=NOW + timedelta(hours=2))["no_unresolved_unsafe_reversal"].level == "FAIL"
    )


# --- mjeti i agreguar -------------------------------------------------------------------------------------------------------------


def ent_doc(name, level="PASS"):
    return json.dumps([{"name": name, "level": level, "reason": "ok"}])


def fake_runner(overrides=None, ops_alerts=()):
    overrides = overrides or {}

    def run(module, args):
        if module in overrides:
            r = overrides[module]
            if isinstance(r, Exception):
                raise r
            return r
        if module.endswith("financial_ops"):
            return 0, json.dumps({"snapshot": {}, "alerts": list(ops_alerts)})
        return 0, ent_doc(module.rsplit(".", 1)[-1])

    return run


def run_tool(env, runner, argv=(), capsys=None):
    code = tool.main(["--json", *argv], engine=env.eng, run=runner)
    out = json.loads(capsys.readouterr().out) if capsys else None
    return code, out


def test_tool_aggregates_central_and_enterprise_checks_and_is_read_only(env, capsys, monkeypatch):
    acct, gids, rows = ready(env)
    price_it(env)
    monkeypatch.setattr(tool, "utcnow", lambda: NOW)
    monkeypatch.setattr(fo, "utcnow", lambda: NOW)
    ingest(env, report(env, rows))
    writes = []

    def spy(conn, cursor, statement, *a):
        if statement.lstrip().split(None, 1)[0].upper() in ("INSERT", "UPDATE", "DELETE"):
            writes.append(statement[:60])

    event.listen(env.eng, "before_cursor_execute", spy)
    try:
        monkeypatch.setattr(mr, "utcnow", lambda: NOW)
        code, out = run_tool(env, fake_runner(), capsys=capsys)
    finally:
        event.remove(env.eng, "before_cursor_execute", spy)
    assert writes == []
    sources = {c["source"] for c in out["checks"]}
    assert sources == {"central", "queue", "money", "pricing", "ops"}
    assert out["status"] in ("PASS", "WARN", "FAIL") and "strict" in out
    assert code in (0, 1)


def test_tool_status_and_exit_codes_for_pass_warn_fail_and_strict(env, capsys, monkeypatch):
    acct, gids, rows = ready(env)
    price_it(env)
    monkeypatch.setattr(mr, "utcnow", lambda: NOW)
    monkeypatch.setattr(fo, "utcnow", lambda: NOW)
    monkeypatch.setattr(tool, "utcnow", lambda: NOW)
    ingest(env, report(env, rows))
    code, out = run_tool(env, fake_runner(), capsys=capsys)
    assert (out["status"], code) == ("PASS", 0)
    warn = fake_runner(
        ops_alerts=[
            {"level": "WARN", "code": "unknown_backlog", "subject": "queue", "message": "old"}
        ]
    )
    code, out = run_tool(env, warn, capsys=capsys)
    assert (out["status"], code) == ("WARN", 0)
    code, out = run_tool(env, warn, ["--strict"], capsys=capsys)
    assert (out["status"], code) == ("WARN", 1)
    crit = fake_runner(
        ops_alerts=[
            {
                "level": "CRITICAL",
                "code": "money_feed_broken",
                "subject": "cursor",
                "message": "bad",
            }
        ]
    )
    code, out = run_tool(env, crit, capsys=capsys)
    assert (out["status"], code) == ("FAIL", 1)
    bad_enterprise = fake_runner(
        {
            "scripts.money_authority_readiness": (
                0,
                json.dumps(
                    [
                        {
                            "name": "authority_mode",
                            "level": "FAIL",
                            "reason": "SMS_MONEY_AUTHORITY=local",
                        }
                    ]
                ),
            )
        }
    )
    code, out = run_tool(env, bad_enterprise, capsys=capsys)
    assert out["status"] == "FAIL" and any(
        c["source"] == "money" and c["level"] == "FAIL" for c in out["checks"]
    )


def test_tool_fails_closed_when_an_enterprise_check_cannot_run(env, capsys, monkeypatch):
    acct, gids, rows = ready(env)
    price_it(env)
    monkeypatch.setattr(mr, "utcnow", lambda: NOW)
    monkeypatch.setattr(fo, "utcnow", lambda: NOW)
    monkeypatch.setattr(tool, "utcnow", lambda: NOW)
    ingest(env, report(env, rows))
    for override in (
        (1, "Traceback..."),
        (0, ""),
        (0, "{not json"),
        (0, "{}"),
        subprocess.TimeoutExpired("x", 1),
        OSError("no python"),
    ):
        code, out = run_tool(env, fake_runner({"scripts.queue_readiness": override}), capsys=capsys)
        assert out["status"] == "FAIL" and code == 1, override
        assert any(
            c["source"] == "queue" and c["name"] == "unavailable" and c["level"] == "FAIL"
            for c in out["checks"]
        )


def test_skipping_enterprise_checks_can_never_produce_pass(env, capsys, monkeypatch):
    acct, gids, rows = ready(env)
    price_it(env)
    monkeypatch.setattr(mr, "utcnow", lambda: NOW)
    monkeypatch.setattr(fo, "utcnow", lambda: NOW)
    monkeypatch.setattr(tool, "utcnow", lambda: NOW)
    ingest(env, report(env, rows))
    code = tool.main(["--json", "--no-enterprise-checks"], engine=env.eng)
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "WARN" and code == 0
    assert any(c["name"] == "skipped" and "NOT VERIFIED" in c["reason"] for c in out["checks"])
    assert tool.main(["--json", "--no-enterprise-checks", "--strict"], engine=env.eng) == 1
    capsys.readouterr()


def test_tool_text_output_and_internal_error_code(env, capsys):
    code = tool.main(["--no-enterprise-checks"], engine=env.eng)
    out = capsys.readouterr().out
    assert "FINANCIAL READINESS:" in out and code in (0, 1)
    assert tool.main([], engine=object(), run=fake_runner()) == 2  # gabim i brendshëm ≠ PASS/FAIL
    capsys.readouterr()


def test_tool_never_imports_enterprise_code_and_runs_the_four_enterprise_scripts_as_processes():
    import inspect

    src = inspect.getsource(tool)
    import re

    assert not re.search(r"^\s*(from app\b|import app\b)", src, re.M)
    assert [m for _, m, _ in tool.ENTERPRISE_RUNS] == ["scripts.queue_readiness", "scripts.money_authority_readiness",
                                                       "scripts.pricing_authority_readiness", "scripts.financial_ops"]  # fmt: skip
    assert all(a == ["--json"] for _, _, a in tool.ENTERPRISE_RUNS)


# --- retention i usage_reports -------------------------------------------------------------------------------------------------


def seed_reports(env, n=12, step_hours=6):
    """n raporte (seq 1..n), të marrë `step_hours` larg njëri-tjetrit, i fundit në NOW − 1min."""
    acct, gids, rows = ready(env)
    for i in range(n):
        at = NOW - MIN - timedelta(hours=step_hours * (n - 1 - i))
        ingest(env, report(env, rows, seq=i + 1, ledger_max_id=10 + i), at=at)
    return rows


def count_reports(env):
    with Session(env.eng) as s:
        return s.scalar(select(func.count()).select_from(UsageReport))


def test_retention_is_off_by_default_and_dry_run_never_deletes(env, monkeypatch):
    seed_reports(env)
    with Session(env.eng) as s:
        p = retention.plan(s, now=NOW)
    assert p.delete_ids == [] and p.kept == 12 and p.params["retention_days"] == 0
    with Session(env.eng) as s:
        p = retention.plan(s, now=NOW, retention_days=2, full_days=1, keep_last=1)
    assert p.delete_ids
    assert (
        retention_cli.main([], engine=env.eng) == 0 and count_reports(env) == 12
    )  # …dry-run s'fshin


def test_retention_keeps_current_keep_last_recent_and_one_per_day_in_the_middle_window(env):
    seed_reports(env, n=12, step_hours=6)  # 3 ditë, 4 raporte/ditë
    with Session(env.eng) as s:
        p = retention.plan(s, now=NOW, retention_days=2, full_days=1, keep_last=2)
        rows = {r.report_id: r for r in s.scalars(select(UsageReport))}
    kept = {r.report_seq for rid, r in rows.items() if rid not in set(p.delete_ids)}
    deleted = {r.report_seq for rid, r in rows.items() if rid in set(p.delete_ids)}
    assert 12 in kept and 11 in kept  # aktuali + keep_last
    assert {9, 10} <= kept  # brenda `full_days`=1 (≤ 24h): të gjitha
    assert deleted and deleted.isdisjoint({9, 10, 11, 12})
    assert max(deleted) < 9
    # mes 1 dhe 2 ditëve: një per ditë UTC; më e vjetër se 2 ditë: fshihet
    old = [rows[i] for i in rows if rows[i].report_seq in deleted]
    assert all((NOW - r.received_at.replace(tzinfo=NOW.tzinfo)) > timedelta(hours=0) for r in old)


def test_retention_apply_deletes_exactly_the_plan_audits_once_and_keeps_the_latest_per_key(env):
    seed_reports(env, n=12)
    with Session(env.eng) as s:
        p = retention.plan(s, now=NOW, retention_days=1, full_days=1, keep_last=1)
        n = retention.apply(s, p, now=NOW)
        s.commit()
    assert n == len(p.delete_ids) > 0 and count_reports(env) == 12 - n
    with Session(env.eng) as s:
        assert s.scalar(select(func.max(UsageReport.report_seq))) == 12  # aktuali ruhet
        (a,) = list(s.scalars(select(AuditLog).where(AuditLog.action == "usage_report.retention")))
        assert (
            a.actor_kind == "system"
            and a.actor_label == "system:retention"
            and a.detail["deleted"] == n
        )
        p2 = retention.plan(s, now=NOW, retention_days=1, full_days=1, keep_last=1)
        assert retention.apply(s, p2, now=NOW) == 0  # idempotent; no-op ⇒ pa audit të dytë
        s.commit()
        assert (
            s.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "usage_report.retention")
            )
            == 1
        )


def test_retention_keeps_reconciliation_working_and_never_touches_money_tables(env):
    seed_reports(env, n=12)
    from apps.central.models import CommercialLedgerEntry, CreditGrant, MoneyEvent, Payment

    def totals():
        with Session(env.eng) as s:
            return tuple(
                s.scalar(select(func.count()).select_from(m))
                for m in (CommercialLedgerEntry, CreditGrant, MoneyEvent, Payment, AuditLog)
            )

    before = totals()
    with Session(env.eng) as s:
        res0 = mr.reconcile(s, now=NOW, thresholds=T)
        retention.apply(
            s, retention.plan(s, now=NOW, retention_days=1, full_days=1, keep_last=1), now=NOW
        )
        s.commit()
        res1 = mr.reconcile(s, now=NOW, thresholds=T)
    assert res0.status == res1.status and res0.counts() == res1.counts()
    after = totals()
    assert after[:4] == before[:4] and after[4] == before[4] + 1  # vetëm një rresht audit shtohet


def test_retention_new_report_after_cleanup_still_passes_the_watermark_check(env):
    seed_reports(env, n=12)
    with Session(env.eng) as s:
        retention.apply(
            s, retention.plan(s, now=NOW, retention_days=1, full_days=1, keep_last=1), now=NOW
        )
        s.commit()
    acct_rows = None
    with Session(env.eng) as s:
        acct_rows = s.scalar(select(func.max(UsageReport.ledger_max_id)))
    from apps.central.services import usage_reports

    with Session(env.eng) as s:
        latest = usage_reports.latest_per_key(s)[0]
        d = report(env, latest.payload["grants"], seq=13, ledger_max_id=acct_rows + 1)
        usage_reports.ingest(s, usage_reports.parse(d), now=NOW)
        s.commit()
    assert count_reports(env) >= 2


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_usage_reports_remain_append_only_outside_the_retention_path(env):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    if env.eng.dialect.name != "postgresql":
        pytest.skip("needs PostgreSQL triggers")
    seed_reports(env, n=3)
    with Session(env.eng) as s:
        rid = s.scalar(select(UsageReport.report_id).order_by(UsageReport.report_seq).limit(1))
        for sql in (
            "DELETE FROM usage_reports WHERE report_id = :i",
            "UPDATE usage_reports SET currency = 'USD' WHERE report_id = :i",
        ):
            with pytest.raises(DBAPIError):
                s.execute(text(sql), {"i": rid})
            s.rollback()
        s.execute(text("SELECT set_config('central.retention_delete', 'on', true)"))
        with pytest.raises(DBAPIError):  # UPDATE nuk lejohet kurrë, as me GUC
            s.execute(
                text("UPDATE usage_reports SET currency = 'USD' WHERE report_id = :i"), {"i": rid}
            )
        s.rollback()
        with pytest.raises(DBAPIError):
            s.execute(text("TRUNCATE usage_reports"))
        s.rollback()
        s.execute(text("SELECT set_config('central.retention_delete', 'on', true)"))
        assert (
            s.execute(text("DELETE FROM usage_reports WHERE report_id = :i"), {"i": rid}).rowcount
            == 1
        )
        s.commit()
    assert uuid is not None and payments is not None
