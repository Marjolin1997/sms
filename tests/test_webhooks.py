import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core import crypto
from app.core.config import settings
from app.models.events import (
    DeliveryStatus,
    EndpointStatus,
    Event,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.services import events, net_guard, webhooks
from app.services import messages as msg
from app.services.wallet import Conflict, NotFound
from tests.test_email import fake_dns, fake_email_provider, verified  # noqa: F401
from tests.test_pipeline import OK, fake, send, world  # noqa: F401

OWNER = "c1"
URL = "https://hooks.example.com/sms"
NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def public_dns():
    old = net_guard.get_resolver()
    net_guard.set_resolver(lambda host: ["93.184.216.34"])
    yield
    net_guard.set_resolver(old)
    webhooks.set_client(None)


class Receiver:
    """httpx transport i rremë që regjistron kërkesat."""

    def __init__(self, status=200, exc=None):
        self.status, self.exc, self.requests = status, exc, []

    def client(self):
        def handler(request: httpx.Request):
            self.requests.append(request)
            if self.exc:
                raise self.exc
            return httpx.Response(self.status)

        c = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
        webhooks.set_client(c)
        return c


def endpoint(db, types=None, owner=OWNER, url=URL):
    ep, secret = webhooks.create_endpoint(db, owner, url, types)
    db.commit()
    return ep, secret


# --- SSRF -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/x",
        "ftp://example.com",
        "https://localhost/x",
        "https://127.0.0.1/x",
        "https://10.1.2.3/x",
        "https://192.168.0.1/x",
        "https://172.16.0.9/x",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/x",
        "https://[::ffff:127.0.0.1]/x",
        "https://100.64.0.1/x",
        "https://0.0.0.0/x",
        "https://user:pw@example.com/x",
        "https://db.internal/x",
        "https://printer.local/x",
        "https://example.com:99999/x",
        "https:///nohost",
        "https://" + "a" * 2000 + ".com",
    ],
)
def test_unsafe_urls_rejected(url):
    with pytest.raises(net_guard.UnsafeUrl):
        net_guard.validate_url(url)


def test_hostname_resolving_to_private_or_mixed_is_rejected():
    net_guard.set_resolver(lambda h: ["10.0.0.1"])
    with pytest.raises(net_guard.UnsafeUrl):
        net_guard.validate_url("https://evil.example.com/x")
    net_guard.set_resolver(lambda h: ["93.184.216.34", "127.0.0.1"])  # një IP private mjafton
    with pytest.raises(net_guard.UnsafeUrl):
        net_guard.validate_url("https://evil.example.com/x")
    net_guard.set_resolver(lambda h: [])
    with pytest.raises(net_guard.UnsafeUrl):
        net_guard.validate_url("https://nx.example.com/x")
    assert net_guard.validate_url("https://8.8.8.8/x")  # IP publike literale ok


def test_http_only_allowed_when_configured(monkeypatch):
    with pytest.raises(net_guard.UnsafeUrl):
        net_guard.validate_url("http://example.com/x")
    monkeypatch.setattr(settings, "webhook_allow_http", True)
    assert net_guard.validate_url("http://example.com/x")


# --- Endpoint-e ---------------------------------------------------------------------


def test_secret_shown_once_and_encrypted_at_rest(db):
    ep, secret = webhooks.create_endpoint(db, OWNER, URL)
    db.commit()
    assert secret.startswith("whsec_") and secret not in ep.secret_enc
    assert crypto.decrypt(ep.secret_enc).decode() == secret
    ep2, secret2 = webhooks.rotate_secret(db, OWNER, ep.id)
    assert secret2 != secret and crypto.decrypt(ep2.secret_enc).decode() == secret2


def test_endpoint_validation_and_limits(db):
    for bad in (["nope"], ["message.*", "bogus.event"], [], ["x.*"]):
        with pytest.raises(webhooks.InvalidWebhook):
            webhooks.create_endpoint(db, OWNER, URL, bad)
    with pytest.raises(webhooks.InvalidWebhook):
        webhooks.create_endpoint(db, OWNER, "https://127.0.0.1/x")
    for i in range(webhooks.MAX_ENDPOINTS):
        webhooks.create_endpoint(db, OWNER, f"https://h{i}.example.com/x")
    with pytest.raises(Conflict):
        webhooks.create_endpoint(db, OWNER, URL)
    ep = db.query(WebhookEndpoint).first()
    with pytest.raises(NotFound):
        webhooks.update_endpoint(db, "c2", ep.id, enabled=False)
    with pytest.raises(webhooks.InvalidWebhook):
        webhooks.update_endpoint(db, OWNER, ep.id, url="http://169.254.169.254/")


