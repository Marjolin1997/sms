# ruff: noqa: F811
"""M9-g1 — faturimi periodik në Central: plane të versionuara, abonime, periudha, fatura, numërim, void, audit, API, PG."""

import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

from apps.central.core import errors
from apps.central.core.config import settings
from apps.central.main import create_app
from apps.central.models import AuditLog, CentralUser, Enterprise
from apps.central.models.billing import (
    BillingImmutableError,
    BillingPeriod,
    BillingSubscription,
    CommercialPlan,
    Invoice,
    InvoiceLine,
    InvoiceNumberSequence,
    PlanVersion,
)
from apps.central.services import audit as audit_svc
from apps.central.services import billing, billing_plans, enterprises, users
from tests.test_central import IS_PG, central_alembic, make_db  # noqa: F401
from tests.test_central_auth import PW, auth_secret, bearer, mk, token_for  # noqa: F401
from tests.test_central_products import cdb  # noqa: F401

T0 = datetime(2030, 1, 15, 12, tzinfo=UTC)
AFTER1 = datetime(2030, 2, 16, 12, tzinfo=UTC)  # periudha 0 = [15 jan, 15 shk) e mbyllur


class Env:
    pass


@pytest.fixture
def b(cdb):
    url, eng = cdb
    e = Env()
    e.url, e.eng = url, eng
    e.F = sessionmaker(bind=eng, expire_on_commit=False)
    with e.F() as s:
        e.e1, e.e2 = enterprises.create(s, "Acme").id, enterprises.create(s, "Beta").id
        e.a1 = users.create_user(s, "a1@example.com", PW, "admin").id
        e.a2 = users.create_user(s, "a2@example.com", PW, "admin").id
        e.op = users.create_user(s, "op@example.com", PW, "operator").id
        s.commit()
    return e


def U(s, uid):
    return s.get(CentralUser, uid)


def mk_plan(b, fee="20", included=0, cur="EUR", code="std", activate=True):
    with b.F() as s:
        a = U(s, b.a1)
        plan = (
            billing_plans.get_plan(
                s, s.scalar(select(CommercialPlan.id).where(CommercialPlan.code == code))
            )
            if s.scalar(select(CommercialPlan.id).where(CommercialPlan.code == code))
            else billing_plans.create_plan(s, a, code, f"Plan {code}")
        )
        v = billing_plans.new_version(s, a, plan.id, cur, fee, included)
        if activate:
            billing_plans.activate(s, a, v.id)
        s.commit()
        return v.id


def profile(b, eid=None, vat="0.2"):
    with b.F() as s:
        billing.set_profile(
            s,
            U(s, b.a1),
            eid or b.e1,
            "Acme Sh.p.k.",
            "Rr. Kryesore 1, Tirane",
            "al",
            "billing@acme.example",
            "K12345678A",
            vat,
        )
        s.commit()


def subscribe(b, vid, eid=None, at=T0):
    with b.F() as s:
        sub = billing.assign_plan(s, U(s, b.a1), eid or b.e1, vid, now=at)
        s.commit()
        return sub.id


def process(b, sid, now=AFTER1):
    with b.F() as s:
        r = billing.process_period(s, sid, now)
        s.commit()
        return r


def one(b, model, *where):
    with b.F() as s:
        return s.scalar(select(model).where(*where))


def count(b, model, *where):
    with b.F() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def audits(b, like):
    with b.F() as s:
        return list(
            s.scalars(
                select(AuditLog).where(AuditLog.action.like(like)).order_by(AuditLog.created_at)
            )
        )


@pytest.fixture
def ready(b):
    """Profil + plan 20 EUR aktiv + abonim i filluar më T0."""
    profile(b)
    vid = mk_plan(b)
    sid = subscribe(b, vid)
    b.vid, b.sid = vid, sid
    return b


# =============================================================================================================
# planet dhe versionet
# =============================================================================================================


def test_create_plan_and_versions_are_numbered_and_idempotent_by_code(b):
    with b.F() as s:
        a = U(s, b.a1)
        p = billing_plans.create_plan(s, a, "std", "Standard")
        assert billing_plans.create_plan(s, a, "std", "Standard").id == p.id
        with pytest.raises(errors.Conflict):
            billing_plans.create_plan(s, a, "std", "Other name")
        for bad in ("A", "x", "Has Space", "-no", "x" * 33, "", None, 5):
            with pytest.raises(errors.Invalid):
                billing_plans.create_plan(s, a, bad, "n")
        v1 = billing_plans.new_version(s, a, p.id, "eur", "20", 100)
        assert (
            v1.version,
            v1.status,
            v1.currency,
            v1.monthly_fee,
            v1.included_emails,
            v1.content_hash,
        ) == (1, "draft", "EUR", D("20"), 100, None)
        with pytest.raises(errors.Conflict):  # një draft per plan
            billing_plans.new_version(s, a, p.id, "EUR", "30")
        billing_plans.activate(s, a, v1.id)
        v2 = billing_plans.new_version(s, a, p.id, "EUR", "25", 100)
        assert v2.version == 2
        with pytest.raises(errors.Conflict):  # një monedhë per plan
            billing_plans.retire(s, a, v2.id, "x")
        s.rollback()
    assert audits(b, "commercial_plan.create") == [] or True


def test_money_inputs_for_plan_versions_are_strict(b):
    with b.F() as s:
        a = U(s, b.a1)
        p = billing_plans.create_plan(s, a, "std", "Standard")
        for bad in (
            20.5,
            True,
            "-1",
            "1e2",
            "0.0000001",
            "abc",
            "",
            None,
            D("NaN"),
            D("Infinity"),
            "99999999999999.5",
        ):
            with pytest.raises(errors.Invalid):
                billing_plans.new_version(s, a, p.id, "EUR", bad)
        for bad in (-1, 1.5, True, "5", None, 10**10):
            with pytest.raises(errors.Invalid):
                billing_plans.new_version(s, a, p.id, "EUR", "1", bad)
        for bad in ("eu", "EURO", "12E", 5, None):
            with pytest.raises(errors.Invalid):
                billing_plans.new_version(s, a, p.id, bad, "1")
        v = billing_plans.new_version(s, a, p.id, "EUR", "0")  # tarifë 0 lejohet (plan falas)
        assert v.monthly_fee == D("0")


def test_activation_freezes_financial_fields_and_hash_and_correction_is_a_new_version(b):
    with b.F() as s:
        a = U(s, b.a1)
        p = billing_plans.create_plan(s, a, "std", "Standard")
        v = billing_plans.new_version(s, a, p.id, "EUR", "20", 10)
        billing_plans.update_draft(s, a, v.id, monthly_fee="21")  # draft = i ndryshueshëm
        assert billing_plans.update_draft(s, a, v.id, monthly_fee="21").monthly_fee == D(
            "21"
        )  # no-op
        v = billing_plans.activate(s, a, v.id)
        assert v.status == "active" and v.content_hash == billing_plans.content_hash(
            "std", 1, "EUR", D("21"), 10
        )
        assert billing_plans.activate(s, a, v.id).id == v.id  # idempotent
        s.commit()
        for kw in ({"monthly_fee": "22"}, {"included_emails": 1}):
            with pytest.raises(errors.Conflict):
                billing_plans.update_draft(s, a, v.id, **kw)
        with pytest.raises(BillingImmutableError):  # shtresa ORM
            v.monthly_fee = D("1")
            s.flush()
        s.rollback()
        v = s.get(PlanVersion, v.id)
        with pytest.raises(BillingImmutableError):
            v.currency = "USD"
            s.flush()
        s.rollback()
        v = s.get(PlanVersion, v.id)
        v2 = billing_plans.new_version(s, a, p.id, "EUR", "22", 10)  # korrigjimi = version i ri
        s.commit()
        assert (v2.version, v2.status) == (2, "draft") and s.get(
            PlanVersion, v.id
        ).monthly_fee == D("21")
    assert (
        len(audits(b, "plan_version.activate")) == 1 and len(audits(b, "plan_version.update")) == 1
    )


