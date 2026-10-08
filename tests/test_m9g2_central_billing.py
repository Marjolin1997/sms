# ruff: noqa: F811
"""M9-g2 — Central: ingest i raporteve kumulative `cp.billing.usage.v1` + faturimi i overage-it të email-it."""

import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from apps.central.core import errors
from apps.central.main import create_app
from apps.central.models import AuditLog, CentralUser
from apps.central.models.billing import (
    BillingImmutableError,
    BillingPeriod,
    CommercialPlan,
    Invoice,
    InvoiceLine,
)
from apps.central.models.billing_usage import BillingUsageReport
from apps.central.services import (
    billing,
    billing_overage,
    billing_plans,
    billing_readiness,
    billing_usage,
    pricing,
    service_auth,
    users,
)
from apps.central.services import enterprise_products as eprod
from apps.central.services import enterprises as ent
from apps.central.services import products as prod
from apps.central.tools import billing_readiness as cli_ready
from apps.central.tools import billing_run as cli_run
from packages.contracts.control_plane.billing import usage_v1 as bv
from tests.test_central import IS_PG, central_alembic, make_db  # noqa: F401
from tests.test_central_auth import PW, auth_secret  # noqa: F401
from tests.test_central_sync_api import assertion, auth, keypair

T0 = datetime(2030, 1, 1, 0, tzinfo=UTC)  # abonim: periudha 0 = [1 jan, 1 shk)
JAN_END = datetime(2030, 2, 1, 0, tzinfo=UTC)
FEB_END = datetime(2030, 3, 1, 0, tzinfo=UTC)
NOW = datetime(2030, 3, 5, 12, tzinfo=UTC)


class Env:
    pass


@pytest.fixture
def env(make_db):
    url = make_db()
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    private, public = keypair()
    e = Env()
    e.url, e.eng, e.private = url, eng, private
    e.F = sessionmaker(bind=eng, expire_on_commit=False)
    with e.F() as s:
        e1, e2 = ent.create(s, "Acme"), ent.create(s, "Beta")
        sms = prod.create(s, "sms", "SMS", "sms")
        email = prod.create(s, "email", "Email", "email")
        admin = users.create_user(s, "a1@example.com", PW, "admin")
        for cid, scopes, ents in (("bill", ["billing:report"], [e1.id]), ("mon", ["money:report"], [e1.id]),
                                  ("sync", ["sync:read"], [e1.id])):  # fmt: skip
            service_auth.create_client(s, cid, scopes, ents)
            service_auth.add_key(s, cid, "k1", public)
        eprod.assign_product(s, e1.id, email.id)
        s.commit()
        e.e1, e.e2, e.sms, e.email, e.admin = e1.id, e2.id, sms.id, email.id, admin.id
    e.seq = 0
    yield e
    eng.dispose()


def A(s, env):
    return s.get(CentralUser, env.admin)


def setup_billing(
    env,
    *,
    fee="10",
    included=100,
    cur="EUR",
    price="0.02",
    price_cur="EUR",
    with_price=True,
    anchor=T0,
):
    """Profil + plan + abonim + (opsionale) çmim email Central nga para anchor-it. → (sid, plan_version_id)."""
    with env.F() as s:
        a = A(s, env)
        billing.set_profile(
            s, a, env.e1, "Acme Sh.p.k.", "Rr. 1", "al", "b@acme.example", None, "0"
        )
        plan = billing_plans.create_plan(s, a, "std", "Standard")
        v = billing_plans.new_version(s, a, plan.id, cur, fee, included)
        billing_plans.activate(s, a, v.id)
        sub = billing.assign_plan(s, a, env.e1, v.id, now=anchor)
        if with_price:
            set_price(env, s, price, price_cur, anchor=anchor)
        s.commit()
        return sub.id, v.id


def set_price(env, s, price, cur="EUR", code="em", eff=None, anchor=T0):
    eff = eff or anchor - timedelta(days=30)
    base = eff - timedelta(
        days=10
    )  # `now` i veprimeve të çmimit: para efektivitetit (activate/assign s'pranojnë të shkuarën)
    a = A(s, env)
    book = pricing.create_book(s, a, code, code.upper(), cur, now=base)
    v = pricing.new_draft(s, a, book.id, now=base)
    pricing.set_rule(s, a, v.id, "email", price, now=base)
    pricing.activate(s, a, v.id, eff, now=base)
    pricing.assign(s, a, env.e1, env.email, book.id, eff, now=base)
    return book.id, v.id


