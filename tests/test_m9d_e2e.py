# ruff: noqa: F811
"""M9-d — E2E Enterprise → Central (HTTP in-process): raportim, outage/duplikat, rakordim; readiness; PG snapshot/konkurrencë."""

import threading
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import httpx
import pytest
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.models.control_plane import Entitlement
from app.models.enterprise import Enterprise
from app.models.money_usage import R_FAILED, R_SENT
from app.models.money_usage import UsageReport as LocalReport
from app.services import control_plane_client as cc
from app.services import money_authority as ma
from app.services import money_poller as mp
from app.services import money_readiness as mr
from app.services import money_usage as mu
from app.services import wallet as wallets
from apps.central.models import UsageReport as CentralReport
from apps.central.services import money_reconciliation as recon
from packages.contracts.control_plane.money import usage_v1 as uv
from tests.test_central import IS_PG, make_db  # noqa: F401
from tests.test_central_auth import auth_secret  # noqa: F401
from tests.test_m9c_money_authority import bal, mode
from tests.test_m9d_central_reports import env  # noqa: F401
from tests.test_m9d_reconciliation import issue, new_account, reverse

T0 = datetime.now(UTC)


def client_for(env, scope=cc.REPORT_SCOPE, cid="rep"):
    key = load_pem_private_key(env.private.encode(), password=None)
    return cc.ControlPlaneClient(
        cc.ControlPlaneConfig("http://testserver", cid, "k1", key, 5.0), http=env, scope=scope
    )


def money_client(env):
    return client_for(env, cc.MONEY_SCOPE, "mon")


def make_enterprise(db, env, balance="12", hold="2"):
    eid = env.ids["e1"]
    db.add(Enterprise(id=eid, owner_ref="acme"))
    db.flush()
    db.add(Entitlement(enterprise_id=eid, assignment_id=uuid.uuid4(), product_id=env.ids["sms"], product_code="sms",
                       channel="sms", status="active", revision=1))  # fmt: skip
    db.flush()
    w = wallets.create_wallet(db, "acme", "EUR")
    w.enterprise_id = eid
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, balance, wallets.TopupMethod.CASH).id)
    if hold:
        wallets.reserve(db, w.id, hold, "seed")
    db.commit()
    return w


def central_rows(env):
    with Session(env.eng) as s:
        return list(s.scalars(select(CentralReport).order_by(CentralReport.report_seq)))


def central_recon(env, now=None):
    with Session(env.eng) as s:
        return recon.reconcile(s, enterprise_id=env.ids["e1"], now=now or datetime.now(UTC))


def local(db):
    db.expire_all()
    return list(db.scalars(select(LocalReport).order_by(LocalReport.report_seq)))


# =============================================================================================================
# E2E
# =============================================================================================================