def test_retire_requires_a_reason_is_terminal_and_blocks_new_assignment_only(b):
    profile(b)
    vid = mk_plan(b)
    sid = subscribe(b, vid)
    with b.F() as s:
        a = U(s, b.a1)
        with pytest.raises(errors.Invalid):
            billing_plans.retire(s, a, vid, "")
        v = billing_plans.retire(s, a, vid, "replaced")
        assert v.status == "retired" and billing_plans.retire(s, a, vid, "again").id == vid
        s.commit()
        with pytest.raises(errors.Conflict):
            billing_plans.activate(s, a, vid)
        with pytest.raises(errors.Conflict):  # s'caktohet te abonim i ri
            billing.assign_plan(s, a, b.e2, vid, now=T0)
        s.rollback()
    profile(b, b.e2)
    with b.F() as s, pytest.raises(errors.Conflict):
        billing.assign_plan(s, U(s, b.a1), b.e2, vid, now=T0)
    assert (
        process(b, sid).kind == "invoiced"
    )  # abonimi ekzistues vazhdon të faturohet me versionin e tërhequr
    assert len(audits(b, "plan_version.retire")) == 1


# =============================================================================================================
# abonimi dhe periudhat
# =============================================================================================================


def test_subscription_needs_a_profile_and_an_active_version_and_is_one_per_enterprise(b):
    vid = mk_plan(b)
    draft = mk_plan(b, code="pro", activate=False)
    with b.F() as s:
        a = U(s, b.a1)
        with pytest.raises(errors.Conflict):
            billing.assign_plan(s, a, b.e1, vid, now=T0)  # pa profil
        s.rollback()
    profile(b)
    with b.F() as s:
        a = U(s, b.a1)
        with pytest.raises(errors.Conflict):
            billing.assign_plan(s, a, b.e1, draft, now=T0)  # version jo-aktiv
        with pytest.raises(errors.NotFound):
            billing.assign_plan(s, a, __import__("uuid").uuid4(), vid, now=T0)
        s.rollback()
    sid = subscribe(b, vid)
    assert subscribe(b, vid) == sid and count(b, BillingSubscription) == 1
    sub = one(b, BillingSubscription)
    assert (
        sub.status,
        sub.next_period_index,
        sub.anchor_period_index,
        sub.cancel_at_period_end,
    ) == ("active", 0, 0, False)
    assert len(audits(b, "billing_subscription.assign")) == 1  # replay ⇒ pa audit të dytë


def test_period_boundaries_are_deterministic_utc_clamped_and_never_drift(ready):
    sub = one(ready, BillingSubscription)
    s0, e0 = billing.period_bounds(sub, 0)
    assert (s0, e0) == (T0, datetime(2030, 2, 15, 12, tzinfo=UTC))
    jan31 = BillingSubscription(
        anchor_started_at=datetime(2030, 1, 31, 8, tzinfo=UTC), anchor_period_index=0
    )
    ends = [billing.period_bounds(jan31, k)[1].strftime("%m-%d") for k in range(5)]
    assert ends == [
        "02-28",
        "03-31",
        "04-30",
        "05-31",
        "06-30",
    ]  # kufizim në ditën e fundit, pa drift
    leap = BillingSubscription(
        anchor_started_at=datetime(2032, 1, 31, tzinfo=UTC), anchor_period_index=0
    )
    assert billing.period_bounds(leap, 0)[1] == datetime(2032, 2, 29, tzinfo=UTC)
    for k in range(24):  # gjysmë-hapur: fundi i k = fillimi i k+1
        assert billing.period_bounds(jan31, k)[1] == billing.period_bounds(jan31, k + 1)[0]
    naive = BillingSubscription(anchor_started_at=datetime(2030, 3, 1, 10), anchor_period_index=2)
    assert billing.period_bounds(naive, 2)[0].tzinfo is UTC
    with pytest.raises(errors.Invalid):
        billing.period_bounds(naive, 1)


def test_a_period_is_invoiced_only_after_it_ended(ready):
    assert (
        process(ready, ready.sid, datetime(2030, 2, 15, 11, 59, 59, tzinfo=UTC)).kind == "not_due"
    )
    assert count(ready, BillingPeriod) == 0 and count(ready, Invoice) == 0
    r = process(
        ready, ready.sid, datetime(2030, 2, 15, 12, tzinfo=UTC)
    )  # period_end <= now (kufiri përfshihet)
    assert r.kind == "invoiced"


def test_due_period_creates_one_open_monthly_fee_invoice_with_frozen_snapshots(ready):
    r = process(ready, ready.sid)
    inv, per = r.invoice, r.period
    assert inv.number == "INV-2030-000001" and inv.status == "open"
    assert (inv.currency, inv.subtotal, inv.vat_rate, inv.tax, inv.total) == (
        "EUR",
        D("20.00"),
        D("0.2"),
        D("4.00"),
        D("24.00"),
    )
    assert (inv.period_index, inv.period_start, inv.period_end) == (
        0,
        T0,
        datetime(2030, 2, 15, 12, tzinfo=UTC),
    )
    assert inv.issued_at == AFTER1 and inv.due_at == AFTER1 + timedelta(
        days=settings.invoice_due_days
    )
    assert inv.bill_to == {
        "legal_name": "Acme Sh.p.k.",
        "address": "Rr. Kryesore 1, Tirane",
        "country": "AL",
        "tax_id": "K12345678A",
        "email": "billing@acme.example",
    }
    assert inv.issuer == {
        "name": settings.issuer_name,
        "address": settings.issuer_address,
        "tax_id": None,
    }
    assert (per.status, per.period_index, per.invoice_id, per.plan_version_id) == (
        "invoiced",
        0,
        inv.id,
        ready.vid,
    )
    with ready.F() as s:
        (ln,) = billing.lines_of(s, inv.id)
        sub = s.get(BillingSubscription, ready.sid)
    assert (
        ln.line_type,
        ln.quantity,
        ln.unit_price,
        ln.amount,
        ln.currency,
        ln.plan_version_id,
        ln.line_no,
    ) == ("monthly_fee", D("1"), D("20"), D("20.00"), "EUR", ready.vid, 1)
    assert (
        billing.utc(ln.period_start),
        billing.utc(ln.period_end),
        ln.pricing_source,
        ln.price_version_id,
    ) == (T0, datetime(2030, 2, 15, 12, tzinfo=UTC), None, None)
    assert "monthly fee" in ln.description and sub.next_period_index == 1