def report(env, count, at, *, watermark=None, seq=None, eid=None, pid=None, rid=None):
    env.seq = seq if seq is not None else env.seq + 1
    wm = (
        watermark if watermark is not None else (0 if count == 0 else count + 3)
    )  # id-të me boshllëqe
    doc = {"schema": bv.SCHEMA, "report_id": rid or str(uuid.uuid4()), "report_seq": env.seq,
           "enterprise_id": str(eid or env.e1), "product_id": str(pid or env.email),
           "generated_at": bv.format_ts(at), "watermark": wm, "cumulative_billable_count": count}  # fmt: skip
    return bv.BillingUsageReportV1.parse(doc)


def ingest(env, count, at, **kw):
    r = report(env, count, at, **kw)
    with env.F() as s:
        out = billing_usage.ingest(s, r, now=max(at, NOW))
        s.commit()
        return out, r


def run(env, sid, now=NOW):
    with env.F() as s:
        r = billing.process_period(s, sid, now)
        s.commit()
        return r


def lines(env, invoice_id):
    with env.F() as s:
        return list(
            s.scalars(
                select(InvoiceLine)
                .where(InvoiceLine.invoice_id == invoice_id)
                .order_by(InvoiceLine.line_no)
            )
        )


# =============================================================================================================
# ingest
# =============================================================================================================


def test_ingest_is_idempotent_and_conflicts_are_detected(env):
    out, r = ingest(env, 5, JAN_END)
    assert out.created and out.latest and out.row.cumulative_billable_count == 5
    with env.F() as s:
        again = billing_usage.ingest(s, r, now=NOW)
        assert not again.created and again.row.report_id == out.row.report_id
        s.commit()
    with env.F() as s:
        assert s.scalar(select(func.count()).select_from(BillingUsageReport)) == 1
        other = report(
            env, 6, JAN_END, rid=r.report_id, seq=r.report_seq
        )  # i njëjti report_id, payload tjetër
        with pytest.raises(errors.Conflict):
            billing_usage.ingest(s, other, now=NOW)
        s.rollback()
        taken = report(
            env, 6, JAN_END + timedelta(hours=1), seq=r.report_seq
        )  # seq i zënë nga raport tjetër
        with pytest.raises(errors.Conflict):
            billing_usage.ingest(s, taken, now=NOW)


def test_regression_is_rejected_against_both_neighbours_and_nothing_is_stored(env):
    ingest(env, 10, JAN_END, seq=2)
    ingest(env, 20, JAN_END + timedelta(days=1), seq=4)
    with env.F() as s:
        for count, at, seq in (
            (9, JAN_END + timedelta(hours=1), 3),  # numërues më i vogël se fqinji i poshtëm
            (21, JAN_END + timedelta(hours=1), 3),  # më i madh se fqinji i sipërm
            (15, JAN_END - timedelta(hours=1), 3),  # koha para fqinjit të poshtëm
            (15, JAN_END + timedelta(days=2), 3),
        ):  # koha pas fqinjit të sipërm
            with pytest.raises(errors.Conflict):
                billing_usage.ingest(
                    s, report(env, count, at, seq=seq, watermark=count + 3), now=NOW
                )
            s.rollback()
        with pytest.raises(errors.Conflict):  # watermark që bie
            billing_usage.ingest(
                s, report(env, 10, JAN_END + timedelta(hours=1), seq=3, watermark=10), now=NOW
            )
        s.rollback()
        ok = billing_usage.ingest(
            s, report(env, 15, JAN_END + timedelta(hours=1), seq=3, watermark=18), now=NOW
        )
        assert ok.created and not ok.latest  # raport i vonuar në mes: ruhet, s'bëhet aktual
        s.commit()
    with env.F() as s:
        assert s.scalar(select(func.count()).select_from(BillingUsageReport)) == 3