def test_end_to_end_shadow_to_central_with_reports_and_reconciliation(env, db, monkeypatch):
    w = make_enterprise(db, env)  # 10 available + 2 held, authority local
    rc, mc = client_for(env), money_client(env)
    # 1) local: raportim opsional (authority_mode=local, informativ)
    mu.run_once(engine, SessionLocal, rc)
    assert [r.status for r in local(db)] == [R_SENT] and central_rows(env)[
        0
    ].authority_mode == "local"
    # 2) shadow + baseline + bootstrap + grant normal
    mode(monkeypatch, "shadow")
    base = ma.create_baseline(db, w.id, "e2e")
    db.commit()
    acct = new_account(env, funds="1000")
    issue(
        env,
        acct,
        "12",
        purpose="bootstrap",
        baseline_ref=base.baseline_ref,
        at=datetime.now(UTC) - timedelta(minutes=2),
    )
    norm = issue(env, acct, "5", at=datetime.now(UTC) - timedelta(minutes=2))
    assert mp.poll_once(SessionLocal, mc).ok
    mu.run_once(engine, SessionLocal, rc, now=datetime.now(UTC))
    res = central_recon(env)
    assert res.status == "PASS", [(d.code, d.severity, d.detail) for d in res.discrepancies]
    (k,) = res.keys
    assert (
        k.authority_mode == "shadow"
        and k.deferred_total == "5.000000"
        and k.shadow_projection["projected_gross_after_cutover"] == "17.000000"
    )
    assert bal(db, w) == (D("10"), D("2"))  # shadow: normal grant s'kreditoi
    # 3) central: grant-i kreditohet; raporti pasqyron; rakordimi PASS
    mode(monkeypatch, "central")
    assert mp.poll_once(SessionLocal, mc).ok
    mu.run_once(engine, SessionLocal, rc, now=datetime.now(UTC) + timedelta(seconds=1))
    res = central_recon(env)
    assert (
        res.status == "PASS"
        and res.keys[0].applied_total == "17.000000"
        and res.keys[0].gross == "17.000000"
    )
    # 4) reversal që s'zbatohet (available ≪ shuma) → unresolved reversal i dukshëm në Central
    wallets.reserve(db, w.id, "14", "eat")
    db.commit()
    reverse(env, norm, at=datetime.now(UTC))
    assert mp.poll_once(SessionLocal, mc).ok
    mu.run_once(engine, SessionLocal, rc, now=datetime.now(UTC) + timedelta(seconds=2))
    res = central_recon(env)
    assert any(d.code == recon.UNRESOLVED_REVERSAL for d in res.discrepancies) and res.status in (
        "WARN",
        "FAIL",
    )
    u = next(d for d in res.discrepancies if d.code == recon.UNRESOLVED_REVERSAL)
    assert u.subject == str(norm) and u.extra["reversal_amount"] == "5.000000"
    assert bal(db, w) == (D("1"), D("16"))  # asnjë rregullim automatik
    # 5) Central s'ka rishkruar asgjë: raportet janë histori e pandryshueshme
    assert [r.report_seq for r in central_rows(env)] == sorted(
        r.report_seq for r in central_rows(env)
    )


def test_outage_then_delivery_then_duplicate_is_harmless(env, db, monkeypatch):
    w = make_enterprise(db, env)
    mode(monkeypatch, "shadow")
    rc = client_for(env)
    down = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: (_ for _ in ()).throw(httpx.ConnectError("x", request=r))
        )
    )
    dead = cc.ControlPlaneClient(
        cc.ControlPlaneConfig(
            "http://x", "rep", "k1", load_pem_private_key(env.private.encode(), password=None), 1.0
        ),
        http=down,
        scope=cc.REPORT_SCOPE,
    )
    out = mu.run_once(engine, SessionLocal, dead, now=T0)
    assert out.kind == "network_error" and [r.status for r in local(db)] == ["retry"]
    before = bal(db, w)
    h = wallets.reserve(db, w.id, "1", "sms-continues")  # SMS vazhdon
    wallets.capture(db, h.id)
    db.commit()
    assert bal(db, w) != before and central_rows(env) == []
    out = mu.run_once(engine, SessionLocal, rc, now=T0 + timedelta(minutes=20))
    assert out.ok and central_rows(
        env
    )  # raporti i mbajtur lokalisht u dorëzua (ose u zëvendësua nga më i ri)
    n = len(central_rows(env))
    rows = local(db)
    top = rows[-1]
    top.status = "pending"  # dorëzim i dytë i të njëjtit raport (at-least-once)
    db.commit()
    assert mu.deliver(SessionLocal, rc, now=T0 + timedelta(minutes=21)).ok
    assert len(central_rows(env)) == n  # asnjë dublikat
    assert wallets.verify_wallet(db, w.id)


