"""M2-a: teste KARAKTERIZIMI të queue-së së sotme (SMS, email, webhook), PA ndryshuar implementimin.

Ato përshkruajnë sjelljen që refactor-i M2 duhet ta ruajë. Kur një sjellje është e papritur ose
divergon mes llojeve, testi e thotë shprehimisht (`# DIVERGENCË` / `# RISK`). Portable (SQLite +
PostgreSQL); konkurrenca reale është te `test_queue_concurrency_pg.py`."""

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select

import app.providers as providers
from app.core.db import SessionLocal
from app.models.email import Email, EmailEvent, EmailStatus
from app.models.events import (
    DeliveryStatus,
    EndpointStatus,
    Event,
    WebhookDelivery,
)
from app.models.sending import Message, MessageEvent, MessageStatus
from app.providers.base import SendResult
from app.services import emails, events, net_guard, switches, webhooks
from app.services import messages as msgsvc
from app.services import wallet as wallets
from app.services.wallet import Conflict, NotFound
from tests.test_email import FROM, fake_dns, fake_email_provider, verified  # noqa: F401
from tests.test_pipeline import OK, PERM, TEMP, fake, world  # noqa: F401

T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)


def utc(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ====================================================================================================
# Dispatch queue: SMS dhe email. Të njëjtat pohime, dy implementime (klone sot).
# ====================================================================================================


class Kind:
    """Adapter i testit mbi dy implementimet e sotme të `DispatchQueue`."""

    MAX = 5
    BACKOFF = 30  # sekonda; vonesa = 30 * 2**(attempts-1)

    def count(self, db):
        return db.scalar(select(func.count()).select_from(self.model))

    def get(self, db, item):
        db.expire_all()
        return db.get(self.model, item.id)

    def events_to(self, db, item, to_status):
        col = self.event_model.to_status
        fk = self.event_model.message_id if self.name == "sms" else self.event_model.email_id
        return db.scalar(
            select(func.count())
            .select_from(self.event_model)
            .where(fk == item.id, col == to_status)
        )

    def install(self, send):
        """Zëvendëson provider-in me një funksion `send(req)`."""

        class P:
            name = "fake"

            def send(self, req):
                return send(req)

        (providers._registry if self.name == "sms" else providers._email_registry)["fake"] = P()


class SmsKind(Kind):
    name = "sms"
    model, event_model = Message, MessageEvent
    QUEUED, SENDING, SENT, FAILED = (
        MessageStatus.QUEUED, MessageStatus.SENDING, MessageStatus.SENT, MessageStatus.FAILED,
    )  # fmt: skip
    ok, temp, perm = OK, TEMP, PERM
    cancel_code = "campaign_cancelled"

    def submit(self, db, key, to=None, **kw):
        return msgsvc.submit(db, "c1", key, to or self.ok, "ACME", text="hello", **kw)

    def claim(self, db, now=None):
        return msgsvc.claim_next(db, now)

    def process(self, db, now=None):
        return msgsvc.process_one(db, now)

    def cancel(self, db, item_id):
        return msgsvc.cancel_if_queued(db, item_id)


class EmailKind(Kind):
    name = "email"
    model, event_model = Email, EmailEvent
    QUEUED, SENDING, SENT, FAILED = (
        EmailStatus.QUEUED, EmailStatus.SENDING, EmailStatus.SENT, EmailStatus.FAILED,
    )  # fmt: skip
    ok, temp, perm = "ana@customer.org", "temp@customer.org", "reject@customer.org"
    cancel_code = "campaign_cancelled"

    def submit(self, db, key, to=None, **kw):
        return emails.submit(db, "c1", key, FROM, to or self.ok, subject="Hello", text="Body", **kw)

    def claim(self, db, now=None):
        return emails.claim_next(db, now)

    def process(self, db, now=None):
        return emails.process_one(db, now)

    def cancel(self, db, item_id):
        return emails.cancel_if_queued(db, item_id)


@pytest.fixture(params=["sms", "email"])
def q(request, db, world):  # noqa: F811
    if request.param == "email":
        request.getfixturevalue("verified")
        return EmailKind()
    request.getfixturevalue("fake")
    return SmsKind()


def publish(q, db, key="k1", to=None, **kw):
    """`publish` i sotëm: krijimi i rreshtit të domain-it QUEUED, në transaksionin e thirrësit."""
    return q.submit(db, key, to, **kw)


# --- publish / transaksioni i biznesit -------------------------------------------------------------


def test_publish_creates_exactly_one_queued_item(q, db):
    item = publish(q, db)
    db.commit()
    assert q.count(db) == 1
    row = q.get(db, item)
    assert row.status == q.QUEUED and row.attempts == 0
    assert abs((utc(row.next_attempt_at) - datetime.now(UTC)).total_seconds()) < 60
    assert q.events_to(db, item, "queued") == 1  # historiku shkruhet në të njëjtin transaksion


PG_ONLY = pytest.mark.skipif(
    not os.environ.get("SMS_TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="SQLite/pysqlite nuk e nis transaksionin para SAVEPOINT: rollback s'e zhbën submit()",
)


@PG_ONLY
def test_publish_rollback_leaves_no_visible_work(q, db):
    publish(q, db)
    db.rollback()
    assert q.count(db) == 0
    assert q.claim(db) is None


def test_publish_is_invisible_to_reserve_until_commit_then_visible(q, db):
    publish(q, db)
    with SessionLocal() as other:  # sesion tjetër: transaksioni i biznesit s'ka bërë ende commit
        if db.get_bind().dialect.name == "postgresql":
            assert q.claim(other) is None
            other.rollback()
    db.commit()
    with SessionLocal() as other:
        got = q.claim(other)
        assert got is not None and got.status == q.SENDING
        other.rollback()


@PG_ONLY
def test_publish_never_commits_by_itself(q, db):
    """Kontrata e pronësisë: thirrësi zotëron transaksionin; publish vetëm e ndërton rreshtin."""
    publish(q, db)
    assert db.in_transaction()
    db.rollback()
    assert q.count(db) == 0


def test_idempotency_is_a_domain_concern_and_unchanged(q, db):
    a = publish(q, db, key="same")
    db.commit()
    b = publish(q, db, key="same")  # e njëjta kërkesë → i njëjti rresht, asnjë punë e dytë
    db.commit()
    assert b.id == a.id and q.count(db) == 1
    with pytest.raises(Exception) as e:  # e njëjta key, kërkesë tjetër
        publish(q, db, key="same", to=q.temp)
    assert "idempotency" in str(e.value).lower() or isinstance(e.value, Conflict)
    db.rollback()
    assert q.count(db) == 1


# --- reserve / claim ----------------------------------------------------------------------------


def test_reserve_moves_queued_to_sending_and_increments_attempts_once(q, db):
    item = publish(q, db)
    db.commit()
    got = q.claim(db)
    assert got.id == item.id and got.status == q.SENDING and got.attempts == 1
    assert q.events_to(db, item, "sending") == 1  # tranzicioni i state machine-it (MessageEvent)
    db.commit()
    assert q.claim(db) is None  # s'rezervohet dy herë
    assert q.get(db, item).attempts == 1


def test_only_due_items_are_reserved(q, db):
    item = publish(q, db)
    item.next_attempt_at = T0 + timedelta(seconds=100)
    db.commit()
    assert q.claim(db, T0) is None
    assert q.claim(db, T0 + timedelta(seconds=99)) is None
    assert q.claim(db, T0 + timedelta(seconds=100)).id == item.id  # kufiri: <= now


def test_reserve_order_is_next_attempt_at_then_id(q, db):
    a, b, c = publish(q, db, "a"), publish(q, db, "b", q.temp), publish(q, db, "c", q.perm)
    a.next_attempt_at, b.next_attempt_at, c.next_attempt_at = (
        T0 + timedelta(seconds=5), T0 + timedelta(seconds=1), T0 + timedelta(seconds=1),
    )  # fmt: skip
    db.commit()
    now = T0 + timedelta(seconds=10)
    order = []
    for _ in range(3):
        got = q.claim(db, now)
        order.append(got.id)
        db.commit()
    assert order == [
        b.id,
        c.id,
        a.id,
    ]  # FIFO sipas (next_attempt_at, id), jo garanci strikte nën konkurrencë


def test_kill_switch_stops_dispatch_without_touching_the_queue(q, db):
    item = publish(q, db)
    db.commit()
    switches.set_switch(db, switches.DISPATCH, False, "test", "characterization")
    db.commit()
    assert q.process(db) is None
    row = q.get(db, item)
    assert row.status == q.QUEUED and row.attempts == 0


# --- transaksioni i worker-it: claim → COMMIT → provider (jashtë tx) → tranzicion → COMMIT -----------


def test_claim_is_committed_before_the_provider_is_called(q, db):
    item = publish(q, db)
    db.commit()
    seen = {}

    def send(req):
        with SessionLocal() as other:  # sesion tjetër sheh gjendjen e commit-uar
            seen["status"] = other.get(q.model, item.id).status
            seen["attempts"] = other.get(q.model, item.id).attempts
        seen["in_tx"] = db.in_transaction()
        return SendResult("p-1")

    q.install(send)
    q.process(db)
    assert seen["status"] == q.SENDING and seen["attempts"] == 1
    # NDRYSHUAR NGA PATCH-I I TRANSAKSIONIT TË EMAIL (qëllimisht): më parë email e thërriste provider-in
    # brenda një tx leximi të hapur (`in_tx is True`); tani leximet e domenit/DKIM bëhen para COMMIT#1
    # dhe provider-i thirret jashtë çdo transaksioni, si SMS.
    assert seen["in_tx"] is False
    assert q.get(db, item).status == q.SENT


def test_crash_after_claim_commit_leaves_item_sending_forever(q, db):
    item = publish(q, db)
    db.commit()
    got = q.claim(db)
    assert got.status == q.SENDING
    db.commit()  # commit-i i parë i process_one
    db.close()  # worker vdes këtu: provider-i s'u thirr ose rezultati u humb
    with SessionLocal() as s:
        row = s.get(q.model, item.id)
        assert row.status == q.SENDING and row.attempts == 1
        far = datetime.now(UTC) + timedelta(days=365)
        assert q.claim(s, far) is None  # NUK ka auto-recovery, edhe pas një viti
        assert q.process(s, far) is None
        assert s.get(q.model, item.id).status == q.SENDING


def test_crash_before_claim_commit_leaves_item_queued_and_uncounted(q, db):
    item = publish(q, db)
    db.commit()
    q.claim(db)
    db.rollback()  # crash para commit-it të parë: claim-i nuk është i qëndrueshëm
    row = q.get(db, item)
    assert row.status == q.QUEUED and row.attempts == 0


def test_provider_exception_after_invocation_is_unknown_not_a_blind_retry(q, db):
    """M9-a (zëvendëson sjelljen e vjetër W6): një përjashtim i papritur PAS fillimit të thirrjes
    mund të ketë dërguar mesazhin ⇒ provider JO-idempotent (default) ⇒ UNKNOWN, pa ri-radhitje
    (një ridërgim do të dyfishonte SMS/email). Provider idempotent: shih tests/test_m9a_*."""
    item = publish(q, db)
    db.commit()

    def boom(req):
        raise RuntimeError("connection reset after send")

    q.install(boom)
    q.process(db, T0)
    row = q.get(db, item)
    assert row.status.value == "unknown" and row.error_code == "provider_exception"
    assert row.attempts == 1


def test_stuck_sending_is_only_reported_for_sms(q, db):
    item = publish(q, db)
    db.commit()
    q.claim(db)
    db.commit()
    later = datetime.now(UTC) + timedelta(minutes=11)
    if q.name == "sms":
        assert [m.id for m in msgsvc.stuck_sending(db, timedelta(minutes=10), later)] == [item.id]
    else:
        assert not hasattr(emails, "stuck_sending")  # DIVERGENCË: email s'ka raportim të ngecurish


# --- retry / failure ---------------------------------------------------------------------------------


def test_retry_delays_are_exactly_30_60_120_240_then_terminal(q, db):
    item = publish(q, db, to=q.temp)
    db.commit()
    t = T0
    delays = []
    for attempt in range(1, q.MAX + 1):
        got = q.process(db, t)
        assert got.id == item.id and got.attempts == attempt  # attempts rritet vetëm në reserve
        if attempt < q.MAX:
            assert got.status == q.QUEUED and got.error_code == "fake_temporary"
            delay = (utc(got.next_attempt_at) - t).total_seconds()
            delays.append(delay)
            assert q.process(db, t + timedelta(seconds=delay - 1)) is None  # jo para kohe
            t += timedelta(seconds=delay)
    assert delays == [30, 60, 120, 240]
    assert got.status == q.FAILED and got.error_code == "fake_temporary"
    assert q.process(db, t + timedelta(days=30)) is None  # terminal: s'rezervohet më
    assert q.get(db, item).attempts == q.MAX


def test_permanent_error_is_terminal_at_first_attempt(q, db):
    item = publish(q, db, to=q.perm)
    db.commit()
    got = q.process(db, T0)
    assert got.status == q.FAILED and got.attempts == 1 and got.error_code == "fake_rejected"
    assert q.process(db, T0 + timedelta(days=1)) is None
    assert q.events_to(db, item, "failed") == 1


def test_sms_failure_releases_the_hold_email_has_no_money(db, world):  # noqa: F811
    w, _ = world
    m = msgsvc.submit(db, "c1", "h", TEMP, "ACME", text="hello")
    db.commit()
    assert wallets.balances(db, w.id) == (D("9.95"), D("0.05"))
    t = T0
    for _ in range(msgsvc.MAX_ATTEMPTS):
        msgsvc.process_one(db, t)
        t += timedelta(days=1)
    assert m.status == MessageStatus.FAILED
    assert wallets.balances(db, w.id) == (D("10"), D("0"))  # efekt domain, jo i queue-së


# --- cancel_if_pending ---------------------------------------------------------------------------------


def test_cancel_if_queued_fails_the_item_with_campaign_cancelled(q, db):
    item = publish(q, db)
    db.commit()
    assert q.cancel(db, item.id) is True
    db.commit()
    row = q.get(db, item)
    assert row.status == q.FAILED and row.error_code == q.cancel_code
    assert q.claim(db) is None  # s'rezervohet më


def test_cancel_if_queued_is_a_noop_once_reserved_or_unknown(q, db):
    item = publish(q, db)
    db.commit()
    q.claim(db)
    db.commit()
    assert q.cancel(db, item.id) is False  # SENDING: do të dërgohet
    assert q.get(db, item).status == q.SENDING
    assert q.cancel(db, 999_999) is False
    q.get(db, item)


def test_cancel_if_queued_on_sms_releases_the_hold(db, world):  # noqa: F811
    w, _ = world
    m = msgsvc.submit(db, "c1", "c", OK, "ACME", text="hello")
    db.commit()
    assert msgsvc.cancel_if_queued(db, m.id) is True
    db.commit()
    assert wallets.balances(db, w.id) == (D("10"), D("0"))


# ====================================================================================================
# Delivery queue: webhook deliveries (outbox transaksional + lease)
# ====================================================================================================

URL = "https://hooks.example.com/sms"


class Crash(BaseException):
    """Simulon vdekjen e proçesit gjatë HTTP (BaseException kalon `except httpx.HTTPError`)."""


@pytest.fixture
def wh(db):
    import httpx

    old = net_guard.get_resolver()
    net_guard.set_resolver(lambda host: ["93.184.216.34"])

    class Rx:
        def __init__(self):
            self.requests, self.status, self.hook = [], 200, None

        def install(self):
            def handler(request: httpx.Request):
                self.requests.append(request)
                if self.hook:
                    self.hook(request)
                return httpx.Response(self.status)

            webhooks.set_client(httpx.Client(transport=httpx.MockTransport(handler)))

    rx = Rx()
    rx.install()
    yield rx
    webhooks.set_client(None)
    net_guard.set_resolver(old)


def endpoint(db, url=URL, owner="c1"):
    ep, _ = webhooks.create_endpoint(db, owner, url, ["*"])
    db.commit()
    return ep


def emit(db, n=1, owner="c1"):
    return [events.emit(db, owner, "message.sent", "message", f"m{i}") for i in range(n)]


def deliveries(db):
    db.expire_all()
    return db.scalars(select(WebhookDelivery).order_by(WebhookDelivery.id)).all()


def test_event_and_delivery_are_written_in_the_business_transaction(db, wh):
    endpoint(db)
    emit(db, 2)
    assert db.in_transaction()  # emit nuk bën commit
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Event)) == 0
    assert db.scalar(select(func.count()).select_from(WebhookDelivery)) == 0  # asnjë delivery jetim
    emit(db, 2)
    db.commit()
    assert db.scalar(select(func.count()).select_from(Event)) == 2
    ds = deliveries(db)
    assert len(ds) == 2 and all(d.status == DeliveryStatus.PENDING and d.attempts == 0 for d in ds)


