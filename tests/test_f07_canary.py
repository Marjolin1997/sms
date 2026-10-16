"""M11-I/F-07A: harness-i sintetik i canary-t, gate-i `provider_readiness`, plani i rikthimit. Pa rrjet, pa provider real."""

import json
import os
import secrets
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

import app.providers as providers
from app.core.config import settings
from app.core.db import Base
from app.models.sending import Message, MessageStatus, Route
from app.services import messages as svc
from app.services import switches
from app.services import wallet as wallets
from scripts import provider_canary as pc
from scripts import provider_readiness as pr

ROOT = Path(__file__).resolve().parent.parent
STRONG = secrets.token_hex(16)  # sentinelë sintetike e rastësishme
ADMIN = {"X-Admin-Key": "test-key"}


# --- harness-i ----------------------------------------------------------------------------------------


def test_canary_runs_green_in_process_and_leaves_no_residue(db, client):
    registry, secrets = dict(providers._registry), dict(settings.dlr_secrets)
    report = pc.run_canary(db, client, ADMIN)
    failed = [s for s in report.steps if not s.ok]
    assert report.ok and not failed and report.network_attempts == 0
    assert len(report.steps) >= 50
    names = " | ".join(s.name for s in report.steps)
    for needle in (
        "provider request payload", "success DLR applied", "failure DLR applied",
        "never re-sent", "duplicate DLR: money moved once", "stale contradicting DLR",
        "bad/missing signature", "unknown provider id", "admin stats expose DLR problems",
        "charged exactly once per delivered message", "http adapter payload", "twilio adapter request",
    ):  # fmt: skip
        assert needle in names, needle
    assert providers._registry == registry and settings.dlr_secrets == secrets  # asnjë mbetje


def test_canary_detects_a_broken_money_path(db, client, monkeypatch):
    monkeypatch.setattr(wallets, "capture", lambda *a, **k: None)
    report = pc.run_canary(db, client, ADMIN)
    assert not report.ok
    assert any(not s.ok and ("captured" in s.name or "money" in s.name) for s in report.steps)


def test_canary_fails_when_anything_touches_the_network(db, client, monkeypatch):
    import socket

    def leaky(self, req):
        socket.create_connection(("203.0.113.9", 9), timeout=1)

    monkeypatch.setattr(pc.CanaryProvider, "send", leaky)
    report = pc.run_canary(db, client, ADMIN)
    assert report.network_attempts >= 1 and not report.ok


@pytest.mark.parametrize(
    "url,ok",
    [
        ("sqlite:///./x.db", True),
        ("postgresql+psycopg://sms:sms@localhost:5432/sms_test", True),
        ("postgresql+psycopg://sms:sms@127.0.0.1/canary_run", True),
        ("postgresql+psycopg://sms:sms@localhost:5432/sms", False),  # emër jo i hedhshëm
        ("postgresql+psycopg://sms:sms@db.prod.example:5432/sms_test", False),  # host i jashtëm
        ("postgresql+psycopg://sms:sms@10.0.0.5/sms_test", False),
        ("postgresql+psycopg://sms:sms@localhost/production", False),
    ],
)
def test_canary_refuses_databases_that_are_not_disposable(url, ok):
    if ok:
        pc.assert_disposable(url)
    else:
        with pytest.raises(pc.NotDisposable):
            pc.assert_disposable(url)


