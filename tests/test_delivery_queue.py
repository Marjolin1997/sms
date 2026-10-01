# ruff: noqa: F811
"""M2-d: teste të kufirit dhe të adapterit `PostgresDeliveryQueue` + boshllëqet e karakterizimit të
webhook-ëve (unsafe_url, numëruesit, identiteti i delivery-t, gjendja e lidhjes gjatë HTTP). Baseline-i
M2-a (test_queue_semantics/test_queue_concurrency_pg) mbetet i pandryshuar."""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, select, text

from app.core.db import SessionLocal, engine
from app.models.events import DeliveryStatus, EndpointStatus, WebhookDelivery, WebhookEndpoint
from app.queue.delivery import DeliveryOutcome, DeliverySpec
from app.queue.postgres import PostgresDeliveryQueue
from app.services import events, net_guard, webhooks
from tests.test_queue_semantics import T0, emit, endpoint, utc, wh  # noqa: F401

PG = engine.dialect.name == "postgresql"
ROOT = Path(__file__).resolve().parents[1] / "app" / "queue"


# --- Kufiri ----------------------------------------------------------------------------------------------


def test_delivery_adapter_imports_no_http_or_domain_modules_and_never_commits():
    banned = ("app.models", "app.services", "app.providers", "app.api", "httpx", "requests",
              "app.core.crypto", "app.core.scope", "app.core.security")  # fmt: skip
    for f in ROOT.glob("*.py"):
        text_ = f.read_text()
        assert ".commit(" not in text_ and ".rollback(" not in text_, f.name
        for node in ast.walk(ast.parse(text_)):
            mods = []
            if isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            assert not [m for m in mods if m.startswith(banned)], (f.name, mods)
        for word in ("secret", "signature", "X-SMS", "consecutive_failures", "DISABLED", "410"):
            assert word not in text_, (f.name, word)


class Rec:
    def __init__(self):
        self.calls = []

    def completed(self, db, item, now):
        self.calls.append(("completed", item.id))
        item.status = DeliveryStatus.SUCCEEDED

    def failed(self, db, item, outcome):
        self.calls.append(("failed", item.id, outcome))
        item.status = DeliveryStatus.FAILED

    def replayed(self, db, item):
        self.calls.append(("replayed", item.id))
        item.status = DeliveryStatus.PENDING


@pytest.fixture
def qr():
    rec = Rec()
    spec = DeliverySpec(
        model=WebhookDelivery,
        base=lambda: select(WebhookDelivery).join(
            WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id
        ),
        eligible=lambda now: (WebhookDelivery.status == DeliveryStatus.PENDING)
        & (WebhookDelivery.next_attempt_at <= now)
        & (WebhookEndpoint.status == EndpointStatus.ACTIVE),
        attempts=WebhookDelivery.attempts, next_attempt_at=WebhookDelivery.next_attempt_at,
        id=WebhookDelivery.id, lease_s=120, retry_delays_s=(30, 120, 600),
    )  # fmt: skip
    return PostgresDeliveryQueue(spec, rec), rec


def one(db, wh):
    endpoint(db)
    emit(db)
    db.commit()
    return db.scalar(select(WebhookDelivery))


def test_spec_max_attempts_is_derived_from_the_explicit_delay_list(qr):
    queue, _ = qr
    assert queue.spec.max_attempts == 4
    assert webhooks.queue.spec.retry_delays_s == (30, 120, 600, 1800, 7200, 21600, 43200)
    assert webhooks.queue.spec.max_attempts == 8 and webhooks.queue.spec.lease_s == 120


def test_reserve_sets_lease_and_attempts_and_does_not_commit(qr, db, wh):
    queue, rec = qr
    d = one(db, wh)
    now = datetime.now(UTC)
    assert queue.reserve(db, now - timedelta(days=1)) is None  # jo due: asnjë efekt
    got = queue.reserve(db, now + timedelta(days=1))
    assert got.id == d.id and got.attempts == 1 and got.status == DeliveryStatus.PENDING
    assert (utc(got.next_attempt_at) - (now + timedelta(days=1))).total_seconds() == 120
    assert db.in_transaction() and rec.calls == []  # reserve s'thërret hooks, s'bën commit
    db.rollback()
    assert db.get(WebhookDelivery, d.id).attempts == 0