def test_event_filters_and_fanout(db):
    all_ep, _ = endpoint(db)
    msgs, _ = endpoint(db, ["message.*"], url="https://b.example.com/x")
    bounced, _ = endpoint(db, ["email.bounced"], url="https://c.example.com/x")
    other, _ = endpoint(db, owner="c2", url="https://d.example.com/x")
    off, _ = endpoint(db, url="https://e.example.com/x")
    webhooks.update_endpoint(db, OWNER, off.id, enabled=False)
    events.emit(db, OWNER, "message.delivered", "message", "m1", {"status": "delivered"}, now=NOW)
    events.emit(db, OWNER, "email.bounced", "email", "e1", now=NOW)
    events.emit(db, OWNER, "email.delivered", "email", "e2", now=NOW)
    db.commit()
    got = {}
    for d in db.query(WebhookDelivery):
        got.setdefault(d.endpoint_id, []).append(db.get(Event, d.event_id).type)
    assert sorted(got[all_ep.id]) == ["email.bounced", "email.delivered", "message.delivered"]
    assert got[msgs.id] == ["message.delivered"] and got[bounced.id] == ["email.bounced"]
    assert other.id not in got and off.id not in got
    with pytest.raises(ValueError):
        events.emit(db, OWNER, "made.up", "x", "1")


# --- Eventet nga transicionet reale --------------------------------------------------


def test_real_transitions_emit_events_without_pii(db, world):  # noqa: F811
    endpoint(db)
    m = send(db)
    msg.process_one(db, datetime.now(UTC) + timedelta(seconds=1))
    msg.apply_dlr(db, "fake", m.provider_message_id, False, "absent_subscriber")
    db.commit()
    evs = db.query(Event).order_by(Event.id).all()
    assert [e.type for e in evs] == ["message.sent", "message.failed"]
    assert evs[1].data == {"message_id": m.public_id, "status": "failed", "segments": 1,
                           "error_code": "absent_subscriber"}  # fmt: skip
    blob = json.dumps([e.data for e in evs])
    assert OK.lstrip("+") not in blob and "hello" not in blob  # pa numër, pa tekst


def test_consent_and_campaign_events(db, world):  # noqa: F811
    from app.services import consent

    consent.record(db, OWNER, "sms", OK, "opt_in", "x", "form", "u", "evidence")
    consent.apply_inbound_keyword(db, OWNER, OK, "STOP")
    db.commit()
    types = [(e.type, e.data) for e in db.query(Event).order_by(Event.id)]
    assert types[0][0] == "consent.opted_in" and types[1][0] == "consent.opted_out"
    assert types[1][1]["address"] == OK.lstrip("+") and types[1][1]["reason"] == "stop_keyword"
    assert types[1][1]["hard"] is True


def test_campaign_lifecycle_events(db, world):  # noqa: F811
    from tests.test_campaigns import audience, campaign, drive, start

    lst, _ = audience(db, 2)
    c = campaign(db, lst)
    start(db, c)
    drive(db)
    kinds = [e.type for e in db.query(Event).order_by(Event.id) if e.type.startswith("campaign")]
    assert kinds == ["campaign.running", "campaign.completed"]  # SCHEDULED/PREPARING nuk emetohen


def test_email_events_emitted_without_address(db, verified):  # noqa: F811
    from app.services import emails
    from tests.test_email import TO
    from tests.test_email import send as esend

    esend(db)
    e = emails.process_one(db, datetime.now(UTC) + timedelta(seconds=1))
    emails.apply_event(db, "fake", e.provider_message_id, "bounce_hard", "550 no such user")
    db.commit()
    all_evs = db.query(Event).order_by(Event.id).all()
    evs = [x for x in all_evs if x.type.startswith("email.")]
    assert [x.type for x in evs] == ["email.sent", "email.bounced"]
    assert evs[1].data == {
        "email_id": e.public_id,
        "status": "bounced",
        "reason": "550 no such user",
    }
    assert TO not in json.dumps([x.data for x in evs])  # eventet e email-it s'kanë adresë
    # bounce i ashpër bllokon adresën: klienti merr edhe consent.opted_out (me adresë, për CRM)
    blocked = [x for x in all_evs if x.type == "consent.opted_out"]
    assert len(blocked) == 1 and blocked[0].data["reason"] == "bounce_hard"