def test_canary_cli_uses_its_own_temporary_database_and_cleans_up(tmp_path):
    decoy = tmp_path / "must_not_exist.db"
    env = {**os.environ, "SMS_DATABASE_URL": f"sqlite:///{decoy}", "SMS_TEST_DATABASE_URL": ""}
    r = subprocess.run(
        [sys.executable, "-m", "scripts.provider_canary", "--json"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    out = json.loads(r.stdout)
    assert r.returncode == 0 and out["ok"] and out["cleaned_up"] and out["network_attempts"] == 0
    assert not decoy.exists()  # DB-ja e jashtme e dhënë me env as nuk u prek
    assert "Zq7" not in r.stdout and "Authorization" not in r.stdout


# --- gate-i provider_readiness -------------------------------------------------------------------------


@pytest.fixture
def cw(db, monkeypatch):
    provider = pc.CanaryProvider()
    monkeypatch.setitem(providers._registry, pc.PROVIDER, provider)
    monkeypatch.setattr(settings, "dlr_secrets", {pc.PROVIDER: STRONG})
    monkeypatch.setattr(settings, "public_base_url", "https://platform.example.test")
    pc.seed(db, "cw1")
    return provider


def put(db, suffix=3, key=None):
    m = svc.submit(
        db,
        "cw1",
        key or f"k{suffix}-{db.query(Message).count()}",
        f"+99999{db.query(Message).count():04d}{suffix:04d}",
        pc.SENDER,
        text="x",
    )
    db.commit()
    return m


def verdict(db, **kw):
    checks = pr.evaluate(db, pc.PROVIDER, **kw)
    return pr.verdict(checks), {c.name: c for c in checks}


def test_pre_cutover_gate_is_go_for_a_fully_configured_provider(db, cw):
    v, checks = verdict(db, phase="pre")
    assert v == "GO" and pr.verdict(list(checks.values()), strict=True) == "GO"
    assert {"provider_registered", "callback_auth", "route_enabled", "switch_dispatch"} <= set(
        checks
    )


@pytest.mark.parametrize(
    "mutate,failing",
    [
        (lambda db, mp: mp.delitem(providers._registry, pc.PROVIDER), "provider_registered"),
        (
            lambda db, mp: mp.setattr(settings, "dlr_secrets", {pc.PROVIDER: "short"}),
            "callback_auth",
        ),
        (lambda db, mp: mp.setattr(settings, "dlr_secrets", {}), "callback_auth"),
        (lambda db, mp: db.query(Route).update({"enabled": False}), "route_enabled"),
        (
            lambda db, mp: switches.set_switch(db, switches.DISPATCH, False, "ops", "x"),
            "switch_dispatch",
        ),
        (
            lambda db, mp: switches.set_switch(db, switches.SUBMIT, False, "ops", "x"),
            "switch_submit",
        ),
    ],
)
def test_pre_cutover_gate_is_no_go_with_the_named_reason(db, cw, monkeypatch, mutate, failing):
    mutate(db, monkeypatch)
    db.commit()
    v, checks = verdict(db, phase="pre")
    assert v == "NO-GO" and checks[failing].level == "FAIL"


def test_https_requirement_and_fake_provider_depend_on_the_environment(db, cw, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "http://platform.example.test")
    v, checks = verdict(db, phase="pre")
    assert v == "GO" and checks["callback_url_scheme"].level == "WARN"  # dev: avertim
    assert pr.verdict(list(checks.values()), strict=True) == "NO-GO"
    monkeypatch.setattr(settings, "env", "production")
    assert verdict(db, phase="pre")[1]["callback_url_scheme"].level == "FAIL"
    checks = {c.name: c for c in pr.config_checks(db, "fake", phase="pre")}
    assert checks["provider_not_fake"].level == "FAIL"


def test_a_competing_route_with_equal_or_higher_priority_is_flagged(db, cw):
    db.add(Route(prefix=pc.PREFIX, country=pc.COUNTRY, provider="other", priority=500))
    db.commit()
    v, checks = verdict(db, phase="pre")
    assert checks["route_wins_precedence"].level == "WARN" and v == "GO"
    assert pr.verdict(list(checks.values()), strict=True) == "NO-GO"


def _post(db, **kw):
    kw.setdefault("min_sample", 1)
    kw.setdefault("min_rate", 0.9)
    return verdict(db, phase="post", **kw)


def test_post_canary_go_when_every_canary_message_is_delivered(db, cw):
    for _ in range(3):
        m = put(db, 3)
        svc.process_one(db)
        db.refresh(m)
        svc.apply_dlr(db, pc.PROVIDER, m.provider_message_id, True)
        db.commit()
    v, checks = _post(db)
    assert (
        v == "GO"
        and checks["delivery_rate"].level == "PASS"
        and "100.0%" in checks["delivery_rate"].reason
    )
    assert (
        checks["unknown_outcomes"].level == "PASS"
        and checks["duplicate_provider_ids"].level == "PASS"
    )


def test_post_canary_no_go_on_unknown_low_rate_or_small_sample_in_strict_mode(db, cw):
    ok = put(db, 3)
    svc.process_one(db)
    db.refresh(ok)
    svc.apply_dlr(db, pc.PROVIDER, ok.provider_message_id, True)
    bad = put(db, 3)
    svc.process_one(db)
    db.refresh(bad)
    svc.apply_dlr(db, pc.PROVIDER, bad.provider_message_id, False, "absent")
    db.commit()
    v, checks = _post(db)
    assert (
        v == "NO-GO"
        and checks["delivery_rate"].level == "FAIL"
        and "50.0%" in checks["delivery_rate"].reason
    )
    unk = put(db, 4)  # timeout i paqartë
    svc.process_one(db)
    db.refresh(unk)
    assert unk.status == MessageStatus.UNKNOWN
    v, checks = _post(db, min_rate=0.0)
    assert v == "NO-GO" and checks["unknown_outcomes"].level == "FAIL"
    v, checks = _post(db, min_rate=0.0, min_sample=50)  # kampion i vogël ⇒ vetëm WARN
    assert checks["delivery_rate"].level == "WARN"


def test_post_canary_flags_sent_without_dlr_backlog_and_dlr_problems(db, cw):
    m = put(db, 3)
    svc.process_one(db)
    db.refresh(m)
    old = datetime.now(UTC) - timedelta(hours=3)
    db.query(Message).update({"updated_at": old})
    queued = put(db, 3)
    queued.next_attempt_at = old
    db.commit()
    v, checks = _post(db, max_sent_age=60, max_queued_age=60)
    assert checks["sent_without_dlr"].level == "WARN" and checks["queue_backlog"].level == "WARN"
    assert v == "GO" and pr.verdict(list(checks.values()), strict=True) == "NO-GO"


def test_readiness_is_read_only_and_never_leaks_secrets_or_destinations(db, cw, capsys):
    m = put(db, 3)
    svc.process_one(db)
    db.commit()

    def snapshot():
        db.rollback()
        return {
            t.name: db.scalar(select(func.count()).select_from(t))
            for t in Base.metadata.sorted_tables
        }

    before = snapshot()
    checks = pr.evaluate(db, pc.PROVIDER, phase="post", min_sample=1)
    plan = pr.rollback_plan(db, pc.PROVIDER)
    rc = pr.main(["check", "--provider", pc.PROVIDER, "--phase", "post", "--json"])
    printed = capsys.readouterr().out
    assert snapshot() == before
    blob = (
        json.dumps([c.__dict__ if hasattr(c, "__dict__") else str(c) for c in checks], default=str)
        + json.dumps(plan)
        + printed
    )
    assert STRONG not in blob and m.destination not in blob and rc in (0, 1)
    json.loads(printed)  # JSON i vlefshëm


def test_rollback_plan_reports_in_flight_work_and_forbids_resending_unknown(db, cw):
    queued = put(db, 3)
    sent = put(db, 3)
    unknown = put(db, 4)
    for _ in range(3):
        svc.process_one(db, datetime.now(UTC) + timedelta(seconds=1))
    db.refresh(queued), db.refresh(sent), db.refresh(unknown)
    plan = pr.rollback_plan(db, pc.PROVIDER)
    assert plan["in_flight"]["unknown"] == 1 == plan["must_not_resend"]
    assert (
        plan["in_flight"]["sent"] + plan["in_flight"]["queued"] + plan["in_flight"]["sending"] == 2
    )
    assert plan["enabled_routes"] == [{"prefix": pc.PREFIX, "priority": 100}]
    assert plan["unknown_oldest_age_seconds"] is not None
    steps = " ".join(plan["steps"])
    assert "NEVER resend UNKNOWN" in steps and "dispatch" in steps and "reconcil" in steps
    assert unknown.status == MessageStatus.UNKNOWN  # i paprekur nga plani


def test_cli_exit_codes_and_usage(db, cw, capsys):
    assert pr.main(["check", "--provider", pc.PROVIDER]) == 0
    assert pr.main(["check", "--provider", "no-such-provider"]) == 1
    assert "NO-GO" in capsys.readouterr().out
    assert pr.main(["rollback-plan", "--provider", pc.PROVIDER, "--json"]) == 0
    json.loads(capsys.readouterr().out)