def test_retry_uses_the_explicit_list_and_hooks_decide_the_terminal_state(qr, db, wh):
    queue, rec = qr
    d = one(db, wh)
    out = []
    for attempt in (1, 2, 3):
        d.attempts = attempt
        out.append(queue.retry(db, d, now=T0, permanent=False))
        assert (utc(d.next_attempt_at) - T0).total_seconds() == (30, 120, 600)[attempt - 1]
    d.attempts = 4
    out.append(queue.retry(db, d, now=T0, permanent=False))
    assert out == [DeliveryOutcome.RETRIED] * 3 + [DeliveryOutcome.EXHAUSTED]
    d.attempts = 1
    assert queue.retry(db, d, now=T0, permanent=True) is DeliveryOutcome.FAILED
    assert [c[0] for c in rec.calls] == ["failed", "failed"]
    assert (
        rec.calls[0][2] is DeliveryOutcome.EXHAUSTED and rec.calls[1][2] is DeliveryOutcome.FAILED
    )


def test_replay_resets_the_same_row_and_calls_the_hook(qr, db, wh):
    queue, rec = qr
    d = one(db, wh)
    d.status, d.attempts = DeliveryStatus.FAILED, 8
    queue.replay(db, d, now=T0)
    assert d.attempts == 0 and utc(d.next_attempt_at) == T0 and d.status == DeliveryStatus.PENDING
    assert rec.calls == [("replayed", d.id)] and db.in_transaction()


def test_publish_adds_without_flush_or_commit(qr, db, wh):
    queue, _ = qr
    ep = endpoint(db)
    ev = events.emit(db, "c1", "message.sent", "message", "x")  # krijon edhe delivery-t e veta
    db.commit()
    extra = WebhookDelivery(endpoint_id=ep.id, event_id=ev.id, next_attempt_at=T0)
    queue.publish(db, [extra])
    assert (
        extra.id is None and extra in db.new
    )  # vetëm add: rreshti del me transaksionin e biznesit
    db.rollback()
    assert extra.id is None


# --- Outbox dhe politika e endpoint-it ---------------------------------------------------------------------


def test_disabled_endpoint_gets_no_delivery_at_publish(db, wh):
    ep = endpoint(db)
    ep.status = EndpointStatus.DISABLED
    db.commit()
    emit(db)
    db.commit()
    assert db.scalar(select(WebhookDelivery)) is None


def test_unsafe_url_disables_the_endpoint_immediately(db, wh):
    ep = endpoint(db)
    emit(db)
    db.commit()
    net_guard.set_resolver(lambda host: ["10.0.0.5"])  # IP private → UnsafeUrl
    d = webhooks.deliver_next(db, T0)
    db.refresh(ep)
    assert d.status == DeliveryStatus.FAILED and d.last_error.startswith("unsafe_url")
    assert ep.status == EndpointStatus.DISABLED and ep.disabled_reason == "unsafe_url"
    assert d.attempts == 1 and wh.requests == []  # HTTP s'u thirr


def test_success_resets_the_failure_counter_and_stamps_delivery(db, wh):
    ep = endpoint(db)
    ep.consecutive_failures = 4
    emit(db)
    db.commit()
    d = webhooks.deliver_next(db, T0)
    db.refresh(ep)
    assert d.status == DeliveryStatus.SUCCEEDED and utc(d.delivered_at) == T0
    assert ep.consecutive_failures == 0 and ep.status == EndpointStatus.ACTIVE


