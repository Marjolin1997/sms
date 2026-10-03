from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

import app.providers as providers
from app.models.sending import AccountPlan, MessageStatus, Route
from app.providers import FakeProvider, ProviderError
from app.services import messages as svc
from app.services import rates, sender_ids, templates
from app.services import wallet as wallets
from app.services.wallet import Conflict, InsufficientFunds, TopupMethod

PAST = datetime(2020, 1, 1, tzinfo=UTC)
OK = "+355691230003"  # pranohet nga FakeProvider
TEMP = "+355691230001"
PERM = "+355691230002"


@pytest.fixture(autouse=True)
def fake():
    p = FakeProvider()
    providers._registry["fake"] = p
    return p


@pytest.fixture
def world(db):
    """Klienti c1: wallet 10 EUR, rate card 0.05/segment, sender ACME miratuar, route AL→fake."""
    w = wallets.create_wallet(db, "c1", "EUR")
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "10", TopupMethod.CASH).id)
    card = rates.create_card(db, "std", "EUR")
    v = rates.new_draft(db, card.id)
    rates.set_rate(db, v.id, "355", "0.05")
    rates.publish(db, v.id, PAST, now=PAST - timedelta(days=1))
    db.add(AccountPlan(owner_ref="c1", rate_card_id=card.id))
    db.add(Route(prefix="355", country="AL", provider="fake"))
    s = sender_ids.request(db, "c1", "AL", "ACME")
    sender_ids.approve(db, s.id, "admin")
    db.commit()
    return w, card


def send(db, key="k1", to=OK, text="hello", **kw):
    m = svc.submit(db, "c1", key, to, "ACME", text=text, **kw)
    db.commit()
    return m


def test_happy_path_deliver_captures(db, world, fake):
    w, _ = world
    m = send(db)
    assert m.status == MessageStatus.QUEUED and m.total_price == D("0.05")
    assert wallets.balances(db, w.id) == (D("9.95"), D("0.05"))
    svc.process_one(db)
    assert m.status == MessageStatus.SENT and m.provider_message_id == "fake-1"
    svc.apply_dlr(db, "fake", "fake-1", delivered=True)
    db.commit()
    assert m.status == MessageStatus.DELIVERED
    assert wallets.balances(db, w.id) == (D("9.95"), D("0"))
    assert wallets.verify_wallet(db, w.id)
    assert fake.calls[0].reference == m.public_id


def test_failed_dlr_refunds_everything(db, world):
    w, _ = world
    m = send(db)
    svc.process_one(db)
    svc.apply_dlr(db, "fake", m.provider_message_id, delivered=False, code="absent_subscriber")
    db.commit()
    assert m.status == MessageStatus.FAILED and m.error_code == "absent_subscriber"
    assert wallets.balances(db, w.id) == (D("10"), D("0"))


def test_dlr_idempotent_and_conflicting(db, world):
    m = send(db)
    svc.process_one(db)
    pid = m.provider_message_id
    svc.apply_dlr(db, "fake", pid, delivered=True)
    svc.apply_dlr(db, "fake", pid, delivered=True)
    with pytest.raises(Conflict):
        svc.apply_dlr(db, "fake", pid, delivered=False)
    assert wallets.balances(db, m.wallet_id) == (D("9.95"), D("0"))


def test_idempotent_submit_charges_once(db, world):
    w, _ = world
    a = send(db)
    b = send(db)
    assert a.id == b.id
    assert wallets.balances(db, w.id) == (D("9.95"), D("0.05"))
    with pytest.raises(Conflict):
        send(db, text="different")


def test_insufficient_funds_creates_nothing(db, world):
    w, _ = world
    with pytest.raises(InsufficientFunds):
        svc.submit(db, "c1", "big", OK, "ACME", text="x" * 1530 * 30)  # 300 segm. = 15 EUR
    db.rollback()
    assert wallets.verify_wallet(db, w.id)


def test_rejections_before_money_moves(db, world):
    w, _ = world
    before = wallets.balances(db, w.id)
    with pytest.raises(sender_ids.SenderNotAllowed):
        svc.submit(db, "c1", "a", OK, "OTHER", text="hi")
    with pytest.raises(svc.NoRoute):
        svc.submit(db, "c1", "b", "+4915112345678", "ACME", text="hi")
    with pytest.raises(svc.AccountDisabled):
        svc.submit(db, "nobody", "c", OK, "ACME", text="hi")
    with pytest.raises(rates.InvalidNumber):
        svc.submit(db, "c1", "d", "0691230003", "ACME", text="hi")
    with pytest.raises(svc.InvalidMessage):
        svc.submit(db, "c1", "e", OK, "ACME")
    plan = db.query(AccountPlan).one()
    plan.enabled = False  # kill switch
    with pytest.raises(svc.AccountDisabled):
        svc.submit(db, "c1", "f", OK, "ACME", text="hi")
    assert wallets.balances(db, w.id) == before