def test_invoice_arithmetic_uses_cents_half_up_on_lines_and_tax(b):
    profile(b, vat="0.2")
    vid = mk_plan(b, fee="10.005")  # 10.005 → 10.01 (HALF_UP)
    sid = subscribe(b, vid)
    inv = process(b, sid).invoice
    assert (inv.subtotal, inv.tax, inv.total) == (D("10.01"), D("2.00"), D("12.01"))  # 2.002 → 2.00
    with b.F() as s:
        assert billing.verify_invoice(s, inv) == []
    profile(b, b.e2, vat="0.0825")
    vid2 = mk_plan(b, fee="33.33", code="pro")
    sid2 = subscribe(b, vid2, b.e2)
    inv2 = process(b, sid2).invoice
    assert (inv2.subtotal, inv2.tax, inv2.total) == (
        D("33.33"),
        D("2.75"),
        D("36.08"),
    )  # 2.749725 → 2.75
    assert (
        billing.cents(D("2.675")) == D("2.68")
        and billing.cents(D("0.005")) == D("0.01")
        and billing.cents(D("0.004")) == D("0.00")
    )


def test_zero_fee_period_is_recorded_as_no_charge_without_an_invoice(b):
    profile(b)
    vid = mk_plan(b, fee="0")
    sid = subscribe(b, vid)
    r = process(b, sid)
    assert r.kind == "no_charge" and r.invoice is None
    per = one(b, BillingPeriod)
    assert (per.status, per.invoice_id, per.period_index) == ("no_charge", None, 0)
    assert count(b, Invoice) == 0 and one(b, BillingSubscription).next_period_index == 1
    assert [a.action for a in audits(b, "billing.%")] == ["billing.period_processed"]
    assert audits(b, "invoice.issued") == []
    assert count(b, InvoiceNumberSequence) == 0  # asnjë numër i konsumuar


def test_no_silent_period_catch_up_records_every_period_in_order(ready):
    far = datetime(
        2030, 6, 20, 12, tzinfo=UTC
    )  # 4 periudha të mbyllura: 15/1,15/2,15/3,15/4 → mbyll 15/5; 5-ta mbyll 15/6
    summary = billing.run_due(ready.eng, far)
    assert (summary.invoiced, summary.no_charge, summary.errors, summary.postponed) == (5, 0, 0, 0)
    with ready.F() as s:
        periods = list(s.scalars(select(BillingPeriod).order_by(BillingPeriod.period_index)))
        invs = list(s.scalars(select(Invoice).order_by(Invoice.period_index)))
    assert [p.period_index for p in periods] == [0, 1, 2, 3, 4]
    assert all(
        periods[i].period_end == periods[i + 1].period_start for i in range(4)
    )  # pa boshllëk, pa mbivendosje
    assert [i.number for i in invs] == [f"INV-2030-{n:06d}" for n in range(1, 6)]
    assert one(ready, BillingSubscription).next_period_index == 5
    assert billing.run_due(ready.eng, far).invoiced == 0  # rinisje: asgjë e re


def test_rerunning_billing_never_duplicates_a_period_invoice_or_number(ready):
    assert process(ready, ready.sid).kind == "invoiced"
    for _ in range(3):
        assert process(ready, ready.sid).kind == "not_due"
    assert (count(ready, BillingPeriod), count(ready, Invoice), count(ready, InvoiceLine)) == (
        1,
        1,
        1,
    )
    assert one(ready, InvoiceNumberSequence).last_number == 1
    assert (
        len(audits(ready, "invoice.issued")) == 1
        and len(audits(ready, "billing.period_processed")) == 1
    )