# --- Dërgimi ------------------------------------------------------------------------


def one_event(db, owner=OWNER, t="message.delivered"):
    ev = events.emit(db, owner, t, "message", "m1", {"status": "delivered"}, now=NOW)
    db.commit()
    return ev


def test_successful_delivery_is_signed_and_verifiable(db):
    ep, secret = endpoint(db)
    one_event(db)
    rx = Receiver()
    rx.client()
    d = webhooks.deliver_next(db, NOW)
    assert d.status == DeliveryStatus.SUCCEEDED and d.attempts == 1 and d.last_status_code == 200
    req = rx.requests[0]
    body = req.content
    assert str(req.url) == URL and req.headers["content-type"] == "application/json"
    payload = json.loads(body)
    assert payload["type"] == "message.delivered" and payload["id"].startswith("evt_")
    assert payload["data"] == {
        "resource_type": "message",
        "resource_id": "m1",
        "status": "delivered",
    }
    sig = req.headers["x-sms-signature"]
    assert webhooks.verify_signature(secret, sig, body, now=NOW.timestamp())
    assert not webhooks.verify_signature("whsec_other", sig, body, now=NOW.timestamp())
    assert not webhooks.verify_signature(secret, sig, body + b" ", now=NOW.timestamp())
    assert not webhooks.verify_signature(secret, sig, body, now=NOW.timestamp() + 600)  # replay
    assert req.headers["x-sms-delivery-id"] == str(d.id)
    assert webhooks.deliver_next(db, NOW) is None  # s'ka më punë


def test_retry_schedule_then_failure_and_circuit_breaker(db):
    ep, _ = endpoint(db)
    rx = Receiver(status=500)
    rx.client()
    t = NOW
    for _ in range(webhooks.DISABLE_AFTER):
        one_event(db)
        d = None
        while True:
            d = webhooks.deliver_next(db, t)
            if d is None:
                t += timedelta(days=2)  # kalon çdo backoff
                d = webhooks.deliver_next(db, t)
                if d is None:
                    break
            if d.status == DeliveryStatus.FAILED:
                break
            expected = webhooks.RETRY_DELAYS[d.attempts - 1]
            assert (d.next_attempt_at.replace(tzinfo=UTC) - t).total_seconds() == expected
            t += timedelta(seconds=expected)
        assert d.status == DeliveryStatus.FAILED and d.attempts == webhooks.MAX_ATTEMPTS
    db.refresh(ep)
    assert ep.status == EndpointStatus.DISABLED and ep.disabled_reason == "too_many_failures"
    before = len(rx.requests)
    one_event(db)
    assert db.query(WebhookDelivery).filter_by(status=DeliveryStatus.PENDING).count() == 0
    assert webhooks.deliver_next(db, t + timedelta(days=5)) is None  # endpoint i çaktivizuar
    assert len(rx.requests) == before


def test_success_resets_failure_counter(db):
    ep, _ = endpoint(db)
    ep.consecutive_failures = 4
    db.commit()
    one_event(db)
    Receiver().client()
    webhooks.deliver_next(db, NOW)
    db.refresh(ep)
    assert ep.consecutive_failures == 0 and ep.status == EndpointStatus.ACTIVE


def test_410_gone_disables_immediately(db):
    ep, _ = endpoint(db)
    one_event(db)
    Receiver(status=410).client()
    d = webhooks.deliver_next(db, NOW)
    db.refresh(ep)
    assert d.status == DeliveryStatus.FAILED and d.attempts == 1
    assert ep.status == EndpointStatus.DISABLED and ep.disabled_reason == "gone"


def test_network_error_retries(db):
    endpoint(db)
    one_event(db)
    Receiver(exc=httpx.ConnectTimeout("t")).client()
    d = webhooks.deliver_next(db, NOW)
    assert d.status == DeliveryStatus.PENDING and d.last_error == "network:ConnectTimeout"


def test_redirects_are_not_followed(db):
    endpoint(db)
    one_event(db)
    rx = Receiver(status=302)
    rx.client()
    d = webhooks.deliver_next(db, NOW)
    assert d.status == DeliveryStatus.PENDING and d.last_error == "http_302"
    assert len(rx.requests) == 1