def test_ingest_validates_product_enterprise_and_future_timestamps(env):
    with env.F() as s:
        with pytest.raises(errors.Invalid):  # produkt SMS
            billing_usage.ingest(s, report(env, 1, JAN_END, pid=env.sms), now=NOW)
        with pytest.raises(errors.Invalid):  # produkt i panjohur
            billing_usage.ingest(s, report(env, 1, JAN_END, pid=uuid.uuid4()), now=NOW)
        with pytest.raises(errors.Invalid):  # enterprise i panjohur
            billing_usage.ingest(s, report(env, 1, JAN_END, eid=uuid.uuid4()), now=NOW)
        with pytest.raises(
            errors.Invalid
        ):  # e ardhmja: do të "mbulonte" periudha që s'kanë mbaruar
            billing_usage.ingest(s, report(env, 1, NOW + timedelta(hours=1)), now=NOW)
        with pytest.raises(errors.Invalid):
            billing_usage.parse({"schema": "x"})
        s.rollback()
        assert s.scalar(select(func.count()).select_from(BillingUsageReport)) == 0


def test_reports_are_immutable_orm_and_postgres_trigger(env):
    out, _ = ingest(env, 3, JAN_END)
    with env.F() as s:
        row = s.get(BillingUsageReport, out.row.report_id)
        row.cumulative_billable_count = 0
        with pytest.raises(BillingImmutableError):
            s.flush()
        s.rollback()
        s.delete(s.get(BillingUsageReport, out.row.report_id))
        with pytest.raises(BillingImmutableError):
            s.flush()
        s.rollback()
    if env.eng.dialect.name == "postgresql":
        with env.eng.begin() as c, pytest.raises(DBAPIError):
            c.execute(text("UPDATE billing_usage_reports SET cumulative_billable_count = 0"))
        with env.eng.begin() as c, pytest.raises(DBAPIError):
            c.execute(text("DELETE FROM billing_usage_reports"))
        with env.eng.begin() as c, pytest.raises(DBAPIError):
            c.execute(text("TRUNCATE billing_usage_reports"))


# =============================================================================================================
# API + autorizim
# =============================================================================================================


def tok(env, client="bill", scope="billing:report"):
    return assertion(env.private, client=client, kid="k1", scope=scope)


def post(env, c, body, client="bill", scope="billing:report"):
    return c.post(
        "/internal/billing/usage-reports", json=body, headers=auth(tok(env, client, scope))
    )


def test_api_scope_authorization_idempotency_and_status_codes(env):
    c = TestClient(create_app(env.eng))
    r = report(env, 4, datetime.now(UTC) - timedelta(minutes=1))
    body = r.to_dict()
    assert c.post("/internal/billing/usage-reports", json=body).status_code == 401
    assert post(env, c, body, "mon", "money:report").status_code == 403  # scope tjetër
    assert post(env, c, body, "sync", "sync:read").status_code == 403
    assert post(env, c, body, "mon", "billing:report").status_code == 403  # klienti s'e ka scope-in
    other = report(env, 4, datetime.now(UTC) - timedelta(minutes=1), eid=env.e2).to_dict()
    assert post(env, c, other).status_code == 403  # enterprise i paautorizuar për klientin
    first = post(env, c, body)
    assert first.status_code == 201 and first.json()["status"] == "stored"
    dup = post(env, c, body)
    assert dup.status_code == 200 and dup.json()["status"] == "duplicate"
    changed = {**body, "cumulative_billable_count": 5, "watermark": 9}
    assert post(env, c, changed).status_code == 409
    assert post(env, c, {**body, "extra": 1}).status_code == 422
    big = c.post("/internal/billing/usage-reports", content=json.dumps({"x": "y" * 10_000}).encode(),
                 headers={**auth(tok(env)), "content-type": "application/json"})  # fmt: skip
    assert big.status_code == 413
    with env.F() as s:
        assert s.scalar(select(func.count()).select_from(BillingUsageReport)) == 1
    # klient i çaktivizuar
    with env.F() as s:
        service_auth.disable_client(s, "bill")
        s.commit()
    assert (
        post(
            env, c, report(env, 5, datetime.now(UTC) - timedelta(seconds=30)).to_dict()
        ).status_code
        == 401
    )


# =============================================================================================================
# periudha: pritje, delta, overage
# =============================================================================================================