def test_temporary_error_backs_off_then_fails_and_refunds(db, world):
    w, _ = world
    m = send(db, to=TEMP)
    t = datetime.now(UTC)
    for attempt in range(1, svc.MAX_ATTEMPTS + 1):
        assert svc.process_one(db, t) is m
        if attempt < svc.MAX_ATTEMPTS:
            assert m.status == MessageStatus.QUEUED and m.attempts == attempt
            assert svc.process_one(db, t) is None  # backoff nuk ka mbaruar
            t += timedelta(seconds=svc.BACKOFF_SECONDS * 2 ** (attempt - 1))
    assert m.status == MessageStatus.FAILED and m.error_code == "fake_temporary"
    assert wallets.balances(db, w.id) == (D("10"), D("0"))
    assert svc.process_one(db, t) is None


def test_permanent_error_fails_immediately(db, world):
    w, _ = world
    m = send(db, to=PERM)
    svc.process_one(db)
    assert m.status == MessageStatus.FAILED and m.attempts == 1
    assert wallets.balances(db, w.id) == (D("10"), D("0"))


def test_unexpected_provider_exception_is_retried_not_lost(db, world, fake):
    m = send(db)

    def boom(_):
        raise RuntimeError("timeout")

    fake.send = boom
    svc.process_one(db)
    assert m.status == MessageStatus.QUEUED and m.error_code == "provider_exception"


def test_unknown_provider_fails(db, world):
    db.query(Route).one().provider = "ghost"
    db.commit()
    m = send(db)
    svc.process_one(db)
    assert m.status == MessageStatus.FAILED and m.error_code == "unknown_provider"
    with pytest.raises(ProviderError):
        providers.get_provider("ghost")


def test_price_frozen_when_rates_change(db, world):
    w, card = world
    m = send(db)
    v2 = rates.new_draft(db, card.id)
    rates.set_rate(db, v2.id, "355", "9.99")
    rates.publish(db, v2.id, datetime.now(UTC) + timedelta(seconds=1), now=datetime.now(UTC))
    db.commit()
    svc.process_one(db)
    svc.apply_dlr(db, "fake", m.provider_message_id, delivered=True)
    assert m.total_price == D("0.05")
    assert wallets.balances(db, w.id)[0] == D("9.95")


def test_illegal_transitions_blocked(db, world):
    m = send(db)
    with pytest.raises(Conflict):
        svc._move(db, m, MessageStatus.DELIVERED)  # QUEUED → DELIVERED nuk lejohet
    with pytest.raises(wallets.NotFound):
        svc.apply_dlr(db, "fake", "nope", True)


def test_template_message(db, world):
    v = templates.create(db, "c1", "otp", "Code {{code}}")
    templates.review(db, v.id, "approve", "admin")
    m = svc.submit(db, "c1", "t1", OK, "ACME", template_id=v.template_id, values={"code": "99"})
    assert m.text == "Code 99" and m.template_version_id == v.id
    with pytest.raises(svc.InvalidMessage):
        svc.submit(db, "c1", "t2", OK, "ACME", text="x", template_id=v.template_id)


def test_status_history_is_recorded(db, world):
    m = send(db)
    svc.process_one(db)
    svc.apply_dlr(db, "fake", m.provider_message_id, True)
    db.commit()
    from app.models.sending import MessageEvent

    trail = [e.to_status for e in db.query(MessageEvent).order_by(MessageEvent.id)]
    assert trail == ["queued", "sending", "sent", "delivered"]


def test_api(client, db, world):
    body = {"owner_ref": "c1", "to": OK, "sender": "ACME", "text": "hi"}
    assert client.post("/v1/messages", json=body).status_code == 422  # pa Idempotency-Key
    r = client.post("/v1/messages", json=body, headers={"Idempotency-Key": "api-1"})
    assert r.status_code == 202 and r.json()["total_price"] == "0.050000"
    r2 = client.post("/v1/messages", json=body, headers={"Idempotency-Key": "api-1"})
    assert r2.json()["id"] == r.json()["id"]
    assert client.get(f"/v1/messages/{r.json()['id']}").json()["status"] == "queued"
    assert client.get(f"/v1/messages/{r.json()['id']}/events").json()[0]["to"] == "queued"
    bad = client.post("/v1/messages", json={**body, "sender": "NOPE"},
                      headers={"Idempotency-Key": "api-2"})  # fmt: skip
    assert bad.status_code == 403
