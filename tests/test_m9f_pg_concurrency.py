# ruff: noqa: F811
"""M9-f — PostgreSQL: konkurrencë/ngarkesë e synuar mbi API-t admin financiare, wallet-in dhe raportet (pa performancë të plotë)."""

import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.central.main import create_app
from apps.central.models import CommercialLedgerEntry, CreditGrant, Payment, UsageReport
from apps.central.models.pricing import PriceAssignment, PriceVersion
from apps.central.services import credit_accounts as accts
from apps.central.services import enterprises, usage_reports
from apps.central.services import money_reconciliation as mr
from apps.central.services import products as prod_svc
from tests.test_central import IS_PG, make_db  # noqa: F401
from tests.test_central_auth import auth_secret, bearer, mk, token_for  # noqa: F401
from tests.test_central_products import cdb  # noqa: F401
from tests.test_m9d_central_reports import mk_doc

pg = pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
FUT = datetime.now(UTC) + timedelta(days=1)


def race(n, fn, timeout=60):
    barrier = threading.Barrier(n, timeout=30)
    out, errors = [None] * n, []

    def run(i):
        try:
            out[i] = fn(i, barrier)
        except BaseException as e:  # noqa: BLE001
            errors.append(repr(e))

    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join(timeout) for t in ts]
    assert not any(t.is_alive() for t in ts), "deadlock or hang"
    assert not errors, errors
    return out


@pytest.fixture
def api(cdb):
    url, eng = cdb
    if url.startswith("sqlite"):
        pytest.skip("needs PostgreSQL row locks")
    c = TestClient(create_app(eng))
    a1 = mk(eng, "a1@example.com", role="admin")
    mk(eng, "a2@example.com", role="admin")
    mk(eng, "a3@example.com", role="admin")
    mk(eng, "op@example.com", role="operator")
    with Session(eng, expire_on_commit=False) as s:
        ent = enterprises.create(s, "Acme").id
        sms = prod_svc.create(s, "sms", "SMS", "sms").id
        acct = accts.create(s, ent, sms, "EUR", s.get(type(a1), a1.id)).id
        s.commit()
    c.eng, c.ent, c.sms, c.acct = eng, ent, sms, acct
    c.h = {n: bearer(token_for(c, f"{n}@example.com")) for n in ("a1", "a2", "a3", "op")}
    return c


def client(api):
    """Klient i veçantë per thread (TestClient s'është i garantuar për përdorim paralel)."""
    return TestClient(create_app(api.eng))


def fund(api, amount="100"):
    p = api.post(
        "/admin/money/payments",
        json={"account_id": str(api.acct), "amount": amount},
        headers=api.h["a1"],
    ).json()["id"]
    assert api.post(f"/admin/money/payments/{p}/approve", headers=api.h["a2"]).status_code == 200


def n(api, model, *w):
    with Session(api.eng) as s:
        return s.scalar(select(func.count()).select_from(model).where(*w))


def avail(api):
    return D(
        api.get(f"/admin/money/accounts/{api.acct}", headers=api.h["op"]).json()["totals"][
            "available_to_grant"
        ]
    )


@pg
def test_concurrent_approvals_of_one_payment_credit_exactly_once(api):
    p = api.post(
        "/admin/money/payments",
        json={"account_id": str(api.acct), "amount": "40"},
        headers=api.h["a1"],
    ).json()["id"]

    def go(i, b):
        c = client(api)
        b.wait()
        return c.post(
            f"/admin/money/payments/{p}/approve", headers=api.h["a2" if i % 2 else "a3"]
        ).status_code

    assert set(race(8, go)) == {200}
    assert n(api, CommercialLedgerEntry) == 1 and avail(api) == D("40")


@pg
def test_concurrent_grants_never_overspend_and_idempotent_duplicates_collapse(api):
    fund(api, "100")

    def distinct(i, b):
        c = client(api)
        b.wait()
        return c.post(
            "/admin/money/grants",
            json={
                "account_id": str(api.acct),
                "amount": "15",
                "idempotency_key": f"grant-key-{i:04d}",
            },
            headers=api.h["a1"],
        ).status_code

    codes = race(12, distinct)
    assert codes.count(201) == 6 and codes.count(409) == 6  # 6 × 15 = 90 ≤ 100 < 7 × 15
    assert avail(api) == D("10") and n(api, CreditGrant) == 6

    def same(i, b):
        c = client(api)
        b.wait()
        return c.post(
            "/admin/money/grants",
            json={
                "account_id": str(api.acct),
                "amount": "10",
                "idempotency_key": "grant-same-0001",
            },
            headers=api.h["a1"],
        ).status_code

    assert set(race(8, same)) == {201} and n(api, CreditGrant) == 7 and avail(api) == D("0")


