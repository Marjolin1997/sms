"""Teste që kanë kuptim vetëm mbi PostgreSQL të vërtetë: migrimet me triggers dhe
konkurrenca reale mbi wallet-in. Kalojnë ose kapërcehen (skip) pa SMS_TEST_DATABASE_URL."""

import os
import subprocess
import sys
import threading
import uuid
from decimal import Decimal as D

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.services import wallet as wallets
from tests.test_pipeline import OK, fake, world  # noqa: F401

URL = os.environ.get("SMS_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL.startswith("postgresql"), reason="needs PostgreSQL")


@pytest.fixture
def migrated_url():
    """Databazë e re e ngritur vetëm me Alembic (me triggers), e fshirë në fund."""
    admin = create_engine(URL, isolation_level="AUTOCOMMIT")
    name = f"sms_mig_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(URL).set(database=name).render_as_string(hide_password=False)
    yield url
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


def alembic(url, *args):
    env = {**os.environ, "SMS_DATABASE_URL": url}
    r = subprocess.run(
        [sys.executable, "-m", "alembic", *args], env=env, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    return r


def test_migrations_up_down_up_and_triggers_block_tampering(migrated_url):
    alembic(migrated_url, "upgrade", "head")
    eng = create_engine(migrated_url)
    with eng.begin() as c:
        c.execute(
            text(
                "insert into sms_wallets (owner_ref, currency, created_at) values ('a','EUR',now())"
            )
        )
        c.execute(
            text(
                "insert into sms_ledger_entries (wallet_id, entry_type, available_delta, held_delta,"
                " available_after, held_after, idempotency_key, created_at)"
                " values (1,'TOPUP',5,0,5,0,'k',now())"
            )
        )
        c.execute(
            text(
                "insert into sms_audit_log (actor, role, action, target_type, target_id, created_at)"
                " values ('a','r','x','t','1',now())"
            )
        )
        c.execute(
            text(
                "insert into sms_dlr_receipts (provider, provider_message_id, status, outcome,"
                " raw_body, received_at) values ('p','m','delivered','applied','{}',now())"
            )
        )
        c.execute(
            text(
                "insert into sms_consent_events (owner_ref, channel, address_hash, action, reason,"
                " source, actor, created_at) values ('a','sms','h','OPT_IN','opt_in','s','a',now())"
            )
        )
    for stmt in (
        "update sms_ledger_entries set available_delta = 999",
        "delete from sms_ledger_entries",
        "truncate sms_ledger_entries",
        "update sms_audit_log set actor = 'evil'",
        "delete from sms_audit_log",
        "truncate sms_audit_log",
        "truncate sms_message_events",
        "update sms_dlr_receipts set outcome = 'x'",
        "delete from sms_dlr_receipts",
        "truncate sms_dlr_receipts",
        "update sms_consent_events set evidence = 'forged'",
        "truncate sms_consent_events",
    ):
        with pytest.raises(DBAPIError, match="append-only|cannot truncate"), eng.begin() as c:
            c.execute(text(stmt))
    with eng.connect() as c:
        assert c.execute(text("select available_delta from sms_ledger_entries")).scalar() == 5
    eng.dispose()
    alembic(migrated_url, "downgrade", "base")
    alembic(migrated_url, "upgrade", "head")


def test_ledger_check_constraints_block_negative_balance(migrated_url):
    alembic(migrated_url, "upgrade", "head")
    eng = create_engine(migrated_url)
    with eng.begin() as c:
        c.execute(
            text(
                "insert into sms_wallets (owner_ref, currency, created_at) values ('a','EUR',now())"
            )
        )
    with pytest.raises(DBAPIError), eng.begin() as c:
        c.execute(
            text(
                "insert into sms_ledger_entries (wallet_id, entry_type, available_delta, held_delta,"
                " available_after, held_after, idempotency_key, created_at)"
                " values (1,'ADJUSTMENT',-1,0,-1,0,'neg',now())"
            )
        )
    eng.dispose()


def test_concurrent_reservations_never_overspend(db):
    """20 rezervime paralele × 1 EUR mbi një wallet me 5 EUR: saktësisht 5 kalojnë."""
    w = wallets.create_wallet(db, "race", "EUR")
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "5", wallets.TopupMethod.CASH).id)
    db.commit()
    ok, insufficient, errors = [], [], []
    start = threading.Barrier(20)

    def worker(i):
        with SessionLocal() as s:
            try:
                start.wait()
                wallets.reserve(s, w.id, "1", f"race-{i}")
                s.commit()
                ok.append(i)
            except wallets.InsufficientFunds:
                s.rollback()
                insufficient.append(i)
            except Exception as e:  # noqa: BLE001
                s.rollback()
                errors.append(repr(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    assert (len(ok), len(insufficient)) == (5, 15)
    db.expire_all()
    assert wallets.balances(db, w.id) == (D("0"), D("5"))
    assert wallets.verify_wallet(db, w.id)


def test_concurrent_same_topup_confirm_credits_once(db):
    w = wallets.create_wallet(db, "race2", "EUR")
    t = wallets.create_topup(db, w.id, "7", wallets.TopupMethod.ELECTRONIC, external_ref="pay-x")
    db.commit()
    start = threading.Barrier(10)
    errors = []

    def worker():
        with SessionLocal() as s:
            try:
                start.wait()
                wallets.confirm_topup(s, t.id)
                s.commit()
            except Exception as e:  # noqa: BLE001
                s.rollback()
                errors.append(repr(e))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    [x.start() for x in threads]
    [x.join() for x in threads]
    assert not errors, errors
    db.expire_all()
    assert wallets.balances(db, w.id) == (D("7"), D("0"))


def test_concurrent_capture_and_release_one_wins(db):
    w = wallets.create_wallet(db, "race3", "EUR")
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "3", wallets.TopupMethod.CASH).id)
    h = wallets.reserve(db, w.id, "3", "h1")
    db.commit()
    outcomes = []
    start = threading.Barrier(2)

    def run(fn, name):
        with Session(db.get_bind()) as s:
            try:
                start.wait()
                fn(s, h.id)
                s.commit()
                outcomes.append((name, "ok"))
            except wallets.Conflict:
                s.rollback()
                outcomes.append((name, "conflict"))

    ts = [
        threading.Thread(target=run, args=(wallets.capture, "capture")),
        threading.Thread(target=run, args=(wallets.release, "release")),
    ]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(o[1] for o in outcomes) == ["conflict", "ok"]
    db.expire_all()
    avail, held = wallets.balances(db, w.id)
    assert held == D("0") and avail in (D("0"), D("3"))
    assert wallets.verify_wallet(db, w.id)