def test_dns_rebinding_to_private_ip_blocked_at_send_time(db):
    ep, _ = endpoint(db)
    one_event(db)
    rx = Receiver()
    rx.client()
    net_guard.set_resolver(lambda h: ["169.254.169.254"])  # DNS ndryshoi pas krijimit
    d = webhooks.deliver_next(db, NOW)
    db.refresh(ep)
    assert rx.requests == []  # asnjë kërkesë nuk doli
    assert d.status == DeliveryStatus.FAILED and d.last_error.startswith("unsafe_url")
    assert ep.status == EndpointStatus.DISABLED and ep.disabled_reason == "unsafe_url"


def test_lease_prevents_double_send_while_in_flight(db):
    from app.core.db import SessionLocal

    endpoint(db)
    one_event(db)
    inner = []

    def handler(request):
        with SessionLocal() as other:  # worker tjetër ndërsa ky është në HTTP
            inner.append(webhooks.deliver_next(other, NOW))
        return httpx.Response(200)

    webhooks.set_client(httpx.Client(transport=httpx.MockTransport(handler)))
    d = webhooks.deliver_next(db, NOW)
    assert inner == [None] and d.status == DeliveryStatus.SUCCEEDED


def test_redeliver_rules(db):
    ep, _ = endpoint(db)
    one_event(db)
    Receiver(status=410).client()
    d = webhooks.deliver_next(db, NOW)
    webhooks.update_endpoint(db, OWNER, ep.id, enabled=True)
    with pytest.raises(NotFound):
        webhooks.redeliver(db, "c2", d.id)
    r = webhooks.redeliver(db, OWNER, d.id)
    assert r.status == DeliveryStatus.PENDING and r.attempts == 0
    with pytest.raises(Conflict):
        webhooks.redeliver(db, OWNER, d.id)


def test_retention_purges_old_closed_events_only(db):
    endpoint(db)
    old = events.emit(db, OWNER, "message.sent", "message", "old", now=NOW - timedelta(days=40))
    keep_pending = events.emit(
        db, OWNER, "message.sent", "message", "p", now=NOW - timedelta(days=40)
    )
    fresh = events.emit(db, OWNER, "message.sent", "message", "fresh", now=NOW)
    db.commit()
    for d in db.query(WebhookDelivery).filter_by(event_id=old.id):
        d.status = DeliveryStatus.SUCCEEDED
    db.commit()
    n = events.purge_old(db, 30, now=NOW)
    db.commit()
    ids = {e.id for e in db.query(Event)}
    assert n == 1 and old.id not in ids and {keep_pending.id, fresh.id} <= ids


# --- API -----------------------------------------------------------------------------

BOOT = {"X-Admin-Key": "test-key"}


@pytest.fixture
def raw_client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app())


def _key(c, owner):
    r = c.post(
        "/v1/admin/api-keys", json={"name": "k", "role": "client", "owner_ref": owner}, headers=BOOT
    )
    return {"Authorization": f"Bearer {r.json()['key']}"}


def test_api_webhook_flow_and_isolation(db, raw_client):
    c = raw_client
    h1, h2 = _key(c, "c1"), _key(c, "c2")
    r = c.post(
        "/v1/webhooks/endpoints", json={"url": URL, "event_types": ["message.*"]}, headers=h1
    )
    assert r.status_code == 201 and r.json()["secret"].startswith("whsec_")
    eid, secret = r.json()["id"], r.json()["secret"]
    assert "secret" not in c.get("/v1/webhooks/endpoints", headers=h1).json()[0]
    assert (
        c.post(
            "/v1/webhooks/endpoints", json={"url": "https://127.0.0.1/x"}, headers=h1
        ).status_code
        == 422
    )
    assert (
        c.patch(f"/v1/webhooks/endpoints/{eid}", json={"enabled": False}, headers=h2).status_code
        == 404
    )
    assert c.get("/v1/webhooks/endpoints", headers=h2).json() == []
    assert c.post(f"/v1/webhooks/endpoints/{eid}/test", headers=h1).status_code == 202
    rx = Receiver()
    rx.client()
    while webhooks.deliver_next(db, datetime.now(UTC) + timedelta(seconds=5)):
        pass
    dl = c.get("/v1/webhooks/deliveries", headers=h1).json()
    assert len(dl) == 1 and dl[0]["status"] == "succeeded" and dl[0]["type"] == "webhook.ping"
    assert c.get("/v1/webhooks/deliveries", headers=h2).json() == []
    assert webhooks.verify_signature(secret, rx.requests[0].headers["x-sms-signature"],
                                     rx.requests[0].content, now=rx.requests and datetime.now(UTC).timestamp())  # fmt: skip
    new = c.post(f"/v1/webhooks/endpoints/{eid}/rotate-secret", headers=h1).json()["secret"]
    assert new != secret
    assert c.post(f"/v1/webhooks/deliveries/{dl[0]['id']}/redeliver", headers=h1).status_code == 200
    assert c.delete(f"/v1/webhooks/endpoints/{eid}", headers=h2).status_code == 404
    assert c.delete(f"/v1/webhooks/endpoints/{eid}", headers=h1).status_code == 204
    assert c.get("/v1/webhooks/endpoints", headers=h1).json() == []
    actions = {a["action"] for a in c.get("/v1/admin/audit", headers=BOOT).json()}
    assert {"webhook.create", "webhook.rotate_secret", "webhook.delete"} <= actions