@pg
def test_concurrent_debit_adjustments_and_grants_never_make_funds_negative(api):
    fund(api, "50")

    def mix(i, b):
        c = client(api)
        b.wait()
        if i % 2:
            return c.post(
                f"/admin/money/accounts/{api.acct}/adjustments",
                json={
                    "kind": "debit",
                    "amount": "12",
                    "reason": "fix",
                    "idempotency_key": f"adj-key-{i:05d}",
                },
                headers=api.h["a1"],
            ).status_code
        return c.post(
            "/admin/money/grants",
            json={
                "account_id": str(api.acct),
                "amount": "12",
                "idempotency_key": f"grant-key-{i:04d}",
            },
            headers=api.h["a2"],
        ).status_code

    codes = race(10, mix)
    assert codes.count(201) == 4 and codes.count(409) == 6  # 4 × 12 = 48 ≤ 50 < 5 × 12
    assert D("0") <= avail(api) == D("2")


@pg
def test_concurrent_creation_with_the_same_external_reference_yields_one_payment(api):
    def go(i, b):
        c = client(api)
        b.wait()
        r = c.post(
            "/admin/money/payments",
            json={
                "account_id": str(api.acct),
                "amount": "9",
                "source": "bank",
                "external_reference": "wire-0001",
            },
            headers=api.h["a1" if i % 2 else "a2"],
        )
        return r.status_code, r.json()["id"]

    out = race(8, go)
    assert {c for c, _ in out} == {201} and len({i for _, i in out}) == 1 and n(api, Payment) == 1


@pg
def test_concurrent_reversals_and_new_grants_keep_the_equation_exact(api):
    fund(api, "100")
    gids = [
        api.post(
            "/admin/money/grants",
            json={
                "account_id": str(api.acct),
                "amount": "10",
                "idempotency_key": f"grant-seed-{i:03d}",
            },
            headers=api.h["a1"],
        ).json()["id"]
        for i in range(5)
    ]

    def go(i, b):
        c = client(api)
        b.wait()
        if i < 5:
            return c.post(
                f"/admin/money/grants/{gids[i]}/reverse",
                json={"reason": "refund"},
                headers=api.h["a1"],
            ).status_code
        return c.post(
            "/admin/money/grants",
            json={
                "account_id": str(api.acct),
                "amount": "10",
                "idempotency_key": f"grant-new-{i:04d}",
            },
            headers=api.h["a2"],
        ).status_code

    codes = race(10, go)
    assert all(c in (200, 201, 409) for c in codes)
    t = api.get(f"/admin/money/accounts/{api.acct}", headers=api.h["op"]).json()["totals"]
    funds, outstanding, free = D(t["funds"]), D(t["outstanding_grants"]), D(t["available_to_grant"])
    assert funds == D("100") and free == funds - outstanding and D("0") <= free <= D("100")
    assert D(t["grants_issued"]) - D(t["grants_reversed"]) == outstanding


@pg
def test_reconciliation_and_overview_reads_during_writes_never_fail_or_see_torn_state(api):
    fund(api, "1000")
    stop = threading.Event()
    bad = []

    def writer():
        c = client(api)
        for i in range(25):
            c.post(
                "/admin/money/grants",
                json={
                    "account_id": str(api.acct),
                    "amount": "1",
                    "idempotency_key": f"grant-load-{i:04d}",
                },
                headers=api.h["a1"],
            )
        stop.set()

    def reader():
        c = client(api)
        while not stop.is_set():
            for path in (
                "/admin/money/reconciliation",
                "/admin/financial/overview",
                "/admin/financial/readiness",
                f"/admin/money/accounts/{api.acct}",
            ):
                r = c.get(path, headers=api.h["op"])
                if r.status_code != 200:
                    bad.append((path, r.status_code, r.text[:100]))
            t = c.get(f"/admin/money/accounts/{api.acct}", headers=api.h["op"]).json()["totals"]
            if D(t["available_to_grant"]) != D(t["funds"]) - D(t["outstanding_grants"]):
                bad.append(("torn", t))

    ts = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(3)]
    [t.start() for t in ts]
    [t.join(90) for t in ts]
    assert not any(t.is_alive() for t in ts) and bad == []
    assert n(api, CreditGrant) == 25