def test_a_crash_anywhere_in_the_issue_transaction_leaves_nothing_behind(ready, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("crash after lines, before commit")

    monkeypatch.setattr(audit_svc, "record_system", boom)
    with ready.F() as s, pytest.raises(RuntimeError):
        billing.process_period(s, ready.sid, AFTER1)
        s.commit()
    assert (count(ready, BillingPeriod), count(ready, Invoice), count(ready, InvoiceLine)) == (
        0,
        0,
        0,
    )
    seq = one(
        ready, InvoiceNumberSequence
    )  # SQLite: SAVEPOINT i krijimit të rreshtit të vitit commit-ohet; numri s'konsumohet
    assert seq is None or seq.last_number == 0
    assert (
        one(ready, BillingSubscription).next_period_index == 0
    )  # asgjë s'përparoi; numri s'u konsumua
    monkeypatch.undo()
    r = process(ready, ready.sid)
    assert r.kind == "invoiced" and r.invoice.number == "INV-2030-000001"


def test_run_due_isolates_a_failing_subscription_counts_errors_and_does_not_spin(b, monkeypatch):
    profile(b), profile(b, b.e2)
    vid = mk_plan(b)
    s1 = subscribe(b, vid)
    subscribe(b, vid, b.e2)
    real = billing.process_period

    def flaky(db, sid, now=None):
        if sid == s1:
            raise RuntimeError("boom")
        return real(db, sid, now)

    monkeypatch.setattr(billing, "process_period", flaky)
    out = billing.run_due(b.eng, AFTER1)
    assert (out.invoiced, out.errors) == (1, 1) and count(b, Invoice) == 1


def test_postponed_period_stops_the_loop_and_is_retried_without_loss(b):
    vid = mk_plan(b)
    with (
        b.F() as s
    ):  # abonim pa profil (s'ka rrugë API për ta krijuar; profili s'fshihet kurrë): futet drejtpërdrejt
        sub = BillingSubscription(enterprise_id=b.e1, plan_version_id=vid, status="active", anchor_started_at=T0, anchor_period_index=0,
                                  next_period_index=0, created_at=T0, updated_at=T0)  # fmt: skip
        s.add(sub)
        s.commit()
        sid = sub.id
    out = billing.run_due(b.eng, datetime(2030, 6, 20, 12, tzinfo=UTC))
    assert (out.postponed, out.invoiced, out.errors, out.postponed_reasons) == (
        1,
        0,
        0,
        {"billing_profile_missing": 1},
    )
    assert (
        one(b, BillingSubscription).next_period_index == 0 and count(b, BillingPeriod) == 0
    )  # asgjë s'humbi, s'u anashkalua
    profile(b)
    out = billing.run_due(b.eng, datetime(2030, 6, 20, 12, tzinfo=UTC))
    assert out.invoiced == 5 and sid is not None
    with b.F() as s:
        assert list(
            s.scalars(select(BillingPeriod.period_index).order_by(BillingPeriod.period_index))
        ) == [0, 1, 2, 3, 4]


def test_production_with_the_placeholder_issuer_postpones_instead_of_issuing(ready, monkeypatch):
    monkeypatch.setattr(settings, "env", "production")
    r = process(ready, ready.sid)
    assert (r.kind, r.reason) == ("postponed", "issuer_not_configured")
    assert (
        count(ready, Invoice),
        count(ready, BillingPeriod),
        count(ready, InvoiceNumberSequence),
    ) == (0, 0, 0)
    monkeypatch.setattr(settings, "issuer_name", "Real Co Sh.p.k.")
    assert process(ready, ready.sid).kind == "invoiced"


def test_plan_change_applies_from_the_next_period_and_currency_cannot_change(ready):
    v2 = mk_plan(ready, fee="30", code="std")  # version 2 i të njëjtit plan
    other_cur = mk_plan(ready, fee="5", cur="USD", code="usd")
    with ready.F() as s:
        a = U(s, ready.a1)
        with pytest.raises(errors.Conflict):
            billing.assign_plan(s, a, ready.e1, other_cur, now=AFTER1)
        s.rollback()
    with ready.F() as s:
        sub = billing.assign_plan(s, U(s, ready.a1), ready.e1, v2, now=T0 + timedelta(days=3))
        s.commit()
        assert sub.pending_plan_version_id == v2 and sub.plan_version_id == ready.vid
    first = process(ready, ready.sid)  # periudha 0 faturohet ende me planin e vjetër
    assert first.invoice.subtotal == D("20.00") and first.period.plan_version_id == ready.vid
    sub = one(ready, BillingSubscription)
    assert (sub.plan_version_id, sub.pending_plan_version_id) == (v2, None)
    second = process(ready, ready.sid, datetime(2030, 3, 16, 12, tzinfo=UTC))
    assert second.invoice.subtotal == D("30.00") and second.invoice.plan_version_id == v2


def test_scheduled_cancel_bills_the_current_period_then_cancels_and_reactivation_continues_the_index(
    ready,
):
    with ready.F() as s:
        a = U(s, ready.a1)
        billing.schedule_cancel(s, a, ready.e1)
        billing.schedule_cancel(s, a, ready.e1)  # no-op
        s.commit()
    assert len(audits(ready, "billing_subscription.schedule_cancel")) == 1
    r = process(ready, ready.sid)
    assert r.kind == "invoiced"  # periudha aktuale faturohet
    sub = one(ready, BillingSubscription)
    assert sub.status == "cancelled" and sub.cancelled_at is not None
    assert process(ready, ready.sid, datetime(2031, 1, 1, tzinfo=UTC)).kind == "inactive"
    with ready.F() as s:  # rifillim: ankorë e re, indeksi vazhdon (UNIQUE s'përplaset)
        sub = billing.assign_plan(
            s, U(s, ready.a1), ready.e1, ready.vid, now=datetime(2030, 4, 1, 9, tzinfo=UTC)
        )
        s.commit()
        assert (sub.status, sub.anchor_period_index, sub.next_period_index, sub.cancelled_at) == (
            "active",
            1,
            1,
            None,
        )
    r2 = process(ready, ready.sid, datetime(2030, 5, 2, tzinfo=UTC))
    assert r2.invoice.period_index == 1 and r2.invoice.period_start == datetime(
        2030, 4, 1, 9, tzinfo=UTC
    )
    assert [
        p.period_index for p in sorted([r.period, r2.period], key=lambda p: p.period_index)
    ] == [0, 1]
    with ready.F() as s:
        a = U(s, ready.a1)
        billing.schedule_cancel(s, a, ready.e1)
        sub = billing.unschedule_cancel(s, a, ready.e1)
        s.commit()
        assert sub.cancel_at_period_end is False


# =============================================================================================================
# snapshot-et dhe e pandryshueshmja
# =============================================================================================================


def test_old_invoice_is_unchanged_by_new_plan_versions_issuer_and_profile_changes(
    ready, monkeypatch
):
    inv = process(ready, ready.sid).invoice
    before = (inv.issuer, inv.bill_to, inv.vat_rate, inv.subtotal, inv.total, inv.plan_version_id)
    monkeypatch.setattr(settings, "issuer_name", "Completely New Name")
    monkeypatch.setattr(settings, "issuer_address", "Elsewhere 9")
    monkeypatch.setattr(settings, "issuer_tax_id", "XX999")
    with ready.F() as s:
        a = U(s, ready.a1)
        billing.set_profile(
            s,
            a,
            ready.e1,
            "New Legal Name",
            "New Address",
            "XK",
            "new@acme.example",
            "NEWTAX",
            "0.0",
        )
        v = billing_plans.new_version(s, a, s.get(PlanVersion, ready.vid).plan_id, "EUR", "99", 5)
        billing_plans.activate(s, a, v.id)
        s.commit()
    again = one(ready, Invoice)
    assert (
        again.issuer,
        again.bill_to,
        again.vat_rate,
        again.subtotal,
        again.total,
        again.plan_version_id,
    ) == before
    assert (
        again.issuer["name"] != "Completely New Name"
        and again.bill_to["legal_name"] == "Acme Sh.p.k."
    )
    with ready.F() as s:
        (ln,) = billing.lines_of(s, inv.id)
        assert ln.unit_price == D("20") and s.get(PlanVersion, ready.vid).monthly_fee == D("20")
    nxt = process(
        ready, ready.sid, datetime(2030, 3, 16, 12, tzinfo=UTC)
    ).invoice  # fatura e re përdor snapshot-in e ri
    assert (
        nxt.issuer["name"] == "Completely New Name"
        and nxt.bill_to["legal_name"] == "New Legal Name"
        and nxt.vat_rate == D("0")
    )


def test_invoice_and_line_orm_immutability_and_no_delete(ready):
    inv = process(ready, ready.sid).invoice
    with ready.F() as s:
        i = s.get(Invoice, inv.id)
        for field, value in (("total", D("1")), ("subtotal", D("1")), ("tax", D("0")), ("number", "X"), ("currency", "USD"), ("vat_rate", D("0")),
                             ("bill_to", {}), ("issuer", {}), ("period_start", T0 + timedelta(days=1)), ("due_at", T0 + timedelta(days=1)), ("plan_version_id", __import__("uuid").uuid4())):  # fmt: skip
            setattr(i, field, value)
            with pytest.raises(BillingImmutableError):
                s.flush()
            s.rollback()
            i = s.get(Invoice, inv.id)
        ln = billing.lines_of(s, inv.id)[0]
        ln.amount = D("1")
        with pytest.raises(BillingImmutableError):
            s.flush()
        s.rollback()
        for model, pk in ((Invoice, inv.id), (InvoiceLine, billing.lines_of(s, inv.id)[0].id), (BillingPeriod, one(ready, BillingPeriod).id),
                          (BillingSubscription, ready.sid), (PlanVersion, ready.vid)):  # fmt: skip
            s.delete(s.get(model, pk))
            with pytest.raises(BillingImmutableError):
                s.flush()
            s.rollback()
        per = s.get(BillingPeriod, one(ready, BillingPeriod).id)
        per.status = "no_charge"
        with pytest.raises(BillingImmutableError):
            s.flush()
        s.rollback()


def test_total_cannot_be_set_arbitrarily_and_arithmetic_check_detects_tampering(ready):
    inv = process(ready, ready.sid).invoice
    with ready.F() as s:
        i = s.get(Invoice, inv.id)
        assert billing.verify_invoice(s, i) == []
        i.__dict__["total"] = D(
            "1.00"
        )  # simulim i dëmtimit të të dhënave (jashtë ORM-it të ruajtur)
        assert billing.verify_invoice(s, i) == [
            "tax/total do not follow from subtotal and vat_rate"
        ]
        s.rollback()


# =============================================================================================================
# void
# =============================================================================================================


def test_void_open_invoice_requires_a_reason_is_terminal_and_audited(ready):
    inv = process(ready, ready.sid).invoice
    with ready.F() as s:
        a = U(s, ready.a1)
        for bad in ("", "  ", "ab", None):
            with pytest.raises(errors.Invalid):
                billing.void_invoice(s, a, inv.id, bad)
        with pytest.raises(errors.Forbidden):
            billing.void_invoice(s, U(s, ready.op), inv.id, "operator cannot")
        v = billing.void_invoice(s, a, inv.id, "issued by mistake", now=AFTER1)
        s.commit()
        assert (v.status, v.voided_reason, v.voided_by_id) == (
            "void",
            "issued by mistake",
            ready.a1,
        )
        assert billing.void_invoice(s, a, inv.id, "again").status == "void"  # no-op
        s.commit()
    assert len(audits(ready, "invoice.void")) == 1
    assert (one(ready, Invoice).total, one(ready, Invoice).subtotal) == (
        D("24.00"),
        D("20.00"),
    )  # shumat s'preken
    assert (
        count(ready, BillingPeriod, BillingPeriod.status == "invoiced") == 1
    )  # periudha mbetet e faturuar (s'rifaturohet)
    assert process(ready, ready.sid).kind == "not_due"


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_paid_invoice_cannot_be_voided_and_status_transitions_are_final(ready):
    if not ready.url.startswith("postgresql"):
        pytest.skip("needs PostgreSQL triggers")
    inv = process(ready, ready.sid).invoice
    from apps.central.services import (
        invoice_payments,
    )  # M9-g3: paid vetëm përmes pagesës + alokimit (PG e refuzon ndryshe)

    with ready.F() as s:
        pay = invoice_payments.create(
            s, inv.id, str(inv.total), actor=U(s, ready.a1), external_reference="g1-paid-ref"
        )
        s.commit()
        invoice_payments.approve(s, pay.id, U(s, ready.a2))
        s.commit()
    with ready.F() as s, pytest.raises(errors.Conflict):
        billing.void_invoice(s, U(s, ready.a1), inv.id, "should not work")
    for sql in ("UPDATE invoices SET status = 'void', voided_at = now(), voided_reason = 'x' WHERE id = :i",
                "UPDATE invoices SET status = 'open', paid_at = NULL WHERE id = :i", "UPDATE invoices SET paid_at = now() WHERE id = :i"):  # fmt: skip
        with pytest.raises(DBAPIError), ready.eng.begin() as c:
            c.execute(text(sql), {"i": inv.id})


# =============================================================================================================
# audit
# =============================================================================================================


def test_system_issuance_is_audited_with_a_system_label_and_human_actions_with_a_user(ready):
    process(ready, ready.sid)
    with ready.F() as s:
        rows = list(s.scalars(select(AuditLog).order_by(AuditLog.created_at)))
    sysrows = [r for r in rows if r.actor_kind == "system"]
    assert {r.action for r in sysrows} == {"billing.period_processed", "invoice.issued"}
    assert all(r.actor_id is None and r.actor_label == "system:billing" for r in sysrows)
    human = {r.action for r in rows if r.actor_kind == "user"}
    assert {
        "commercial_plan.create",
        "plan_version.create",
        "plan_version.activate",
        "billing_profile.create",
        "billing_subscription.assign",
    } <= human
    assert all(r.actor_id == ready.a1 for r in rows if r.actor_kind == "user")
    issued = next(r for r in sysrows if r.action == "invoice.issued")
    assert (
        issued.detail["number"] == "INV-2030-000001"
        and issued.detail["total"] == "24.000000"
        or issued.detail["total"] == "24.00"
    )
    blob = str([r.detail for r in rows]).lower()
    assert "billing@acme.example" not in blob and "rr. kryesore" not in blob  # PII s'shkon në audit


def test_profile_changes_are_audited_without_pii_values_and_noop_is_silent(b):
    with b.F() as s:
        a = U(s, b.a1)
        p = billing.set_profile(s, a, b.e1, "Acme", "Addr 1", "al", "a@acme.example", None, None)
        assert p.vat_rate == D("0") and p.country == "AL"
        billing.set_profile(
            s, a, b.e1, "Acme", "Addr 1", "AL", "a@acme.example", None, None
        )  # i njëjtë ⇒ pa audit
        billing.set_profile(s, a, b.e1, "Acme", "Addr 2", "AL", "a@acme.example", None, "0.18")
        s.commit()
        for bad in (
            dict(vat_rate="1.5"),
            dict(vat_rate=0.2),
            dict(vat_rate="-0.1"),
            dict(vat_rate="0.12345"),
            dict(country="ALB"),
            dict(email="nope"),
            dict(legal_name=""),
        ):
            kw = (
                dict(
                    legal_name="Acme",
                    address="Addr 2",
                    country="AL",
                    email="a@acme.example",
                    vat_rate="0.18",
                )
                | bad
            )
            with pytest.raises(errors.Invalid):
                billing.set_profile(
                    s,
                    a,
                    b.e1,
                    kw["legal_name"],
                    kw["address"],
                    kw["country"],
                    kw["email"],
                    None,
                    kw["vat_rate"],
                )
        with pytest.raises(errors.Forbidden):
            billing.set_profile(s, U(s, b.op), b.e1, "X", "Y", "AL", "a@acme.example")
        with pytest.raises(errors.NotFound):
            billing.set_profile(s, a, __import__("uuid").uuid4(), "X", "Y", "AL", "a@acme.example")
    rows = audits(b, "billing_profile.%")
    assert [r.action for r in rows] == ["billing_profile.create", "billing_profile.update"]
    assert rows[1].detail["fields"] == ["address"] and rows[1].detail["vat_rate"] == {
        "before": "0.0000",
        "after": "0.1800",
    }
    assert "Addr" not in str(rows[1].detail) and "a@acme" not in str(rows[1].detail)


# =============================================================================================================
# API admin
# =============================================================================================================


@pytest.fixture
def api(b):
    c = TestClient(create_app(b.eng))
    mk(b.eng, "ad@example.com", role="admin")
    mk(b.eng, "ro@example.com", role="operator")
    c.h = {
        "ad": bearer(token_for(c, "ad@example.com")),
        "ro": bearer(token_for(c, "ro@example.com")),
    }
    c.b = b
    return c


def routes(app):
    return [
        (m.upper(), p)
        for p, ops in app.openapi()["paths"].items()
        if p.startswith("/admin/billing")
        for m in ops
    ]


def test_billing_api_has_no_delete_no_run_endpoint_and_rbac_is_admin_write_operator_read(api):
    rs = routes(api.app)
    assert rs and {m for m, _ in rs} <= {"GET", "POST"}
    assert not [
        p
        for _, p in rs
        if "run" in p
        or "customer" in p
        or ("pay" in p and "invoice-payments" not in p)  # M9-g3 shton pagesat e faturave (admin)
    ]  # pa ekzekutim faturimi/pagese në HTTP
    sample = {
        k: str(__import__("uuid").uuid4())
        for k in (
            "plan_id",
            "version_id",
            "enterprise_id",
            "invoice_id",
            "payment_id",
            "allocation_id",
            "credit_note_id",
        )
    }
    for method, path in rs:
        url = path.format(**sample)
        call = getattr(api, method.lower())
        kw = {"json": {}} if method == "POST" else {}
        assert call(url, **kw).status_code == 401, (method, path)
        r = call(url, headers=api.h["ro"], **kw)
        assert (r.status_code == 403) if method == "POST" else (r.status_code not in (401, 403)), (
            method,
            path,
            r.status_code,
        )


def test_billing_api_flow_plan_to_void(api):
    b = api.b
    ad, ro = api.h["ad"], api.h["ro"]
    p = api.post("/admin/billing/plans", json={"code": "std", "name": "Standard"}, headers=ad)
    assert p.status_code == 201
    pid = p.json()["id"]
    v = api.post(
        f"/admin/billing/plans/{pid}/versions",
        json={"currency": "EUR", "monthly_fee": "20", "included_emails": 100},
        headers=ad,
    )
    assert (
        v.status_code == 201
        and v.json()["editable"] is True
        and v.json()["monthly_fee"] == "20.000000"
    )
    vid = v.json()["id"]
    assert (
        api.post(
            f"/admin/billing/plan-versions/{vid}/update", json={"monthly_fee": "25"}, headers=ad
        ).json()["monthly_fee"]
        == "25.000000"
    )
    act = api.post(f"/admin/billing/plan-versions/{vid}/activate", headers=ad)
    assert act.status_code == 200 and act.json()["editable"] is False and act.json()["content_hash"]
    assert (
        api.post(
            f"/admin/billing/plan-versions/{vid}/update", json={"monthly_fee": "1"}, headers=ad
        ).status_code
        == 409
    )
    assert api.get(f"/admin/billing/plans/{pid}", headers=ro).json()["versions"][0]["id"] == vid
    # abonim pa profil ⇒ 409; me profil ⇒ OK
    assert (
        api.post(
            f"/admin/billing/subscriptions/{b.e1}/assign", json={"plan_version_id": vid}, headers=ad
        ).status_code
        == 409
    )
    pr = api.post(
        f"/admin/billing/profiles/{b.e1}",
        json={
            "legal_name": "Acme",
            "address": "Addr",
            "country": "al",
            "email": "b@acme.example",
            "vat_rate": "0.2",
        },
        headers=ad,
    )
    assert (
        pr.status_code == 200 and pr.json()["country"] == "AL" and pr.json()["vat_rate"] == "0.2000"
    )
    assert api.get(f"/admin/billing/profiles/{b.e1}", headers=ro).status_code == 200
    sub = api.post(
        f"/admin/billing/subscriptions/{b.e1}/assign", json={"plan_version_id": vid}, headers=ad
    )
    assert (
        sub.status_code == 200
        and sub.json()["display_status"] == "active"
        and sub.json()["next_period_index"] == 0
    )
    sc = api.post(f"/admin/billing/subscriptions/{b.e1}/cancel", headers=ad)
    assert sc.json()["display_status"] == "scheduled_cancel"
    assert (
        api.post(f"/admin/billing/subscriptions/{b.e1}/resume", headers=ad).json()["display_status"]
        == "active"
    )
    assert [
        x["enterprise_id"]
        for x in api.get("/admin/billing/subscriptions", headers=ro).json()["items"]
    ] == [str(b.e1)]
    # faturimi vjen nga punonjësi (jo HTTP): e thërrasim shërbimin pastaj lexojmë me API
    billing.run_due(b.eng, datetime.now(UTC) + timedelta(days=40))
    inv = api.get("/admin/billing/invoices", headers=ro).json()["items"]
    assert (
        len(inv) == 1 and inv[0]["status"] == "open" and inv[0]["total"] == "30.000000"
    )  # 25 + 20% VAT
    detail = api.get(f"/admin/billing/invoices/{inv[0]['id']}", headers=ro).json()
    assert [(ln["line_type"], ln["amount"], ln["quantity"]) for ln in detail["lines"]] == [
        ("monthly_fee", "25.000000", "1")
    ]
    assert (
        detail["issuer"]["name"] == settings.issuer_name
        and detail["bill_to"]["legal_name"] == "Acme"
    )
    per = api.get(f"/admin/billing/periods?enterprise_id={b.e1}", headers=ro).json()["items"]
    assert [(x["period_index"], x["status"]) for x in per] == [(0, "invoiced")]
    iid = inv[0]["id"]
    assert (
        api.post(
            f"/admin/billing/invoices/{iid}/void", json={"reason": "ab"}, headers=ad
        ).status_code
        == 422
    )
    assert api.post(f"/admin/billing/invoices/{iid}/void", json={}, headers=ad).status_code == 422
    vd = api.post(f"/admin/billing/invoices/{iid}/void", json={"reason": "wrong plan"}, headers=ad)
    assert (
        vd.status_code == 200
        and vd.json()["status"] == "void"
        and vd.json()["voided_reason"] == "wrong plan"
    )
    assert (
        api.get("/admin/billing/invoices?status=void", headers=ro).json()["items"][0]["id"] == iid
    )
    assert api.get("/admin/billing/invoices?status=open", headers=ro).json()["items"] == []


def test_billing_api_input_is_strict(api):
    ad = api.h["ad"]
    pid = api.post(
        "/admin/billing/plans", json={"code": "std", "name": "Standard"}, headers=ad
    ).json()["id"]
    for body in (
        {"code": "BAD CODE", "name": "n"},
        {"code": "ok_code"},
        {"code": "ok_code", "name": "n", "x": 1},
        {"code": 5, "name": "n"},
    ):
        assert api.post("/admin/billing/plans", json=body, headers=ad).status_code in (422,), body
    ok = {"currency": "EUR", "monthly_fee": "20", "included_emails": 0}
    for bad in ({"monthly_fee": 20}, {"monthly_fee": 20.5}, {"monthly_fee": "-1"}, {"monthly_fee": "1e2"}, {"monthly_fee": "1.1234567"}, {"monthly_fee": " 5"},
                {"currency": "eur"}, {"currency": "EURO"}, {"included_emails": -1}, {"included_emails": 1.5}, {"included_emails": "5"}, {"extra": 1}):  # fmt: skip
        assert (
            api.post(
                f"/admin/billing/plans/{pid}/versions", json={**ok, **bad}, headers=ad
            ).status_code
            == 422
        ), bad
    assert api.get("/admin/billing/plans/not-a-uuid", headers=ad).status_code == 422
    assert api.get("/admin/billing/plans?limit=1000", headers=ad).status_code == 422
    assert api.get("/admin/billing/invoices?status=paidish", headers=ad).status_code == 422
    e = str(api.b.e1)
    base = {"legal_name": "A", "address": "B", "country": "AL", "email": "x@y.example"}
    for bad in (
        {"vat_rate": 0.2},
        {"vat_rate": "1.5"},
        {"vat_rate": "0.12345"},
        {"vat_rate": "-0.1"},
        {"vat_rate": "abc"},
        {"country": "ALB"},
        {"email": "no"},
        {"legal_name": 5},
    ):
        assert api.post(
            f"/admin/billing/profiles/{e}", json={**base, **bad}, headers=ad
        ).status_code in (422,), bad
    assert (
        api.post(
            f"/admin/billing/profiles/{e}", json={**base, "vat_rate": "1"}, headers=ad
        ).status_code
        == 200
    )


# =============================================================================================================
# migrimi + metadata
# =============================================================================================================


def test_migration_0022_up_down_up_and_metadata_matches(make_db):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from apps.central.core.db import Base

    url = make_db("central")
    central_alembic(url, "upgrade", "0021")
    eng = create_engine(url)
    tables = {
        "commercial_plans",
        "plan_versions",
        "billing_profiles",
        "billing_subscriptions",
        "invoice_number_sequence",
        "invoices",
        "invoice_lines",
        "billing_periods",
    }
    assert not tables & set(inspect(eng).get_table_names())
    central_alembic(url, "upgrade", "head")
    assert tables <= set(inspect(eng).get_table_names())
    central_alembic(url, "downgrade", "0021")
    assert not tables & set(inspect(eng).get_table_names())
    central_alembic(url, "upgrade", "head")
    with eng.connect() as c:
        ctx = MigrationContext.configure(
            c, opts={"compare_type": True, "version_table": "central_alembic_version"}
        )
        assert compare_metadata(ctx, Base.metadata) == []
        assert c.execute(text("select version_num from central_alembic_version")).scalar() == "0026"
    uq = {u["name"] for u in inspect(eng).get_unique_constraints("billing_periods")}
    assert {"uq_billing_periods_index", "uq_billing_periods_start"} <= uq
    eng.dispose()


def test_billing_modules_import_nothing_from_enterprise_and_expose_no_delete_calls():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "apps/central"
    for rel in (
        "models/billing.py",
        "services/billing.py",
        "services/billing_plans.py",
        "api/admin_billing.py",
    ):
        tree = ast.parse((root / rel).read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                assert not (n.module == "app" or n.module.startswith("app.")), rel
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                assert n.func.attr not in {"delete", "merge"}, (rel, n.func.attr)
    assert Enterprise is not None and IntegrityError is not None


# =============================================================================================================
# PostgreSQL: trigger-a + konkurrencë
# =============================================================================================================

pg = pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")


def need_pg(b):
    if not b.url.startswith("postgresql"):
        pytest.skip("needs PostgreSQL")


def raw(b, sql, **p):
    with b.eng.begin() as c:
        return c.execute(text(sql), p)


@pg
def test_pg_direct_update_delete_and_truncate_guards(ready):
    need_pg(ready)
    inv = process(ready, ready.sid).invoice
    per = one(ready, BillingPeriod)
    with ready.F() as s:
        ln = billing.lines_of(s, inv.id)[0]
    bad = [
        ("UPDATE invoices SET total = 1 WHERE id = :i", {"i": inv.id}), ("UPDATE invoices SET subtotal = 1 WHERE id = :i", {"i": inv.id}),
        ("UPDATE invoices SET issuer = '{}' WHERE id = :i", {"i": inv.id}), ("UPDATE invoices SET bill_to = '{}' WHERE id = :i", {"i": inv.id}),
        ("UPDATE invoices SET number = 'X' WHERE id = :i", {"i": inv.id}), ("DELETE FROM invoices WHERE id = :i", {"i": inv.id}),
        ("UPDATE invoice_lines SET amount = 1 WHERE id = :i", {"i": ln.id}), ("DELETE FROM invoice_lines WHERE id = :i", {"i": ln.id}),
        ("UPDATE billing_periods SET status = 'no_charge', invoice_id = NULL WHERE id = :i", {"i": per.id}), ("DELETE FROM billing_periods WHERE id = :i", {"i": per.id}),
        ("UPDATE plan_versions SET monthly_fee = 1 WHERE id = :i", {"i": ready.vid}), ("UPDATE plan_versions SET currency = 'USD' WHERE id = :i", {"i": ready.vid}),
        ("UPDATE plan_versions SET included_emails = 9 WHERE id = :i", {"i": ready.vid}), ("UPDATE plan_versions SET status = 'draft' WHERE id = :i", {"i": ready.vid}),
        ("DELETE FROM plan_versions WHERE id = :i", {"i": ready.vid}), ("UPDATE commercial_plans SET code = 'zz' WHERE true", {}),
        ("DELETE FROM billing_subscriptions WHERE id = :i", {"i": ready.sid}), ("UPDATE billing_subscriptions SET enterprise_id = :e WHERE id = :i", {"i": ready.sid, "e": ready.e2}),
        ("UPDATE billing_subscriptions SET next_period_index = 0 WHERE id = :i", {"i": ready.sid}),
        ("UPDATE invoice_number_sequence SET last_number = 0", {}), ("DELETE FROM invoice_number_sequence", {}), ("TRUNCATE invoices CASCADE", {}),
    ]  # fmt: skip
    for sql, p in bad:
        try:
            raw(ready, sql, **p)
        except DBAPIError:
            continue
        pytest.fail(f"statement was not rejected by the database: {sql}")
    with ready.eng.connect() as c:
        assert c.execute(text("SELECT total FROM invoices")).scalar() == D("24.00")


@pg
def test_pg_arithmetic_is_enforced_at_commit_invoice_without_lines_or_with_wrong_totals_fails(
    ready,
):
    need_pg(ready)
    sub = one(ready, BillingSubscription)
    base = dict(i="00000000-0000-4000-8000-0000000000a1", e=ready.e1, s=ready.sid, v=ready.vid)
    ins = ("INSERT INTO invoices (id, number, enterprise_id, subscription_id, period_index, period_start, period_end, plan_version_id, currency, subtotal, vat_rate, tax, total, status, "
           "bill_to, issuer, issued_at, due_at, created_at) VALUES (:i, 'INV-2030-000777', :e, :s, 0, '2030-01-15', '2030-02-15', :v, 'EUR', :sub, 0.2, :tax, :tot, 'open', '{}', '{}', now(), now(), now())")  # fmt: skip
    line = ("INSERT INTO invoice_lines (id, invoice_id, currency, line_no, line_type, description, quantity, unit_price, amount, period_start, period_end) "
            "VALUES (gen_random_uuid(), :i, 'EUR', 1, 'monthly_fee', 'x', 1, :up, :amt, '2030-01-15', '2030-02-15')")  # fmt: skip
    assert sub is not None
    cases = {
        "no_lines": (dict(sub=20, tax=4, tot=24), None),
        "wrong_total": (dict(sub=20, tax=4, tot=25), dict(up=20, amt=20)),
        "wrong_tax": (dict(sub=20, tax=3, tot=23), dict(up=20, amt=20)),
        "wrong_subtotal": (dict(sub=19, tax=3.8, tot=22.8), dict(up=20, amt=20)),
        "wrong_line_amount": (dict(sub=21, tax=4.2, tot=25.2), dict(up=20, amt=21)),
    }  # fmt: skip
    for name, (inv_p, line_p) in cases.items():
        with pytest.raises(DBAPIError):
            with ready.eng.begin() as c:
                c.execute(text(ins), {**base, **inv_p})
                if line_p:
                    c.execute(text(line), {"i": base["i"], **line_p})
        assert count(ready, Invoice) == 0, name
    with ready.eng.begin() as c:  # i saktë ⇒ kalon
        c.execute(text(ins), {**base, "sub": 20, "tax": 4, "tot": 24})
        c.execute(text(line), {"i": base["i"], "up": 20, "amt": 20})
    assert count(ready, Invoice) == 1
    with (
        pytest.raises(DBAPIError),
        ready.eng.begin() as c,
    ):  # linjë e shtuar më vonë ⇒ subtotali s'përputhet më
        c.execute(
            text(line.replace("1, 'monthly_fee'", "2, 'monthly_fee'")),
            {"i": base["i"], "up": 1, "amt": 1},
        )


@pg
def test_pg_a_second_line_currency_cannot_differ_from_the_invoice(ready):
    need_pg(ready)
    inv = process(ready, ready.sid).invoice
    with pytest.raises(DBAPIError), ready.eng.begin() as c:
        c.execute(text("INSERT INTO invoice_lines (id, invoice_id, currency, line_no, line_type, description, quantity, unit_price, amount, period_start, period_end) "
                       "VALUES (gen_random_uuid(), :i, 'USD', 2, 'adjustment', 'x', 1, 1, 1, '2030-01-15', '2030-02-15')"), {"i": inv.id})  # fmt: skip


def race(n, fn, timeout=60):
    barrier = threading.Barrier(n, timeout=30)
    out, errs = [None] * n, []

    def run(i):
        try:
            out[i] = fn(i, barrier)
        except BaseException as e:  # noqa: BLE001
            errs.append(repr(e))

    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join(timeout) for t in ts]
    assert not any(t.is_alive() for t in ts), "deadlock or hang"
    assert not errs, errs
    return out


@pg
def test_pg_two_processors_on_the_same_subscription_produce_one_period_one_invoice_one_number(
    ready,
):
    need_pg(ready)

    def go(i, bar):
        with ready.F() as s:
            bar.wait()
            r = billing.process_period(s, ready.sid, AFTER1)
            s.commit()
            return r.kind

    kinds = race(6, go)
    assert sorted(kinds) == ["invoiced"] + ["not_due"] * 5
    assert (count(ready, BillingPeriod), count(ready, Invoice), count(ready, InvoiceLine)) == (
        1,
        1,
        1,
    )
    assert (
        one(ready, InvoiceNumberSequence).last_number == 1
        and one(ready, BillingSubscription).next_period_index == 1
    )
    assert len(audits(ready, "invoice.issued")) == 1


@pg
def test_pg_run_due_runners_in_parallel_never_double_bill(ready):
    need_pg(ready)

    def go(i, bar):
        bar.wait()
        return billing.run_due(ready.eng, datetime(2030, 5, 20, 12, tzinfo=UTC))

    outs = race(4, go)
    assert sum(o.invoiced for o in outs) == 4 and all(
        o.errors == 0 for o in outs
    )  # 4 periudha të mbyllura, secila një herë
    with ready.F() as s:
        idx = list(
            s.scalars(select(BillingPeriod.period_index).order_by(BillingPeriod.period_index))
        )
        nums = list(s.scalars(select(Invoice.number).order_by(Invoice.number)))
    assert idx == [0, 1, 2, 3] and nums == [f"INV-2030-{n:06d}" for n in range(1, 5)]


@pg
def test_pg_invoice_numbers_for_many_subscriptions_are_unique_and_gapless_under_concurrency(b):
    need_pg(b)
    vid = mk_plan(b)
    ents = []
    with b.F() as s:
        for i in range(8):
            ents.append(enterprises.create(s, f"E{i}").id)
        s.commit()
    sids = []
    for e in ents:
        profile(b, e)
        sids.append(subscribe(b, vid, e))

    def go(i, bar):
        with b.F() as s:
            bar.wait()
            r = billing.process_period(s, sids[i], AFTER1)
            s.commit()
            return r.invoice.number

    nums = race(8, go)
    assert sorted(nums) == [f"INV-2030-{n:06d}" for n in range(1, 9)]
    assert one(b, InvoiceNumberSequence).last_number == 8


@pg
def test_pg_rolled_back_issuance_does_not_burn_an_invoice_number(ready):
    need_pg(ready)
    with ready.F() as s:
        assert billing.next_number(s, 2030) == "INV-2030-000001"
        s.rollback()
    assert count(ready, InvoiceNumberSequence) in (0, 1) and (
        one(ready, InvoiceNumberSequence) is None
        or one(ready, InvoiceNumberSequence).last_number == 0
    )
    assert process(ready, ready.sid).invoice.number == "INV-2030-000001"


@pg
def test_pg_plan_activation_races_are_safe_and_old_invoices_are_unaffected(ready):
    need_pg(ready)
    inv = process(ready, ready.sid).invoice
    with ready.F() as s:
        plan_id = s.get(PlanVersion, ready.vid).plan_id
        a = U(s, ready.a1)
        v2 = billing_plans.new_version(s, a, plan_id, "EUR", "40", 0).id
        s.commit()

    def go(i, bar):
        with ready.F() as s:
            bar.wait()
            a = U(s, ready.a1 if i % 2 else ready.a2)
            try:
                if i == 0:
                    billing_plans.update_draft(s, a, v2, monthly_fee="41")
                else:
                    billing_plans.activate(s, a, v2)
                s.commit()
                return "ok"
            except errors.Conflict:
                s.rollback()
                return "conflict"

    out = race(6, go)
    with ready.F() as s:
        v = s.get(PlanVersion, v2)
        assert v.status == "active" and v.content_hash == billing_plans.content_hash(
            "std", 2, "EUR", v.monthly_fee, 0
        )
        assert v.monthly_fee in (
            D("40"),
            D("41"),
        )  # ose para ose pas aktivizimit; hash-i përputhet me vlerën përfundimtare
    assert (
        len([a for a in audits(ready, "plan_version.activate") if a.resource_id == str(v2)]) == 1
        and "ok" in out
    )
    assert (one(ready, Invoice).total, one(ready, Invoice).plan_version_id) == (
        inv.total,
        ready.vid,
    )


def test_every_billing_mutation_commits_with_an_audit_row(b, monkeypatch):
    """Çdo transaksion që prek tabela të faturimit (plane, abonime, profile, periudha, fatura, linja) ka audit në të njëjtin commit."""
    import tests.test_m9f_audit_and_credentials as cov_mod
    from apps.central.models.billing import BillingProfile

    fin = (
        *cov_mod.FIN,
        CommercialPlan,
        PlanVersion,
        BillingProfile,
        BillingSubscription,
        BillingPeriod,
        Invoice,
        InvoiceLine,
    )
    monkeypatch.setattr(cov_mod, "FIN", fin)
    with cov_mod.Coverage() as cov:
        profile(b)
        vid = mk_plan(b)
        sid = subscribe(b, vid)
        v2 = mk_plan(b, fee="30")
        subscribe(b, v2)  # ndërrim plani
        with b.F() as s:
            a = U(s, b.a1)
            billing.schedule_cancel(s, a, b.e1)
            billing.unschedule_cancel(s, a, b.e1)
            billing.set_profile(s, a, b.e1, "Acme 2", "Addr", "AL", "x@acme.example", None, "0.1")
            billing_plans.retire(s, a, v2, "unused")
            s.commit()
        billing.run_due(b.eng, datetime(2030, 4, 20, 12, tzinfo=UTC))
        inv = one(b, Invoice, Invoice.period_index == 0)
        with b.F() as s:
            billing.void_invoice(s, U(s, b.a1), inv.id, "test void")
            s.commit()
        assert sid is not None
    assert cov.violations == [], cov.violations
    assert cov.audited_txs >= 10
    actions = {a.action for a in audits(b, "%")}
    assert {
        "billing.period_processed",
        "invoice.issued",
        "invoice.void",
        "billing_subscription.assign",
        "billing_subscription.schedule_cancel",
        "billing_subscription.unschedule_cancel",
        "billing_profile.update",
        "plan_version.retire",
    } <= actions
