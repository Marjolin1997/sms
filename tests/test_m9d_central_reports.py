# ruff: noqa: F811
"""M9-d — Central: marrja e raporteve (auth `money:report`, idempotencë, konflikt, jashtë-rendit, watermark,
immutability, Decimal), endpoint-i i rakordimit, migrimi, PG konkurrencë."""

import copy
import threading
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from apps.central.main import create_app
from apps.central.models import UsageReport
from apps.central.models.money import MoneyImmutableError
from apps.central.services import enterprises as ent
from apps.central.services import products as prod
from apps.central.services import service_auth, usage_reports
from tests.test_central import IS_PG, central_alembic, make_db  # noqa: F401
from tests.test_central_auth import auth_secret  # noqa: F401
from tests.test_central_sync_api import assertion, auth, keypair

T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)
U = lambda n: str(uuid.UUID(int=n))  # noqa: E731
REF = "ab" * 32


def mk_doc(eid, pid, *, seq=1, rid=None, cur="EUR", mode="central", gross="100.000000", held="0.000000",
           grants=(), baseline=None, ledger_max_id=10, cursor_seq=5, epoch=None, flows=None, hold_total=None,
           generated="2030-01-01T12:00:00.000000+00:00", last_success="2030-01-01T11:59:00.000000+00:00"):  # fmt: skip
    from decimal import Decimal as D

    avail = format(D(gross) - D(held), "f")
    f = {"baseline_gross": "0.000000", "grants_applied": gross, "grant_reversals": "0.000000", "captured": "0.000000",
         "negative_adjustments": "0.000000", "invoice_debits": "0.000000", "other_debits": "0.000000",
         "positive_local_credit": "0.000000", "released": "0.000000", **(flows or {})}  # fmt: skip
    return {
        "schema": "cp.money.usage.v1", "report_id": str(rid or uuid.uuid4()), "report_seq": seq,
        "enterprise_id": str(eid), "product_id": str(pid), "currency": cur, "generated_at": generated,
        "authority_mode": mode, "ledger_max_id": ledger_max_id,
        "wallet": {"available": avail, "held": held, "gross": gross, "active_hold_total": hold_total or held,
                   "active_hold_count": 0 if D(held) == 0 else 1},
        "baseline": baseline, "flows": f,
        "integrity": {"ledger_sum_available": avail, "ledger_sum_held": held, "orphan_grant_credit": "0.000000",
                      "orphan_grant_reversal": "0.000000"},
        "cursor": {"epoch": epoch, "last_seq": cursor_seq, "generation": 1, "last_success_at": last_success,
                   "has_error": False},
        "grants": list(grants),
    }  # fmt: skip


@pytest.fixture
def env(make_db):
    url = make_db()
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    private, public = keypair()
    with Session(eng, expire_on_commit=False) as s:
        e1, e2, e3 = ent.create(s, "Acme"), ent.create(s, "Beta"), ent.create(s, "Gamma")
        sms = prod.create(s, "sms", "SMS", "sms")
        for cid, scopes, ents in (("rep", ["money:report"], [e1.id, e2.id]), ("rep1", ["money:report"], [e1.id]),
                                  ("mon", ["money:read"], [e1.id]), ("syn", ["sync:read"], [e1.id])):  # fmt: skip
            service_auth.create_client(s, cid, scopes, ents)
            service_auth.add_key(s, cid, "k1", public)
        s.commit()
        ids = dict(e1=e1.id, e2=e2.id, e3=e3.id, sms=sms.id)
    c = TestClient(create_app(eng))
    c.eng, c.private, c.ids = eng, private, ids
    yield c
    eng.dispose()


def tok(env, client="rep", scope="money:report"):
    return assertion(env.private, client=client, kid="k1", scope=scope)


def post(env, doc, client="rep", scope="money:report", token=None):
    return env.post(
        "/internal/money/usage-reports", json=doc, headers=auth(token or tok(env, client, scope))
    )


def rows(env):
    with Session(env.eng) as s:
        return list(s.scalars(select(UsageReport).order_by(UsageReport.report_seq)))


def doc(env, **kw):
    return mk_doc(env.ids["e1"], env.ids["sms"], **kw)


# --- auth ---------------------------------------------------------------------------------------------------


def test_report_is_stored_with_extracted_columns_and_exact_payload(env):
    d = doc(env, gross="12.500000", held="2.000000", ledger_max_id=77, cursor_seq=9)
    r = post(env, d)
    assert r.status_code == 201, r.text
    assert r.json() == {
        "status": "stored",
        "report_id": d["report_id"],
        "report_seq": 1,
        "latest": True,
    }
    (row,) = rows(env)
    assert (str(row.enterprise_id), row.currency, row.report_seq, row.ledger_max_id, row.money_cursor_seq) == (
        d["enterprise_id"], "EUR", 1, 77, 9)  # fmt: skip
    assert (
        str(row.gross) == "12.500000"
        and str(row.held) == "2.000000"
        and str(row.available) == "10.500000"
    )
    assert (
        row.payload == d
        and row.received_at is not None
        and row.schema_version == "cp.money.usage.v1"
    )