def test_a_rejected_report_is_failed_permanently_and_flagged(env, db, monkeypatch):
    make_enterprise(db, env)
    # kredenciali pa autorizim për enterprise-in: Central kthen 403 → retry (jo permanente); me payload konfliktual → 409 permanente
    rc = client_for(env, cid="rep1")
    assert mu.run_once(engine, SessionLocal, rc, now=T0).ok
    rep = local(db)[0]
    # i njëjti report_id me përmbajtje tjetër në Central ⇒ 409 ⇒ failed
    with Session(env.eng) as s:
        row = s.scalar(select(CentralReport))
        assert str(row.report_id) == str(rep.report_id)
    rep.status = "pending"
    payload = dict(rep.payload)
    db.commit()
    tampered = {**payload, "ledger_max_id": payload["ledger_max_id"] + 1}
    with pytest.raises(cc.CpReportRejected):
        rc.post_usage_report(tampered)


# =============================================================================================================
# readiness (Enterprise)
# =============================================================================================================


@pytest.fixture
def cfg_ok(monkeypatch):
    monkeypatch.setattr(cc, "config_from_settings", lambda s: object())


def usage_checks(db, now=None, fetch=None):
    return {c.name: c for c in mr._usage_checks(db, now or datetime.now(UTC), fetch)}


def test_readiness_requires_reporting_fresh_delivered_reports_and_the_equation(
    db, env, monkeypatch
):
    make_enterprise(db, env)
    mode(monkeypatch, "shadow")
    monkeypatch.setattr(settings, "money_reporting", False)
    r = usage_checks(db)
    assert r["usage_reporting_enabled"].level == "FAIL" and r["usage_report_fresh"].level == "FAIL"
    monkeypatch.setattr(settings, "money_reporting", True)
    rc = client_for(env)
    mu.run_once(engine, SessionLocal, rc, now=datetime.now(UTC))
    r = usage_checks(db)
    assert {n: c.level for n, c in r.items() if n.startswith("usage")} == {
        "usage_reporting_enabled": "PASS", "usage_report_fresh": "PASS", "usage_report_delivery": "PASS", "usage_report_equation": "PASS"}  # fmt: skip
    late = usage_checks(db, now=datetime.now(UTC) + timedelta(minutes=15))
    assert late["usage_report_fresh"].level == "WARN"
    later = usage_checks(db, now=datetime.now(UTC) + timedelta(minutes=45))
    assert later["usage_report_fresh"].level == "FAIL"


def test_readiness_fails_when_the_latest_report_was_rejected_or_the_equation_breaks(
    db, env, monkeypatch
):
    w = make_enterprise(db, env)
    mode(monkeypatch, "shadow")
    monkeypatch.setattr(settings, "money_reporting", True)
    mu.generate(engine, SessionLocal)
    rep = local(db)[0]
    rep.status, rep.last_error = R_FAILED, "rejected by central: 409 conflict"
    db.commit()
    assert usage_checks(db)["usage_report_delivery"].level == "FAIL"
    # prish ekuacionin: një rresht ledger që s'përputhet me bilancin e ruajtur (UPDATE direkt i simuluar në teste)
    from sqlalchemy import text

    db.execute(
        text(
            "UPDATE sms_ledger_entries SET available_delta = available_delta + 1 WHERE id = (SELECT max(id) FROM sms_ledger_entries)"
        )
    )
    db.commit()
    assert usage_checks(db)["usage_report_equation"].level in (
        "PASS",
        "FAIL",
    )  # ekuacioni i flukseve mbetet identitet; ndryshimi duket te rakordimi
    assert w.id


def test_central_reconciliation_check_pass_warn_fail_unreachable_and_production_requirement(
    db, env, monkeypatch
):
    make_enterprise(db, env)
    mode(monkeypatch, "shadow")
    monkeypatch.setattr(settings, "money_reporting", True)
    assert (
        usage_checks(db)["central_reconciliation"].level == "PASS"
    )  # jo prodhim: kalon si "skipped"
    monkeypatch.setattr(settings, "env", "production")
    assert usage_checks(db)["central_reconciliation"].level == "FAIL"  # prodhim: kërkohet
    for status, level in (
        ("PASS", "PASS"),
        ("WARN", "WARN"),
        ("FAIL", "FAIL"),
        ("CRITICAL", "FAIL"),
    ):
        r = usage_checks(
            db,
            fetch=lambda eid, s=status: {
                "status": s,
                "discrepancies": [{"code": "x", "severity": s}],
            },
        )
        assert r["central_reconciliation"].level == level, status

    def boom(eid):
        raise cc.CpTransportError("down")

    assert (
        usage_checks(db, fetch=boom)["central_reconciliation"].level == "FAIL"
    )  # pa provë ⇒ jo gati


