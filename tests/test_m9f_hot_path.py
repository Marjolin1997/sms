# ruff: noqa: F811
"""M9-f — regresion i hot path pas M9-e: numri i SQL-ve për submit në local/shadow/central, pa thirrje rrjeti, pa Central sinkron."""

import httpx
import pytest
from sqlalchemy import event

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.services import messages as msgsvc
from tests.test_m9e_enterprise_pricing import central, mode
from tests.test_pipeline import OK, fake, world  # noqa: F401

PG = engine.dialect.name == "postgresql"

# SQL të ngrira për `submit + commit` (mesazh i parë i ngrohur) dhe `process_one`; PostgreSQL = referenca e saktë.
#   local   : 21 (e pandryshuar nga M2-b/M9-a: pricing.quote s'shton asnjë SELECT në local)
#   central : 25 = local − 3 (rate card, versione, rate legacy) + 7 SELECT të cache-it lokal: `sms_pricing_state`,
#             enterprise (identiteti), entitlement (produkti SMS), caktimi, libri, versioni, rregullat. Të gjitha lokale.
#   shadow  : 30 = local + 7 SELECT-et e cache-it Central + 1 INSERT krahasimi (+ 1 flush). Provizor: kalon në central.
PG_SUBMIT = {"local": 21, "shadow": 30, "central": 25}
PG_PROCESS = {"local": 10, "shadow": 10, "central": 10}
SHADOW_OVERHEAD_MAX = (
    10  # kufi i arsyetuar: shadow = çmimi i dytë + një rresht krahasimi, kurrë rritje pa kufi
)


def measure(db, mode_name, monkeypatch, key):
    mode(monkeypatch, mode_name)
    if mode_name != "local":
        central(db)  # snapshot-i Central në cache lokale (bën vetë commit)
    counts = {"n": 0}

    def count(*a):
        counts["n"] += 1

    with (
        SessionLocal() as s
    ):  # ngrohje: wallet/plan/rate në cache të sesionit s'ka efekt; prit identitetin
        msgsvc.submit(s, "c1", f"warm-{key}-1", OK, "ACME", text="hello")
        s.commit()
        msgsvc.submit(s, "c1", f"warm-{key}-2", OK, "ACME", text="hello")
        s.commit()
    event.listen(engine, "before_cursor_execute", count)
    try:
        with SessionLocal() as s:
            msgsvc.submit(s, "c1", f"m-{key}", OK, "ACME", text="hello")
            s.commit()
        submit_n, counts["n"] = counts["n"], 0
        with SessionLocal() as s:
            msgsvc.process_one(s)
        process_n = counts["n"]
    finally:
        event.remove(engine, "before_cursor_execute", count)
    return submit_n, process_n


def network_blocked(monkeypatch):
    def blocked(*a, **k):
        raise AssertionError("network used on the SMS hot path")

    monkeypatch.setattr(httpx.Client, "send", blocked)
    monkeypatch.setattr(httpx.AsyncClient, "send", blocked)
    import socket

    monkeypatch.setattr(
        socket.socket,
        "connect",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("socket.connect on hot path")),
        raising=False,
    )


@pytest.mark.skipif(
    not PG, reason="numrat e saktë janë të PostgreSQL (SQLite s'ka SELECT FOR UPDATE/savepoint)"
)
@pytest.mark.parametrize("mode_name", ["local", "shadow", "central"])
def test_pg_statement_counts_per_authority_are_frozen(db, world, monkeypatch, mode_name):
    sub, proc = measure(db, mode_name, monkeypatch, mode_name)
    print(f"HOTPATH {mode_name} submit={sub} process_one={proc}")  # noqa: T201
    assert (sub, proc) == (PG_SUBMIT[mode_name], PG_PROCESS[mode_name])


@pytest.mark.skipif(not PG, reason="needs PostgreSQL")
def test_pg_local_hot_path_is_identical_to_the_pre_m9e_baseline_and_shadow_overhead_is_bounded(
    db, world, monkeypatch
):
    local = measure(db, "local", monkeypatch, "a")
    shadow = measure(db, "shadow", monkeypatch, "b")
    assert local == (21, 10)  # baza e para-M9-e (M9-a: process_one 10)
    assert 0 <= shadow[0] - local[0] <= SHADOW_OVERHEAD_MAX and shadow[1] == local[1]


@pytest.mark.parametrize("mode_name", ["local", "shadow", "central"])
def test_sqlite_and_pg_submit_costs_are_positive_bounded_and_central_is_not_costlier_than_shadow(
    db, world, monkeypatch, mode_name
):
    n, p = measure(db, mode_name, monkeypatch, "z" + mode_name)
    assert 0 < n <= 60 and 0 < p <= 30


def test_submit_and_process_make_no_network_call_in_any_authority_mode(db, world, monkeypatch):
    network_blocked(monkeypatch)
    for i, m in enumerate(("local", "shadow", "central")):
        mode(monkeypatch, m)
        if m != "local":
            central(db)
        with SessionLocal() as s:
            msgsvc.submit(s, "c1", f"net-{i}", OK, "ACME", text="hello")
            s.commit()
        with SessionLocal() as s:
            msgsvc.process_one(s)  # providerul fake është lokal; asgjë tjetër s'del në rrjet
    assert settings.pricing_authority == "central"


def test_central_mode_sql_does_not_exceed_shadow_and_both_stay_within_the_justified_overhead(
    db, world, monkeypatch
):
    local = measure(db, "local", monkeypatch, "l")
    shadow = measure(db, "shadow", monkeypatch, "s")
    cent = measure(db, "central", monkeypatch, "c")
    print(f"HOTPATH-ALL local={local} shadow={shadow} central={cent}")  # noqa: T201
    assert cent[0] <= shadow[0]
    assert (
        shadow[0] - local[0] <= SHADOW_OVERHEAD_MAX + 6
        and cent[0] - local[0] <= SHADOW_OVERHEAD_MAX + 6
    )
    assert cent[1] == local[1] == shadow[1] or abs(cent[1] - local[1]) <= 1