def test_billing_waits_for_a_covering_report_and_never_estimates(env):
    sid, _ = setup_billing(env)
    ingest(env, 50, T0 - timedelta(hours=1))  # baseline para periudhës
    ingest(env, 120, JAN_END - timedelta(hours=1))  # para fundit të periudhës: NUK mbulon
    r = run(env, sid)
    assert r.kind == "waiting_usage" and r.reason == billing_overage.WAIT_REPORT
    with env.F() as s:
        assert s.scalar(select(func.count()).select_from(BillingPeriod)) == 0
        assert s.scalar(select(func.count()).select_from(Invoice)) == 0
        assert billing.get_subscription(s, env.e1).next_period_index == 0
    ingest(env, 130, JAN_END + timedelta(minutes=5))  # raporti i prerjes
    r = run(env, sid)
    assert r.kind == "invoiced"


def test_monthly_plus_overage_uses_frozen_plan_and_price_snapshot(env):
    sid, vid = setup_billing(env, fee="10", included=100, price="0.02")
    ingest(env, 50, T0 - timedelta(hours=1))
    _, cut = ingest(env, 200, JAN_END + timedelta(minutes=1))  # delta 150, included 100 ⇒ extra 50
    r = run(env, sid)
    assert r.kind == "invoiced"
    ls = lines(env, r.invoice.id)
    assert [x.line_type for x in ls] == ["monthly_fee", "email_overage"]
    ov = ls[1]
    assert (ov.quantity, ov.unit_price, ov.amount, ov.currency, ov.pricing_source) == (
        D(50),
        D("0.02"),
        D("1.00"),
        "EUR",
        "central",
    )
    assert (
        ov.plan_version_id == vid and ov.price_book_id and ov.price_version_id and ov.price_rule_id
    )
    assert (billing.utc(ov.period_start), billing.utc(ov.period_end)) == (T0, JAN_END)
    assert r.invoice.subtotal == D("11.00")
    with env.F() as s:
        p = s.scalar(select(BillingPeriod))
        assert (p.usage_from, p.usage_to, str(p.usage_to_report_id)) == (50, 200, cut.report_id)
        assert p.usage_from_report_id is not None and p.status == "invoiced"
        assert billing.verify_invoice(s, s.get(Invoice, r.invoice.id)) == []
    # çmimi i ri më vonë NUK ndryshon faturën e lëshuar (foto e ngrirë)
    with env.F() as s:
        set_price(env, s, "0.50", code="em2", eff=JAN_END + timedelta(days=1))
        s.commit()
    assert [(x.unit_price, x.amount) for x in lines(env, r.invoice.id)][1] == (D("0.02"), D("1.00"))


def test_overage_only_no_charge_and_zero_overage_rules(env):
    sid, _ = setup_billing(env, fee="0", included=100, price="0.10")
    ingest(env, 0, T0 - timedelta(hours=1))
    ingest(
        env, 100, JAN_END + timedelta(minutes=1)
    )  # delta = included ⇒ pa overage, pa tarifë ⇒ no_charge
    r = run(env, sid)
    assert r.kind == "no_charge" and r.invoice is None
    with env.F() as s:
        p = s.scalar(select(BillingPeriod))
        assert (p.usage_from, p.usage_to, p.status) == (
            0,
            100,
            "no_charge",
        )  # prova e matjes mbetet edhe pa faturë
    ingest(
        env, 130, FEB_END + timedelta(minutes=1)
    )  # periudha 1: delta 30 < included ⇒ prapë asgjë
    assert run(env, sid).kind == "no_charge"
    ingest(
        env, 330, FEB_END + timedelta(days=32)
    )  # periudha 2: delta 200 ⇒ extra 100, tarifë 0 ⇒ vetëm overage
    r = run(env, sid, FEB_END + timedelta(days=40))
    assert r.kind == "invoiced"
    ls = lines(env, r.invoice.id)
    assert (
        [x.line_type for x in ls] == ["email_overage"]
        and ls[0].amount == D("10.00")
        and r.invoice.subtotal == D("10.00")
    )