def test_scope_is_money_report_only_and_other_scopes_are_denied(env):
    d = doc(env)
    assert post(env, d, client="mon", scope="money:read").status_code == 403
    assert post(env, d, client="syn", scope="sync:read").status_code == 403
    assert (
        post(env, d, client="mon", scope="money:report").status_code == 403
    )  # klienti s'e ka scope-in
    assert env.post("/internal/money/usage-reports", json=d).status_code == 401
    assert (
        env.post("/internal/money/usage-reports", json=d, headers=auth("garbage")).status_code
        == 401
    )
    assert rows(env) == []
    # money:report s'jep qasje në feed-in money:read
    ep = env.get("/internal/money/state", headers=auth(tok(env, "rep", "money:read")))
    assert ep.status_code == 403


def test_a_client_cannot_report_for_an_enterprise_it_is_not_authorized_for(env):
    assert (
        post(env, mk_doc(env.ids["e3"], env.ids["sms"])).status_code == 403
    )  # e3 s'është e autorizuar
    assert post(env, mk_doc(env.ids["e2"], env.ids["sms"]), client="rep1").status_code == 403
    assert post(env, mk_doc(env.ids["e2"], env.ids["sms"]), client="rep").status_code == 201
    assert len(rows(env)) == 1


def test_unknown_product_and_unknown_enterprise_are_rejected(env):
    assert post(env, mk_doc(env.ids["e1"], U(0xDEAD))).status_code == 422
    assert post(env, mk_doc(U(0xBEEF), env.ids["sms"])).status_code == 403
    assert rows(env) == []


# --- idempotencë / konflikt / rend --------------------------------------------------------------------------


def test_same_report_twice_is_a_noop_and_different_payload_conflicts(env):
    d = doc(env)
    assert post(env, d).status_code == 201
    r2 = post(env, d)
    assert r2.status_code == 200 and r2.json()["status"] == "duplicate" and len(rows(env)) == 1
    changed = copy.deepcopy(d)
    changed["wallet"]["gross"] = "101.000000"
    changed["wallet"]["available"] = "101.000000"
    assert post(env, changed).status_code == 409
    assert rows(env)[0].payload == d  # historia s'u rishkrua


def test_same_seq_with_a_different_report_id_conflicts(env):
    assert post(env, doc(env, seq=1)).status_code == 201
    r = post(env, doc(env, seq=1))
    assert r.status_code == 409 and len(rows(env)) == 1


def test_an_older_report_arriving_late_is_stored_but_never_becomes_current(env):
    new, old = (
        doc(env, seq=5, ledger_max_id=50, gross="200.000000"),
        doc(env, seq=3, ledger_max_id=30, gross="100.000000"),
    )
    assert post(env, new).json()["latest"] is True
    r = post(env, old)
    assert r.status_code == 201 and r.json()["latest"] is False
    assert [x.report_seq for x in rows(env)] == [3, 5]
    with Session(env.eng) as s:
        top = usage_reports.latest(s, env.ids["e1"], env.ids["sms"], "EUR")
        assert top.report_seq == 5 and str(top.gross) == "200.000000"
        assert [
            h.report_seq for h in usage_reports.history(s, env.ids["e1"], env.ids["sms"], "EUR")
        ] == [5, 3]
        assert len(usage_reports.latest_per_key(s, env.ids["e1"])) == 1


def test_ledger_watermark_must_be_monotone_with_report_seq(env):
    assert post(env, doc(env, seq=2, ledger_max_id=20)).status_code == 201
    assert (
        post(env, doc(env, seq=3, ledger_max_id=19)).status_code == 409
    )  # seq më i lartë, watermark më i ulët
    assert (
        post(env, doc(env, seq=1, ledger_max_id=21)).status_code == 409
    )  # seq më i ulët, watermark më i lartë
    assert post(env, doc(env, seq=3, ledger_max_id=20)).status_code == 201  # e barabartë lejohet
    assert [x.report_seq for x in rows(env)] == [2, 3]


def test_watermark_is_tracked_per_key_not_across_currencies(env):
    assert post(env, doc(env, seq=1, ledger_max_id=100, cur="EUR")).status_code == 201
    assert post(env, doc(env, seq=1, ledger_max_id=5, cur="USD")).status_code == 201


