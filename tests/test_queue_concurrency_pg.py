"""M2-a: konkurrencë REALE në PostgreSQL mbi queue-në e sotme (FOR UPDATE SKIP LOCKED, lease, outbox).
Dy ose më shumë lidhje DB, sinkronizim me Barrier/Event me timeout. Kalon ose kapërcehet pa
SMS_TEST_DATABASE_URL. Ruan sjelljen që refactor-i M2 nuk duhet ta ndryshojë."""

import os
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.core.db import SessionLocal, engine
from app.models.events import DeliveryStatus, EndpointStatus, WebhookDelivery
from tests.test_email import fake_dns, fake_email_provider, verified  # noqa: F401
from tests.test_pipeline import fake, world  # noqa: F401
from tests.test_queue_semantics import (  # noqa: F401
    URL,
    Crash,
    emit,
    endpoint,
    publish,
    q,
    utc,
    wh,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("SMS_TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="needs PostgreSQL",
)
T = 20  # timeout i sinkronizimit (s): një bllokim del si dështim, jo si varje


def run_threads(n, fn):
    """Nis n thread me Barrier; mbledh përjashtimet; dështon nëse ndonjë varet."""
    barrier = threading.Barrier(n, timeout=T)
    errors, out = [], [None] * n

    def wrap(i):
        try:
            out[i] = fn(i, barrier)
        except BaseException as e:  # noqa: BLE001
            errors.append(repr(e))

    ts = [threading.Thread(target=wrap, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join(T * 2) for t in ts]
    assert not any(t.is_alive() for t in ts), "thread i varur"
    assert not errors, errors
    return out


# --- reserve: dy lidhje ------------------------------------------------------------------------------


def test_two_sessions_get_different_items_skip_locked(q, db):  # noqa: F811
    a = publish(q, db, "a")
    b = publish(q, db, "b", q.temp)
    db.commit()
    with SessionLocal() as s1, SessionLocal() as s2:
        first = q.claim(s1)  # mban lock-un (pa commit)
        second = q.claim(s2)  # SKIP LOCKED: kalon te tjetri, nuk pret
        assert {first.id, second.id} == {a.id, b.id} and first.id != second.id
        s1.commit()
        s2.commit()


def test_locked_only_item_is_skipped_not_awaited_and_returns_after_rollback(q, db):  # noqa: F811
    item = publish(q, db)
    db.commit()
    with SessionLocal() as s1, SessionLocal() as s2:
        assert q.claim(s1).id == item.id
        t0 = time.monotonic()
        assert q.claim(s2) is None  # s'bllokon (SKIP LOCKED, jo FOR UPDATE që pret)
        assert time.monotonic() - t0 < 5
        s1.rollback()  # worker-i vdes para commit-it: lock-u lirohet, claim-i zhbëhet
        again = q.claim(s2)
        assert again.id == item.id and again.attempts == 1  # increment-i i s1 nuk mbeti
        s2.commit()


def test_uncommitted_publish_is_invisible_committed_is_visible(q, db):  # noqa: F811
    publish(q, db)
    with SessionLocal() as other:
        assert q.claim(other) is None
        other.rollback()
    db.rollback()
    with SessionLocal() as other:
        assert q.claim(other) is None  # rollback: asnjë punë jetime
        other.rollback()
    publish(q, db, "k2")
    db.commit()
    with SessionLocal() as other:
        assert q.claim(other) is not None
        other.rollback()


def test_stampede_each_item_is_reserved_exactly_once(q, db):  # noqa: F811
    n = 24
    for i in range(n):
        publish(
            q, db, f"k{i}", to=f"+35569123{1000 + i}" if q.name == "sms" else f"u{i}@customer.org"
        )
    db.commit()

    def worker(i, barrier):
        got = []
        with SessionLocal() as s:
            barrier.wait()
            while (m := q.claim(s)) is not None:
                got.append(m.id)
                s.commit()  # claim i qëndrueshëm, si te process_one
        return got

    claimed = [i for part in run_threads(8, worker) for i in part]
    assert len(claimed) == n and len(set(claimed)) == n  # asnjë dublikat, asnjë i humbur
    db.expire_all()
    rows = db.scalars(select(q.model)).all()
    assert {r.attempts for r in rows} == {1} and {r.status for r in rows} == {q.SENDING}


def test_stampede_processing_calls_the_provider_once_per_item(q, db):  # noqa: F811
    n = 16
    for i in range(n):
        publish(
            q, db, f"k{i}", to=f"+35569123{2000 + i}" if q.name == "sms" else f"v{i}@customer.org"
        )
    db.commit()
    refs, lock = [], threading.Lock()

    def send(req):
        with lock:
            refs.append(req.reference)
        from app.providers.base import SendResult

        return SendResult(f"p-{req.reference}")

    q.install(send)
    later = datetime.now(UTC) + timedelta(seconds=5)

    def worker(i, barrier):
        with SessionLocal() as s:
            barrier.wait()
            while q.process(s, later):
                pass

    run_threads(6, worker)
    assert len(refs) == n and len(set(refs)) == n


# --- cancel_if_pending ka garë me reserve -----------------------------------------------------------------


def test_cancel_skips_an_item_a_worker_holds_and_the_send_proceeds(q, db):  # noqa: F811
    item = publish(q, db)
    db.commit()
    with SessionLocal() as worker, SessionLocal() as canceller:
        assert q.claim(worker).id == item.id
        assert q.cancel(canceller, item.id) is False  # SKIP LOCKED: worker-i e ka; do të dërgohet
        canceller.rollback()
        worker.commit()
    assert q.get(db, item).status == q.SENDING


def test_reserve_skips_an_item_a_canceller_holds_then_it_is_failed(q, db):  # noqa: F811
    item = publish(q, db)
    db.commit()
    with SessionLocal() as worker, SessionLocal() as canceller:
        assert q.cancel(canceller, item.id) is True  # lock i mbajtur, pa commit
        assert q.claim(worker) is None
        worker.rollback()
        canceller.commit()
    row = q.get(db, item)
    assert row.status == q.FAILED and row.error_code == q.cancel_code
    with SessionLocal() as worker:
        assert q.claim(worker) is None


# --- crash i vërtetë i lidhjes ---------------------------------------------------------------------------


def test_backend_killed_mid_claim_releases_the_item_and_forgets_the_attempt(q, db):  # noqa: F811
    item = publish(q, db)
    db.commit()
    s1 = SessionLocal()
    pid = s1.execute(text("select pg_backend_pid()")).scalar()
    claimed = q.claim(s1)  # lock + UPDATE, pa commit
    assert claimed.id == item.id
    with engine.connect() as admin:
        admin.execute(text("select pg_terminate_backend(:p)"), {"p": pid})
        admin.commit()
    try:
        s1.close()
    except Exception:  # noqa: BLE001
        pass
    with SessionLocal() as s2:
        again = q.claim(s2)
        assert again.id == item.id and again.attempts == 1 and again.status == q.SENDING
        s2.commit()


def test_backend_killed_after_claim_commit_leaves_sending(q, db):  # noqa: F811
    """Dritarja e ddështimit e SMS/email: pas commit-it të claim-it, vdekja e worker-it lë SENDING
    (nuk ka lease/auto-recovery sot). Ripërpjekja është vendim manual."""
    item = publish(q, db)
    db.commit()
    s1 = SessionLocal()
    q.claim(s1)
    s1.commit()
    s1.close()
    with SessionLocal() as s2:
        assert q.claim(s2, datetime.now(UTC) + timedelta(days=30)) is None
    assert q.get(db, item).status == q.SENDING


# --- idempotency nën konkurrencë ----------------------------------------------------------------------------


def test_concurrent_publish_with_the_same_key_creates_one_item(q, db):  # noqa: F811
    def go(i, barrier):
        with SessionLocal() as s:
            barrier.wait()
            try:
                item = publish(q, s, "same-key")
                s.commit()
                return item.id
            except Exception:  # noqa: BLE001
                s.rollback()
                raise

    ids = run_threads(8, go)
    assert (
        len(set(ids)) == 1 and q.count(db) == 1
    )  # domain concern: idempotency e ruan queue-n me 1 punë


# --- ku hapet transaksioni gjatë thirrjes së provider-it (dokumentim empirik) ---------------------------------


def test_worker_connection_state_during_the_provider_call(q, db):  # noqa: F811
    """SMS dhe email: asnjë lidhje 'idle in transaction' gjatë provider call (patch-i i transaksionit
    të email: leximet e domenit/DKIM bëhen para COMMIT#1; më parë email ishte 1) dhe rreshti s'është i kyçur."""
    item = publish(q, db)
    db.commit()
    seen = {}

    def send(req):
        from app.providers.base import SendResult

        with engine.connect() as other:
            seen["idle_in_tx"] = other.execute(
                text(
                    "select count(*) from pg_stat_activity where datname = current_database()"
                    " and state = 'idle in transaction' and pid <> pg_backend_pid()"
                )
            ).scalar()
            row = other.execute(
                text(f"select id from {q.model.__tablename__} where id = :i for update nowait"),
                {"i": item.id},
            ).first()  # NOWAIT kalon: rreshti i item-it nuk është i kyçur gjatë provider call
            seen["row_locked"] = row is None
            other.rollback()
        return SendResult("p-1")

    q.install(send)
    q.process(db)
    assert seen["row_locked"] is False
    assert seen["idle_in_tx"] == 0  # ndryshuar qëllimisht: email ishte 1 para patch-it


# ====================================================================================================
# Webhook: lease + outbox nën konkurrencë
# ====================================================================================================


def test_active_lease_blocks_a_concurrent_worker_until_it_expires(db, wh):  # noqa: F811
    endpoint(db)
    emit(db)
    db.commit()
    inside, release = threading.Event(), threading.Event()
    now = datetime.now(UTC)

    def hook(request):
        inside.set()
        assert release.wait(T)

    wh.hook = hook
    result = {}

    def slow_worker():
        with SessionLocal() as s:
            result["d"] = webhooks_deliver(s, now)

    t = threading.Thread(target=slow_worker)
    t.start()
    assert inside.wait(T)  # worker-i A është në HTTP, lease i commit-uar, asnjë lock DB
    with SessionLocal() as s2:
        assert webhooks_deliver(s2, now + timedelta(seconds=5)) is None  # lease aktiv
        release.set()
    t.join(T)
    assert result["d"].status == DeliveryStatus.SUCCEEDED
    assert len(wh.requests) == 1


def webhooks_deliver(session, now):
    from app.services import webhooks

    return webhooks.deliver_next(session, now)


def test_after_lease_expiry_exactly_one_of_many_workers_takes_it_over(db, wh):  # noqa: F811
    endpoint(db)
    emit(db)
    db.commit()
    t0 = datetime.now(UTC)

    def die(request):
        raise Crash

    wh.hook = die
    with SessionLocal() as s, pytest.raises(Crash):
        webhooks_deliver(s, t0)
    wh.hook = None
    wh.requests.clear()
    after = t0 + timedelta(seconds=121)

    def worker(i, barrier):
        with SessionLocal() as s:
            barrier.wait()
            return webhooks_deliver(s, after)

    got = run_threads(6, worker)
    assert sum(1 for g in got if g is not None) == 1
    assert len(wh.requests) == 1  # një ridërgim, jo gjashtë
    d = db.scalars(select(WebhookDelivery)).one()
    db.refresh(d)
    assert d.attempts == 2 and d.status == DeliveryStatus.SUCCEEDED


def test_reserve_does_not_wait_on_a_row_locked_by_another_transaction(db, wh):  # noqa: F811
    endpoint(db)
    emit(db)
    db.commit()
    with SessionLocal() as holder, SessionLocal() as worker:
        holder.execute(select(WebhookDelivery).with_for_update()).all()  # lock i mbajtur
        t0 = time.monotonic()
        assert webhooks_deliver(worker, datetime.now(UTC)) is None  # SKIP LOCKED
        assert time.monotonic() - t0 < 5
        holder.rollback()
        worker.rollback()
    assert webhooks_deliver(db, datetime.now(UTC)) is not None


def test_outbox_row_is_invisible_to_workers_until_the_business_commit(db, wh):  # noqa: F811
    endpoint(db)
    emit(db)  # transaksioni i biznesit ende i hapur
    with SessionLocal() as w:
        assert webhooks_deliver(w, datetime.now(UTC)) is None
    db.rollback()
    with SessionLocal() as w:
        assert webhooks_deliver(w, datetime.now(UTC)) is None  # rollback: asgjë për t'u dërguar
    assert wh.requests == []
    emit(db)
    db.commit()
    with SessionLocal() as w:
        assert webhooks_deliver(w, datetime.now(UTC)) is not None
    assert len(wh.requests) == 1
    _ = (URL, EndpointStatus, utc)