def test_late_email_is_billed_exactly_once_in_the_next_delta(env):
    """Krijuar 30 jan, ende SENDING në faturimin e janarit, DELIVERED 2 shk ⇒ numërohet në shkurt, saktësisht një herë."""
    sid, _ = setup_billing(env, fee="0", included=0, price="1.00")
    ingest(env, 10, T0 - timedelta(hours=1))
    ingest(
        env, 12, JAN_END + timedelta(minutes=1)
    )  # janari: 2 email të faturueshëm (i vonuari s'është ende)
    r1 = run(env, sid)
    assert r1.kind == "invoiced" and lines(env, r1.invoice.id)[0].quantity == D(2)
    ingest(
        env, 15, FEB_END + timedelta(minutes=1)
    )  # shkurti: +3 (përfshin të vonuarin e DELIVERED më 2 shk)
    r2 = run(env, sid)
    assert r2.kind == "invoiced" and lines(env, r2.invoice.id)[0].quantity == D(3)
    with env.F() as s:
        ps = list(s.scalars(select(BillingPeriod).order_by(BillingPeriod.period_index)))
        assert [(p.usage_from, p.usage_to) for p in ps] == [
            (10, 12),
            (12, 15),
        ]  # zinxhir i pandërprerë
        assert ps[1].usage_from_report_id == ps[0].usage_to_report_id
        assert (
            sum(x for x in (p.usage_to - p.usage_from for p in ps)) == 15 - 10
        )  # asnjë email dy herë
    assert (
        run(env, sid, FEB_END + timedelta(minutes=2)).kind == "not_due"
    )  # rirunimi s'ndryshon asgjë


def test_rerun_and_billing_run_never_double_bill_and_report_stats(env, capsys):
    old = datetime(
        2024, 1, 1, tzinfo=UTC
    )  # tool-i përdor orën reale: periudha 0 e mbyllur prej kohësh
    sid, _ = setup_billing(env, fee="5", included=0, price="1.00", anchor=old)
    ingest(env, 0, old - timedelta(hours=1))
    assert (
        cli_run.main(["--json"], engine=env.eng) == 3
    )  # M9-g4: autoriteti i faturimit është ende `local` ⇒ billing_run refuzon
    with env.F() as s:
        from apps.central.models.billing_import import BillingAuthorityState

        s.add(BillingAuthorityState(id=1, mode="central", ack=True))
        s.commit()
    assert cli_run.main(["--json"], engine=env.eng) == 0
    first = json.loads(capsys.readouterr().out)
    assert (
        first["waiting_usage"] == 1 and first["due"] == 1 and first["invoiced"] == 0
    )  # pa raport prerjeje: pret
    ingest(env, 7, datetime(2024, 2, 1, 0, 1, tzinfo=UTC))
    out = []
    for _ in range(3):
        assert cli_run.main(["--json", "--limit", "10"], engine=env.eng) == 0
        out.append(json.loads(capsys.readouterr().out))
    assert (
        out[0]["invoiced"] == 1 and out[0]["waiting_usage"] == 1
    )  # periudha 1 pret raportin e shkurtit
    assert [o["invoiced"] for o in out[1:]] == [0, 0]
    with env.F() as s:
        assert s.scalar(select(func.count()).select_from(Invoice)) == 1
        assert s.scalar(select(func.count()).select_from(BillingPeriod)) == 1
    assert cli_run.main(["--limit", "0"], engine=env.eng) == 2
    assert sid


def test_currency_mismatch_and_missing_price_fail_closed(env):
    sid, _ = setup_billing(env, price="0.02", price_cur="USD")
    ingest(env, 0, T0 - timedelta(hours=1))
    ingest(env, 500, JAN_END + timedelta(minutes=1))
    r = run(env, sid)
    assert r.kind == "postponed" and r.reason == billing_overage.CURRENCY_MISMATCH
    with env.F() as s:
        assert s.scalar(select(func.count()).select_from(Invoice)) == 0
        assert billing.get_subscription(s, env.e1).next_period_index == 0


def test_enterprise_without_email_price_is_not_metered_and_needs_no_report(env):
    sid, _ = setup_billing(env, fee="10", included=0, with_price=False)
    r = run(env, sid, NOW)  # asnjë raport, asnjë çmim ⇒ vetëm tarifa mujore
    assert r.kind == "invoiced" and [x.line_type for x in lines(env, r.invoice.id)] == [
        "monthly_fee"
    ]
    with env.F() as s:
        p = s.scalar(select(BillingPeriod))
        assert p.usage_from is None and p.usage_to is None and p.usage_to_report_id is None