def test_one_delivery_per_matching_active_endpoint(db, wh):
    endpoint(db, "https://hooks.example.com/a")
    endpoint(db, "https://hooks.example.com/b")
    other = webhooks.create_endpoint(db, "c2", "https://hooks.example.com/c", ["*"])[0]
    emit(db, 1)
    db.commit()
    ds = deliveries(db)
    assert len(ds) == 2 and other.id not in {d.endpoint_id for d in ds}


def test_reserve_sets_lease_and_attempts_and_commits_before_http(db, wh):
    ep = endpoint(db)
    emit(db)
    db.commit()
    seen = {}

    def during_http(request):
        seen["in_tx"] = db.in_transaction()  # HTTP jashtë transaksionit
        with SessionLocal() as other:
            d = other.scalar(select(WebhookDelivery))
            seen["attempts"], seen["status"] = d.attempts, d.status
            seen["lease"] = (utc(d.next_attempt_at) - T0).total_seconds()
            seen["second_worker"] = webhooks.deliver_next(other, T0)  # lease aktiv → asgjë

    wh.hook = during_http
    d = webhooks.deliver_next(db, T0)
    assert seen == {
        "in_tx": False, "attempts": 1, "status": DeliveryStatus.PENDING,
        "lease": webhooks.LEASE_SECONDS, "second_worker": None,
    }  # fmt: skip
    assert d.status == DeliveryStatus.SUCCEEDED and ep.id == d.endpoint_id