def test_api_event_log_pull_with_cursor(db, raw_client):
    c = raw_client
    h1, h2 = _key(c, "c1"), _key(c, "c2")
    for i in range(3):
        events.emit(db, "c1", "message.sent", "message", f"m{i}")
    events.emit(db, "c1", "email.bounced", "email", "e1")
    events.emit(db, "c2", "message.sent", "message", "other")
    db.commit()
    page = c.get("/v1/events", params={"limit": 2}, headers=h1).json()
    assert [e["data"]["resource_id"] for e in page] == ["m0", "m1"]
    nxt = c.get("/v1/events", params={"after_id": page[-1]["cursor"]}, headers=h1).json()
    assert [e["data"]["resource_id"] for e in nxt] == ["m2", "e1"]
    only = c.get("/v1/events", params={"type": "email.bounced"}, headers=h1).json()
    assert len(only) == 1
    assert [e["data"]["resource_id"] for e in c.get("/v1/events", headers=h2).json()] == ["other"]


def test_self_service_keys(raw_client):
    c = raw_client
    h1 = _key(c, "c1")
    r = c.post("/v1/portal/api-keys", json={"name": "ci"}, headers=h1)
    assert r.status_code == 201 and r.json()["key"].startswith("sms_")
    kid = r.json()["id"]
    new = {"Authorization": f"Bearer {r.json()['key']}"}
    assert c.get("/v1/portal/api-keys", headers=new).status_code == 200  # çelësi i ri punon
    assert c.get("/v1/admin/stats", headers=new).status_code == 403  # dhe s'ka privilegje shtesë
    assert all("key" not in k for k in c.get("/v1/portal/api-keys", headers=h1).json())
    other = _key(c, "c2")
    assert c.post(f"/v1/portal/api-keys/{kid}/revoke", headers=other).status_code == 404
    assert c.post(f"/v1/portal/api-keys/{kid}/revoke", headers=h1).status_code == 200
    assert c.get("/v1/portal/api-keys", headers=new).status_code == 401
    assert (
        c.post("/v1/portal/api-keys", json={"name": "x"}, headers=BOOT).status_code == 403
    )  # staf
    for n in range(20):
        c.post("/v1/portal/api-keys", json={"name": f"k{n}"}, headers=h1)
    assert c.post("/v1/portal/api-keys", json={"name": "over"}, headers=h1).status_code == 409


def test_portal_overview(db, world, raw_client):  # noqa: F811
    c = raw_client
    h1 = _key(c, "c1")
    send(db, key="o1")
    msg.process_one(db, datetime.now(UTC) + timedelta(seconds=1))
    db.commit()
    o = c.get("/v1/portal/overview", headers=h1).json()
    assert o["wallets"][0]["available"] == "9.950000" and o["wallets"][0]["held"] == "0.050000"
    assert o["sms_last_30d"] == {"sent": 1} and o["webhooks"] == {
        "active_endpoints": 0,
        "failed_deliveries_24h": 0,
    }
    assert c.get("/v1/portal/overview", headers=_key(c, "c2")).json()["wallets"] == []


def test_me_endpoint(raw_client):
    c = raw_client
    assert c.get("/v1/me").status_code == 401
    me = c.get("/v1/me", headers=_key(c, "c1")).json()
    assert (
        me["role"] == "client" and me["owner_ref"] == "c1" and "messages:send" in me["permissions"]
    )
    assert "keys:manage" not in me["permissions"]
    boot = c.get("/v1/me", headers=BOOT).json()
    assert (
        boot["role"] == "superadmin" and boot["owner_ref"] is None and boot["permissions"] == ["*"]
    )