@pg
def test_concurrent_pricing_drafts_activations_and_assignments_stay_consistent(api):
    bid = api.post(
        "/admin/pricing/books",
        json={"code": "retail", "name": "Retail", "currency": "EUR"},
        headers=api.h["a1"],
    ).json()["id"]

    def drafts(i, b):
        c = client(api)
        b.wait()
        return c.post(f"/admin/pricing/books/{bid}/versions", headers=api.h["a1"]).status_code

    codes = race(6, drafts)
    assert codes.count(201) == 1 and codes.count(409) == 5
    with Session(api.eng) as s:
        vid = str(s.scalar(select(PriceVersion.id)))
    api.post(
        f"/admin/pricing/versions/{vid}/rules",
        json={"channel": "sms", "prefix": "355", "unit_price": "0.05"},
        headers=api.h["a1"],
    )

    def activate(i, b):
        c = client(api)
        b.wait()
        return c.post(
            f"/admin/pricing/versions/{vid}/activate",
            json={"effective_from": FUT.isoformat()},
            headers=api.h["a1" if i % 2 else "a2"],
        ).status_code

    assert set(race(6, activate)) == {200}
    with Session(api.eng) as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(PriceVersion)
                .where(PriceVersion.status == "active")
            )
            == 1
        )

    def assign(i, b):
        c = client(api)
        b.wait()
        return c.post(
            "/admin/pricing/assignments",
            json={
                "enterprise_id": str(api.ent),
                "product_id": str(api.sms),
                "book_id": bid,
                "effective_from": FUT.isoformat(),
            },
            headers=api.h["a1"],
        ).status_code

    assert set(race(6, assign)) == {201} and n(api, PriceAssignment) == 1

    # mutacion paralel mbi version aktiv: asnjë ndryshim i përbashkët i çmimit
    def edit(i, b):
        c = client(api)
        b.wait()
        return c.post(
            f"/admin/pricing/versions/{vid}/rules",
            json={"channel": "sms", "prefix": "355", "unit_price": f"0.0{i + 1}"},
            headers=api.h["a1"],
        ).status_code

    assert set(race(5, edit)) == {409}
    assert (
        api.get(f"/admin/pricing/versions/{vid}", headers=api.h["op"]).json()["rules"][0][
            "unit_price"
        ]
        == "0.050000"
    )


@pg
def test_report_ingestion_retention_and_reconciliation_run_concurrently_without_losing_the_latest(
    api,
):
    from apps.central.services import retention

    def doc(seq):
        return mk_doc(api.ent, api.sms, seq=seq, ledger_max_id=10 + seq)

    def ingest(i, b):
        with Session(api.eng) as s:
            b.wait()
            try:
                usage_reports.ingest(
                    s,
                    usage_reports.parse(doc(i + 1)),
                    now=datetime.now(UTC) - timedelta(days=40 - i),
                )
                s.commit()
                return "ok"
            except Exception as e:  # noqa: BLE001  (konflikt watermark/seq i pritshëm)
                s.rollback()
                return type(e).__name__

    out = race(8, ingest)
    assert set(out) <= {"ok", "Conflict"} and out.count("ok") >= 1

    def mixed(i, b):
        with Session(api.eng) as s:
            b.wait()
            if i == 0:
                retention.apply(s, retention.plan(s, retention_days=10, full_days=1, keep_last=1))
                s.commit()
                return "retention"
            res = mr.reconcile(s)
            s.rollback()
            return res.status

    res = race(5, mixed)
    assert res[0] == "retention" and all(x in ("PASS", "WARN", "FAIL", "CRITICAL") for x in res[1:])
    with Session(api.eng) as s:
        latest = usage_reports.latest_per_key(s)
        assert len(latest) == 1 and latest[0].report_seq == s.scalar(
            select(func.max(UsageReport.report_seq))
        )


# --- Enterprise: reserve/capture/release nën ngarkesë ----------------------------------------------------------------------


@pg
def test_wallet_reservations_never_overdraw_and_captures_release_exactly(db):
    from app.core.db import SessionLocal
    from app.models.wallet import Hold, HoldStatus, TopupMethod
    from app.services import wallet as wallets
    from app.services.wallet import InsufficientFunds

    w = wallets.create_wallet(db, "load", "EUR")
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "1.00", TopupMethod.CASH).id)
    db.commit()

    def reserve(i, b):
        with SessionLocal() as s:
            b.wait()
            try:
                wallets.reserve(s, w.id, "0.10", f"load-{i}")
                s.commit()
                return "ok"
            except InsufficientFunds:
                s.rollback()
                return "no_funds"

    out = race(20, reserve)
    assert out.count("ok") == 10 and out.count("no_funds") == 10
    assert wallets.balances(db, w.id) == (D("0.00"), D("1.00"))
    holds = list(db.scalars(select(Hold).where(Hold.wallet_id == w.id)))

    def settle(i, b):
        with SessionLocal() as s:
            b.wait()
            if i % 2:
                wallets.capture(s, holds[i].id)
            else:
                wallets.release(s, holds[i].id)
            s.commit()
            return i

    race(10, settle)
    db.expire_all()
    avail_, held = wallets.balances(db, w.id)
    captured = sum(
        h.captured_amount for h in db.scalars(select(Hold).where(Hold.wallet_id == w.id))
    )
    assert held == D("0") and avail_ + captured == D("1.00") and captured == D("0.50")
    assert all(
        h.status in (HoldStatus.CAPTURED, HoldStatus.RELEASED)
        for h in db.scalars(select(Hold).where(Hold.wallet_id == w.id))
    )