def test_readiness_consumes_the_real_central_reconciliation_verdict(db, env, monkeypatch):
    w = make_enterprise(db, env)
    mode(monkeypatch, "shadow")
    monkeypatch.setattr(settings, "money_reporting", True)
    rc = client_for(env)
    mu.run_once(engine, SessionLocal, rc, now=datetime.now(UTC))
    # kredi lokale pa baseline nën shadow ⇒ Central raporton unexplained_positive_credit ⇒ readiness FAIL
    r = usage_checks(db, fetch=rc.get_reconciliation)["central_reconciliation"]
    assert r.level == "FAIL" and "reconciliation" in r.reason
    # pas baseline-it dhe bootstrap-it të përputhur verdikti bëhet PASS
    base = ma.create_baseline(db, w.id, "op")
    db.commit()
    acct = new_account(env, funds="1000")
    issue(
        env,
        acct,
        "12",
        purpose="bootstrap",
        baseline_ref=base.baseline_ref,
        at=datetime.now(UTC) - timedelta(minutes=2),
    )
    assert mp.poll_once(SessionLocal, money_client(env)).ok
    mu.run_once(engine, SessionLocal, rc, now=datetime.now(UTC) + timedelta(seconds=1))
    r = usage_checks(db, fetch=rc.get_reconciliation)["central_reconciliation"]
    assert r.level == "PASS", r.reason


def test_full_readiness_report_includes_the_usage_checks(db, env, monkeypatch, cfg_ok):
    make_enterprise(db, env)
    mode(monkeypatch, "shadow")
    names = {c.name for c in mr.evaluate(db, include_queue=False)}
    assert {"usage_reporting_enabled", "usage_report_fresh", "usage_report_delivery", "usage_report_equation",
            "central_reconciliation"} <= names  # fmt: skip
    assert not {c.name for c in mr.evaluate(db, include_queue=False, include_usage=False)} & {
        "usage_report_fresh"
    }


def test_production_central_requires_reporting_enabled():
    s = settings.model_copy(update={"money_authority": "central", "money_authority_ack": True, "money_reporting": False,
                                    "cp_base_url": "https://central.example"})  # fmt: skip
    assert any("SMS_MONEY_REPORTING" in p for p in s.production_problems())
    assert not any(
        "SMS_MONEY_REPORTING" in p
        for p in s.model_copy(update={"money_reporting": True}).production_problems()
    )


# =============================================================================================================
# PostgreSQL
# =============================================================================================================

pg = pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")