def test_the_same_delivery_id_header_is_used_across_retries_and_replay(db, wh):
    ep = endpoint(db)
    emit(db)
    db.commit()
    wh.status = 500
    d = webhooks.deliver_next(db, T0)
    webhooks.deliver_next(db, T0 + timedelta(seconds=30))
    wh.status = 410
    webhooks.deliver_next(db, T0 + timedelta(seconds=200))
    assert db.get(WebhookDelivery, d.id).status == DeliveryStatus.FAILED
    db.refresh(ep)
    assert (
        ep.status == EndpointStatus.DISABLED
    )  # 410 → çaktivizim; replay NUK e riaktivizon endpoint-in
    webhooks.redeliver(db, "c1", d.id)
    db.commit()
    assert webhooks.deliver_next(db, datetime.now(UTC) + timedelta(seconds=1)) is None
    ep.status = EndpointStatus.ACTIVE  # veprim i veçantë i klientit
    db.commit()
    wh.status = 200
    assert webhooks.deliver_next(db, datetime.now(UTC) + timedelta(seconds=1)) is not None
    ids = {r.headers["x-sms-delivery-id"] for r in wh.requests}
    assert (
        ids == {str(d.id)} and len(wh.requests) == 4
    )  # 3 + 1 pas replay; i njëjti rresht, i njëjti id
    assert db.query(WebhookDelivery).count() == 1  # replay s'krijon delivery të ri


def test_nested_exhaustion_counts_one_failure_per_exhausted_delivery(db, wh):
    ep = endpoint(db)
    emit(db)
    db.commit()
    wh.status = 503
    t = T0
    for _ in range(webhooks.MAX_ATTEMPTS):
        t += timedelta(days=2)
        webhooks.deliver_next(db, t)
    db.refresh(ep)
    d = db.scalar(select(WebhookDelivery))
    assert d.status == DeliveryStatus.FAILED and d.attempts == 8 and ep.consecutive_failures == 1
    assert ep.status == EndpointStatus.ACTIVE  # 1 < DISABLE_AFTER


# --- Gjendja e lidhjes gjatë HTTP ---------------------------------------------------------------------------


def test_http_runs_without_a_session_transaction_sql_or_pool_connection(db, wh):
    endpoint(db)
    emit(db)
    db.commit()
    log, seen = [], {}

    def sql(*a):
        log.append("sql")

    def commit(*a):
        log.append("commit")

    event.listen(engine, "before_cursor_execute", sql)
    event.listen(engine, "commit", commit)

    def hook(request):
        log.append("HTTP_START")
        seen["in_tx"] = db.in_transaction()
        seen["out"] = engine.pool.checkedout() if PG else 0
        log.append("HTTP_END")

    wh.hook = hook
    try:
        webhooks.deliver_next(db, T0)
    finally:
        event.remove(engine, "before_cursor_execute", sql)
        event.remove(engine, "commit", commit)
    start, end = log.index("HTTP_START"), log.index("HTTP_END")
    assert log[start - 1] == "commit" and "sql" not in log[start - 1 : end + 1]
    assert seen == {"in_tx": False, "out": 0}


@pytest.mark.skipif(not PG, reason="needs PostgreSQL")
def test_no_row_lock_and_no_idle_in_transaction_during_http(db, wh):
    ep = endpoint(db)
    emit(db)
    db.commit()
    seen = {}

    def hook(request):
        with engine.connect() as other:
            seen["states"] = [
                r[0]
                for r in other.execute(
                    text(
                        "select state from pg_stat_activity where datname = current_database()"
                        " and pid <> pg_backend_pid()"
                    )
                )
            ]
            # NOWAIT: nëse HTTP mbante lock rreshti, ky do të dështonte
            for tbl in ("sms_webhook_deliveries", "sms_webhook_endpoints"):
                other.execute(text(f"select 1 from {tbl} for update nowait")).all()
            other.rollback()

    wh.hook = hook
    webhooks.deliver_next(db, T0)
    assert "idle in transaction" not in seen["states"]
    assert ep.id is not None


_ = (SessionLocal,)