def test_missing_baseline_and_ambiguous_product_are_postponed(env):
    sid, _ = setup_billing(env, fee="0", included=0, price="1")
    ingest(
        env, 9, JAN_END + timedelta(minutes=1)
    )  # raport prerjeje, por asnjë baseline para fillimit
    r = run(env, sid)
    assert r.kind == "postponed" and r.reason == billing_overage.BASELINE_MISSING
    with env.F() as s:
        email2 = prod.create(s, "email2", "Email 2", "email")
        eprod.assign_product(s, env.e1, email2.id)
        s.commit()
    assert run(env, sid).reason == billing_overage.PRODUCT_AMBIGUOUS


def test_period_audit_is_system_and_has_no_pii(env):
    sid, _ = setup_billing(env, fee="1", included=0, price="1")
    ingest(env, 0, T0 - timedelta(hours=1))
    ingest(env, 3, JAN_END + timedelta(minutes=1))
    run(env, sid)
    with env.F() as s:
        rows = list(
            s.scalars(select(AuditLog).where(AuditLog.action == "billing.period_processed"))
        )
        assert len(rows) == 1
        d = json.dumps(rows[0].detail)
        assert '"usage"' in d and "@" not in d and "subject" not in d.lower()


def test_central_billing_never_calls_enterprise_synchronously(env, monkeypatch):
    import socket

    sid, _ = setup_billing(env, fee="1", included=0, price="1")
    ingest(env, 0, T0 - timedelta(hours=1))
    ingest(env, 2, JAN_END + timedelta(minutes=1))

    def boom(*a, **k):
        raise AssertionError("network call during billing")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    assert run(env, sid).kind == "invoiced"
    import inspect

    for mod in (billing, billing_overage, billing_usage):
        src = inspect.getsource(mod)
        assert (
            "httpx" not in src
            and "requests" not in src
            and "urllib" not in src
            and "import app" not in src
        )


# =============================================================================================================
# readiness
# =============================================================================================================


def test_billing_readiness_reports_waiting_postponed_and_stale_reports(env, capsys):
    sid, _ = setup_billing(env, fee="1", included=0, price="1")
    with env.F() as s:
        items = {c.name: c for c in billing_readiness.checks(s, NOW)}
        assert (
            items["billing_usage_reports_fresh"].level == "FAIL"
        )  # abonim i matur pa asnjë raport
        assert (
            items["billing_periods_not_stuck_waiting"].level == "FAIL"
        )  # periudha e mbyllur për >24h
    ingest(env, 0, NOW - timedelta(minutes=1))
    with env.F() as s:
        items = {c.name: c for c in billing_readiness.checks(s, NOW)}
        assert items["billing_usage_reports_fresh"].level == "PASS"
        assert items["billing_periods_not_stuck_waiting"].level == "PASS"
        assert items["billing_no_postponed_config"].level == "FAIL"  # baseline mungon
    assert cli_ready.main(["--json"], engine=env.eng) in (0, 1)
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] in ("PASS", "WARN", "FAIL") and "@" not in json.dumps(payload)
    assert sid


# =============================================================================================================
# PostgreSQL: konkurrencë
# =============================================================================================================


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_concurrent_ingest_and_concurrent_billing_runs_create_one_period_and_one_invoice(env):
    if env.eng.dialect.name != "postgresql":
        pytest.skip("needs PostgreSQL")
    sid, _ = setup_billing(env, fee="5", included=0, price="1")
    ingest(env, 0, T0 - timedelta(hours=1))
    ingest(env, 4, JAN_END + timedelta(minutes=1))
    results, errs = [], []
    barrier = threading.Barrier(4)

    def worker():
        try:
            barrier.wait()
            results.append(billing.run_due(env.eng, NOW - timedelta(days=30)))
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    with env.F() as s:
        assert s.scalar(select(func.count()).select_from(BillingPeriod)) == 1
        assert s.scalar(select(func.count()).select_from(Invoice)) == 1
        assert s.scalar(select(func.count()).select_from(CommercialPlan)) == 1
    # ingest paralel i të njëjtit report_id ⇒ një rresht
    r = report(env, 6, JAN_END + timedelta(hours=1))
    outs = []
    barrier2 = threading.Barrier(4)

    def ing():
        barrier2.wait()
        with env.F() as s:
            outs.append(billing_usage.ingest(s, r, now=NOW).created)
            s.commit()

    ts = [threading.Thread(target=ing) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(outs) == 1 and len(outs) == 4