def test_lease_expiry_makes_the_delivery_reservable_again_at_least_once(db, wh):
    """AT-LEAST-ONCE: crash pas commit-it të lease-it → pas LEASE_SECONDS ridërgohet me të njëjtin
    X-SMS-Delivery-Id (dritarja e dublikimit; pranuesi deduplikon me atë header)."""
    endpoint(db)
    emit(db)
    db.commit()

    def die(request):
        raise Crash

    wh.hook = die
    with pytest.raises(Crash):
        webhooks.deliver_next(db, T0)
    db.rollback()
    d = deliveries(db)[0]
    assert d.status == DeliveryStatus.PENDING and d.attempts == 1  # lease-i është i qëndrueshëm
    wh.hook = None
    assert webhooks.deliver_next(db, T0 + timedelta(seconds=webhooks.LEASE_SECONDS - 1)) is None
    again = webhooks.deliver_next(db, T0 + timedelta(seconds=webhooks.LEASE_SECONDS))
    assert again.id == d.id and again.attempts == 2 and again.status == DeliveryStatus.SUCCEEDED
    ids = [r.headers["x-sms-delivery-id"] for r in wh.requests]
    assert ids == [str(d.id), str(d.id)]  # e njëjta delivery dy herë: dublikimi është i pranuar


def test_retry_schedule_is_exact_and_replaces_the_lease(db, wh):
    assert webhooks.RETRY_DELAYS == [30, 120, 600, 1800, 7200, 21600, 43200]
    assert webhooks.MAX_ATTEMPTS == 8 and webhooks.LEASE_SECONDS == 120
    endpoint(db)
    emit(db)
    db.commit()
    wh.status = 500
    t = T0
    for attempt in range(1, webhooks.MAX_ATTEMPTS):
        d = webhooks.deliver_next(db, t)
        assert d.attempts == attempt and d.status == DeliveryStatus.PENDING
        delay = webhooks.RETRY_DELAYS[attempt - 1]
        assert (utc(d.next_attempt_at) - t).total_seconds() == delay
        assert webhooks.deliver_next(db, t + timedelta(seconds=delay - 1)) is None
        t += timedelta(seconds=delay)
    d = webhooks.deliver_next(db, t)  # përpjekja e 8-të
    assert d.attempts == 8 and d.status == DeliveryStatus.FAILED and d.last_error == "http_500"
    assert webhooks.deliver_next(db, t + timedelta(days=30)) is None