def _threads(n, fn):
    ts = [threading.Thread(target=fn, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]


def test_concurrent_submit_same_key_charges_once(db, world):  # noqa: F811
    from app.services import messages as svc

    w, _ = world
    barrier = threading.Barrier(8)
    ids, errors = [], []

    def go(_):
        with SessionLocal() as s:
            try:
                barrier.wait()
                m = svc.submit(s, "c1", "same-key", OK, "ACME", text="hello")
                s.commit()
                ids.append(m.id)
            except Exception as e:  # noqa: BLE001
                s.rollback()
                errors.append(repr(e))

    _threads(8, go)
    assert not errors, errors
    assert len(set(ids)) == 1
    db.expire_all()
    assert wallets.balances(db, w.id) == (D("9.95"), D("0.05"))  # një rezervim i vetëm


def test_parallel_workers_send_each_message_exactly_once(db, world, fake):  # noqa: F811
    from app.services import messages as svc

    for i in range(12):
        svc.submit(db, "c1", f"w{i}", OK, "ACME", text="hello")
    db.commit()

    def worker(_):
        with SessionLocal() as s:
            while svc.process_one(s):
                pass

    _threads(4, worker)
    refs = [c.reference for c in fake.calls]
    assert len(refs) == 12 and len(set(refs)) == 12  # asnjë dërgim i dyfishtë, asnjë i humbur