# --- validim ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=1),
        lambda d: d.pop("grants"),
        lambda d: d.update(schema="cp.money.usage.v2"),
        lambda d: d.update(report_id="not-a-uuid"),
        lambda d: d.update(report_seq=0),
        lambda d: d.update(currency="eur"),
        lambda d: d.update(authority_mode="remote"),
        lambda d: d["wallet"].update(gross="1.000000"),  # gross ≠ available + held
        lambda d: d["wallet"].update(available=100.0),  # float
        lambda d: d["flows"].update(captured="-1.000000"),  # total negativ
        lambda d: d["flows"].update(captured="1.5"),  # jo 6 decimale
        lambda d: d["cursor"].update(epoch="x"),
        lambda d: d["wallet"].update(extra="1.000000"),
        lambda d: d.update(generated_at="2030-01-01 12:00:00"),
        lambda d: d.update(grants=[{"grant_id": U(1)}]),
    ],
)
def test_malformed_reports_are_rejected_with_422_and_store_nothing(env, mutate):
    d = doc(env)
    mutate(d)
    assert post(env, d).status_code == 422
    assert rows(env) == []


def test_non_object_and_oversized_bodies_are_rejected(env):
    assert (
        env.post(
            "/internal/money/usage-reports",
            content=b"[]",
            headers={**auth(tok(env)), "content-type": "application/json"},
        ).status_code
        == 422
    )
    big = doc(env, grants=[
        {"grant_id": str(uuid.UUID(int=i + 1)), "status": "applied", "amount": "1.000000", "currency": "EUR",
         "product_id": str(env.ids["sms"]), "purpose": "standard", "baseline_ref": None, "issued_seq": i + 1,
         "reversed_seq": None, "updated_at": "2030-01-01T12:00:00.000000+00:00", "detail": None}
        for i in range(5001)])  # fmt: skip
    assert post(env, big).status_code == 422


def test_decimal_extremes_are_stored_exactly(env):
    # NUMERIC(20,6) është i saktë në PostgreSQL; SQLite (vetëm dev) ruan float ⇒ ekstremi i plotë vetëm në PG
    from decimal import Decimal as D

    big = "99999999999999.999999" if IS_PG else "12345.678901"
    small_avail = format(D(big) - D("0.000001"), "f")
    d = doc(env, gross=big, held="0.000001")
    d["wallet"]["available"] = small_avail
    d["integrity"]["ledger_sum_available"], d["integrity"]["ledger_sum_held"] = (
        small_avail,
        "0.000001",
    )
    d["flows"]["grants_applied"] = big
    d["wallet"]["active_hold_total"] = "0.000001"
    assert post(env, d).status_code == 201
    (row,) = rows(env)
    assert str(row.gross) == big and str(row.held) == "0.000001"
    assert (
        row.payload["wallet"]["available"] == small_avail
    )  # payload kanonik (string) gjithmonë i saktë


def test_negative_balances_are_accepted_so_that_they_can_be_flagged_critical(env):
    d = doc(env, gross="-1.000000", held="0.000000", flows={"grants_applied": "-1.000000"})
    d["flows"]["grants_applied"] = "0.000000"  # totals ≥ 0 ; balanca negative
    assert post(env, d).status_code == 201


# --- immutability -------------------------------------------------------------------------------------------


def test_stored_reports_are_immutable_orm_and_database(env):
    d = doc(env)
    post(env, d)
    with Session(env.eng) as s:
        row = s.scalar(select(UsageReport))
        row.gross = 1
        with pytest.raises(MoneyImmutableError):
            s.flush()
        s.rollback()
        row = s.scalar(select(UsageReport))
        s.delete(row)
        with pytest.raises(MoneyImmutableError):
            s.flush()
        s.rollback()
    if IS_PG:
        for stmt in (
            "UPDATE usage_reports SET ledger_max_id = 1",
            "DELETE FROM usage_reports",
            "TRUNCATE usage_reports",
        ):
            with pytest.raises(DBAPIError), env.eng.begin() as c:
                c.execute(text(stmt))
    assert rows(env)[0].payload == d


def test_report_ingestion_has_no_financial_effect_on_central_money_tables(env):
    from apps.central.models import CommercialLedgerEntry, CreditGrant, MoneyEvent

    def counts():
        with Session(env.eng) as s:
            return [
                s.scalar(select(func.count()).select_from(m))
                for m in (CommercialLedgerEntry, CreditGrant, MoneyEvent)
            ]

    before = counts()
    post(env, doc(env))
    assert counts() == before


# --- indekset, migrimi, endpoint-i i rakordimit ------------------------------------------------------------------


def test_indexes_for_enterprise_product_currency_time_and_watermark_exist(env):
    ix = {i["name"]: i["column_names"] for i in inspect(env.eng).get_indexes("usage_reports")}
    assert ix["ix_usage_reports_enterprise_generated"] == ["enterprise_id", "generated_at"]
    assert ix["ix_usage_reports_key_ledger"] == [
        "enterprise_id",
        "product_id",
        "currency",
        "ledger_max_id",
    ]
    uq = {
        c["name"]: c["column_names"]
        for c in inspect(env.eng).get_unique_constraints("usage_reports")
    }
    assert uq["uq_usage_reports_seq"] == ["enterprise_id", "product_id", "currency", "report_seq"]