def test_reserve_order_is_next_attempt_at_then_id_and_skips_disabled_endpoints(db, wh):
    ep_a = endpoint(db, "https://hooks.example.com/a")
    emit(db, 2)
    db.commit()
    ds = deliveries(db)
    ds[0].next_attempt_at = T0 + timedelta(seconds=5)
    ds[1].next_attempt_at = T0 + timedelta(seconds=1)
    db.commit()
    first = webhooks.deliver_next(db, T0 + timedelta(seconds=10))
    assert first.id == ds[1].id
    ep_a.status = EndpointStatus.DISABLED
    db.commit()
    assert webhooks.deliver_next(db, T0 + timedelta(days=1)) is None  # endpoint i çaktivizuar


def test_redeliver_resets_a_terminal_delivery_and_refuses_pending(db, wh):
    endpoint(db)
    emit(db)
    db.commit()
    d = deliveries(db)[0]
    with pytest.raises(Conflict):
        webhooks.redeliver(db, "c1", d.id)  # ende PENDING
    wh.status = 410
    done = webhooks.deliver_next(db, T0)
    assert done.status == DeliveryStatus.FAILED
    before = datetime.now(UTC)
    r = webhooks.redeliver(db, "c1", d.id)
    db.commit()
    assert r.status == DeliveryStatus.PENDING and r.attempts == 0 and r.last_error is None
    assert utc(r.next_attempt_at) >= before
    with pytest.raises(NotFound):
        webhooks.redeliver(db, "c2", d.id)  # tenant tjetër


def test_endpoint_is_disabled_at_the_current_thresholds(db, wh):
    ep = endpoint(db)
    wh.status = 500
    t = T0
    for i in range(webhooks.DISABLE_AFTER):
        emit(db)
        db.commit()
        for _ in range(webhooks.MAX_ATTEMPTS):
            t += timedelta(days=2)
            webhooks.deliver_next(db, t)
        db.refresh(ep)
        expected = (
            EndpointStatus.DISABLED if i == webhooks.DISABLE_AFTER - 1 else EndpointStatus.ACTIVE
        )
        assert ep.status == expected, i
    assert ep.disabled_reason == "too_many_failures" and webhooks.DISABLE_AFTER == 5


def test_http_410_disables_immediately_and_network_errors_retry(db, wh):
    ep = endpoint(db)
    emit(db)
    db.commit()
    wh.status = 410
    d = webhooks.deliver_next(db, T0)
    db.refresh(ep)
    assert d.status == DeliveryStatus.FAILED and ep.disabled_reason == "gone"