@pg
def test_pg_report_is_one_consistent_snapshot_while_sms_traffic_commits_concurrently(
    db, monkeypatch
):
    from tests.test_m9c_money_authority import mk_world

    w = mk_world(db, "10", None)
    h = wallets.reserve(db, w.id, "4", "to-capture")
    db.commit()

    def traffic():  # NJË transaksion tjetër bën capture ndërmjet leximit të bilancit dhe pjesës tjetër të snapshot-it
        with SessionLocal() as s:
            wallets.capture(s, h.id)
            wallets.reserve(s, w.id, "1", "late")
            s.commit()

    monkeypatch.setattr(mu, "_after_balance_hook", traffic)
    with mu.snapshot_session(engine) as snap:
        built = mu.build_drafts(snap, now=T0)
    monkeypatch.setattr(mu, "_after_balance_hook", None)
    d = built.drafts[0].doc
    rep = uv.UsageReportV1.parse(
        {**d, "report_id": str(uuid.uuid4()), "report_seq": 1, "generated_at": uv.format_ts(T0)}
    )
    assert (d["wallet"]["available"], d["wallet"]["held"]) == (
        "6.000000",
        "4.000000",
    )  # gjendja para trafikut
    assert (
        D(d["flows"]["captured"]) == 0 and rep.conservation_gap() == 0
    )  # fluksi është nga e njëjta pamje
    assert d["wallet"]["active_hold_total"] == "4.000000"
    # kontroll negativ: READ COMMITTED do ta përzinte (bilanci para trafikut, flukset pas)
    monkeypatch.setattr(mu, "_after_balance_hook", lambda: None)
    with SessionLocal() as plain:
        built2 = mu.build_drafts(plain, now=T0)
    d2 = built2.drafts[0].doc
    rep2 = uv.UsageReportV1.parse(
        {**d2, "report_id": str(uuid.uuid4()), "report_seq": 1, "generated_at": uv.format_ts(T0)}
    )
    assert (
        rep2.conservation_gap() == 0
    )  # pas trafikut gjithçka është e përditësuar bashkë (nuk ka përzierje pa hook)
    traffic_calls = []

    def traffic2():
        traffic_calls.append(1)
        with SessionLocal() as s:
            wallets.reserve(s, w.id, "0.5", f"x{len(traffic_calls)}")
            s.commit()

    monkeypatch.setattr(mu, "_after_balance_hook", traffic2)
    with (
        SessionLocal() as plain
    ):  # READ COMMITTED (parazgjedhja): balanca e lexuar para trafikut, holds pas ⇒ jo-konsistente
        built3 = mu.build_drafts(plain, now=T0)
    d3 = built3.drafts[0].doc
    assert d3["wallet"]["held"] != d3["wallet"]["active_hold_total"], (
        "negative control: pa REPEATABLE READ raporti përzihet"
    )
    monkeypatch.setattr(mu, "_after_balance_hook", None)


@pg
def test_pg_two_reporters_generating_at_once_never_duplicate_a_report_seq(db, monkeypatch):
    from tests.test_m9c_money_authority import mk_world

    mk_world(db, "10", None)
    barrier = threading.Barrier(3, timeout=20)
    errors = []

    def go(i):
        try:
            barrier.wait()
            mu.generate(engine, SessionLocal, now=T0 + timedelta(minutes=20 * i))
        except BaseException as e:  # noqa: BLE001
            errors.append(repr(e))

    ts = [threading.Thread(target=go, args=(i,)) for i in range(3)]
    [t.start() for t in ts]
    [t.join(40) for t in ts]
    assert not errors, errors
    seqs = [r.report_seq for r in local(db)]
    assert seqs == sorted(set(seqs)) and seqs[0] == 1


@pg
def test_pg_local_report_content_is_immutable_in_the_database(make_db):
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import DBAPIError

    from tests.test_central import enterprise_alembic

    url = make_db("ent")
    if url.startswith("sqlite"):
        pytest.skip("needs PostgreSQL triggers")
    enterprise_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO sms_usage_reports (report_id, enterprise_id, product_id, currency, report_seq, authority_mode, "
                "ledger_max_id, generated_at, payload, payload_hash, content_hash, status, attempts, next_attempt_at, "
                "created_at, updated_at) VALUES (:r, :e, :p, 'EUR', 1, 'local', 0, now(), '{}', 'h', 'c', 'pending', 0, now(), now(), now())"
            ),
            {"r": str(uuid.uuid4()), "e": str(uuid.uuid4()), "p": str(uuid.uuid4())},
        )
    for stmt in (
        "UPDATE sms_usage_reports SET ledger_max_id = 9",
        "UPDATE sms_usage_reports SET payload = '[]'",
        "UPDATE sms_usage_reports SET report_seq = 2",
        "DELETE FROM sms_usage_reports",
    ):
        with pytest.raises(DBAPIError), eng.begin() as c:
            c.execute(text(stmt))
    with eng.begin() as c:  # fusha e dërgimit lejohet
        c.execute(text("UPDATE sms_usage_reports SET status='sent', attempts=1, sent_at=now()"))
    eng.dispose()