def test_reconciliation_endpoint_needs_the_report_scope_and_enterprise_authorization(env):
    ok = env.get(
        f"/internal/money/reconciliation?enterprise_id={env.ids['e1']}", headers=auth(tok(env))
    )
    assert (
        ok.status_code == 200
        and ok.json()["status"] == "PASS"
        and set(ok.json()) >= {"status", "counts", "discrepancies", "keys"}
    )
    assert (
        env.get(
            f"/internal/money/reconciliation?enterprise_id={env.ids['e3']}", headers=auth(tok(env))
        ).status_code
        == 403
    )
    assert (
        env.get(
            f"/internal/money/reconciliation?enterprise_id={env.ids['e1']}",
            headers=auth(tok(env, "mon", "money:read")),
        ).status_code
        == 403
    )
    assert (
        env.get(f"/internal/money/reconciliation?enterprise_id={env.ids['e1']}").status_code == 401
    )


def test_report_routes_exist_only_under_internal_money(env):
    paths = {
        p: sorted(v)
        for p, v in env.app.openapi()["paths"].items()
        if "usage" in p or "reconciliation" in p
    }
    assert paths == {
        "/internal/money/usage-reports": ["post"],
        "/internal/money/reconciliation": ["get"],
    }


def test_central_migration_0019_up_down_up(make_db):  # noqa: F811
    url = make_db()
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    assert "usage_reports" in inspect(eng).get_table_names()
    central_alembic(url, "downgrade", "0018")
    assert "usage_reports" not in inspect(eng).get_table_names()
    central_alembic(url, "upgrade", "head")
    eng.dispose()


# --- PostgreSQL: konkurrencë ---------------------------------------------------------------------------------------

pg = pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")


def _race(n, fn):
    barrier = threading.Barrier(n, timeout=20)
    out, errors = [None] * n, []

    def run(i):
        try:
            out[i] = fn(i, barrier)
        except BaseException as e:  # noqa: BLE001
            errors.append(repr(e))

    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join(40) for t in ts]
    assert not any(t.is_alive() for t in ts) and not errors, errors
    return out


@pg
def test_pg_two_identical_submissions_store_one_row_and_both_succeed(env):
    d = doc(env)

    def go(i, b):
        b.wait()
        return post(env, d).status_code

    codes = _race(2, go)
    assert sorted(codes) == [200, 201] and len(rows(env)) == 1


@pg
def test_pg_same_report_id_with_different_payloads_yields_one_winner_and_a_conflict(env):
    rid = uuid.uuid4()
    a, b = doc(env, rid=rid, gross="10.000000"), doc(env, rid=rid, gross="11.000000")

    def go(i, bar):
        bar.wait()
        return post(env, (a, b)[i]).status_code

    codes = _race(2, go)
    assert sorted(codes) == [201, 409] and len(rows(env)) == 1


@pg
def test_pg_same_seq_with_different_ids_yields_one_winner(env):
    def go(i, bar):
        bar.wait()
        return post(env, doc(env, seq=4)).status_code

    codes = _race(3, go)
    assert sorted(codes) == [201, 409, 409] and len(rows(env)) == 1


@pg
def test_pg_newer_and_older_reports_racing_leave_the_newest_as_current(env):
    docs = [doc(env, seq=s, ledger_max_id=s * 10) for s in (1, 2, 3, 4)]

    def go(i, bar):
        bar.wait()
        return post(env, docs[i]).status_code

    codes = _race(4, go)
    assert all(c in (201, 409) for c in codes)
    with Session(env.eng) as s:
        top = usage_reports.latest(s, env.ids["e1"], env.ids["sms"], "EUR")
        stored = [r.report_seq for r in rows(env)]
    assert top.report_seq == max(stored) and stored == sorted(stored)


@pg
def test_pg_reconciliation_while_a_report_arrives_is_deterministic_per_snapshot(env):
    from apps.central.services import money_reconciliation as mr

    post(env, doc(env, seq=1, ledger_max_id=1))

    def go(i, bar):
        bar.wait()
        if i == 0:
            return post(env, doc(env, seq=2, ledger_max_id=2)).status_code
        with Session(env.eng) as s:
            res = mr.reconcile(s, enterprise_id=env.ids["e1"], now=T0)
            return [k.report_seq for k in res.keys]

    out = _race(2, go)
    assert out[0] == 201 and out[1] in ([1], [2])  # ose një pamje ose tjetra, kurrë e përzier
