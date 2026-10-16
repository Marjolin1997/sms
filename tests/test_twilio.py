"""Adapter Twilio: dërgimi (transport i simuluar), klasifikimi i gabimeve, nënshkrimi dhe
callback-et e statusit/SMS-it hyrës."""

import json
from urllib.parse import parse_qsl, urlencode

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.inbound import InboundMessage
from app.models.sending import Message, MessageStatus
from app.providers import ProviderError, SendRequest
from app.providers.twilio import TwilioProvider, twilio_signature, verify_twilio_signature
from app.services import consent, sender_ids
from app.services import messages as svc
from app.services import wallet as wallets
from tests.test_pipeline import OK, send, world  # noqa: F401

TOKEN = "tw-token"
CB = "https://api.example.com/webhooks/twilio/status"


def req(**kw):
    base = dict(reference="ref-1", sender="355690000001", destination="355691234567",
                text="Kodi 481516", encoding="GSM-7", segments=1)  # fmt: skip
    return SendRequest(**(base | kw))


def provider(handler, **kw):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TwilioProvider("ACtest", TOKEN, CB, client=client, **kw)


# --- Nënshkrimi ----------------------------------------------------------------


def test_signature_matches_the_official_documentation_example():
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = [("CallSid", "CA1234567890ABCDE"), ("Caller", "+14158675309"), ("Digits", "1234"),
              ("From", "+14158675309"), ("To", "+18005551212")]  # fmt: skip
    assert twilio_signature("12345", url, params) == "RSOYDt4T1cUTdK1PDd93/VVr8B8="
    assert verify_twilio_signature("12345", url, params[::-1], "RSOYDt4T1cUTdK1PDd93/VVr8B8=")
    assert not verify_twilio_signature("wrong", url, params, "RSOYDt4T1cUTdK1PDd93/VVr8B8=")
    assert not verify_twilio_signature("12345", url, params, "")
    assert not verify_twilio_signature("", url, params, "RSOYDt4T1cUTdK1PDd93/VVr8B8=")


# --- Dërgimi ---------------------------------------------------------------------


def test_send_builds_the_expected_request():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["form"] = dict(parse_qsl(request.content.decode()))
        return httpx.Response(201, json={"sid": "SM123", "status": "queued"})

    assert provider(handler).send(req()).provider_message_id == "SM123"
    assert seen["url"] == "https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages.json"
    assert seen["auth"].startswith("Basic ")
    assert seen["form"] == {"To": "+355691234567", "From": "+355690000001",
                            "Body": "Kodi 481516", "StatusCallback": CB}  # fmt: skip


def test_alphanumeric_sender_is_passed_as_is_and_messaging_service_replaces_from():
    forms = []

    def handler(request):
        forms.append(dict(parse_qsl(request.content.decode())))
        return httpx.Response(201, json={"sid": "SM1", "status": "queued"})

    provider(handler).send(req(sender="ACME"))
    assert forms[-1]["From"] == "ACME"
    provider(handler, messaging_service_sid="MG42").send(req())
    assert "From" not in forms[-1] and forms[-1]["MessagingServiceSid"] == "MG42"


@pytest.mark.parametrize(
    ("status", "body", "temporary", "code"),
    [
        (400, {"code": 21211, "message": "invalid To"}, False, "twilio_21211"),
        (400, {"code": 21610, "message": "unsubscribed"}, False, "twilio_21610"),
        (404, {}, False, "http_404"),
        (401, {"code": 20003}, True, "twilio_auth"),
        (403, {}, True, "twilio_auth"),
        (429, {"code": 20429}, True, "http_429"),
        (503, {}, True, "http_503"),
        (500, {}, False, "twilio_outcome_unknown"),
    ],
)
def test_error_classification(status, body, temporary, code):
    p = provider(lambda r: httpx.Response(status, json=body))
    with pytest.raises(ProviderError) as e:
        p.send(req())
    assert (e.value.temporary, e.value.code) == (temporary, code)


def test_connect_errors_are_temporary_but_ambiguous_ones_never_retry():
    def refuse(request):
        raise httpx.ConnectError("no route")

    def slow(request):
        raise httpx.ReadTimeout("timed out after sending")

    with pytest.raises(ProviderError) as e:
        provider(refuse).send(req())
    assert e.value.temporary is True
    with pytest.raises(ProviderError) as e:
        provider(slow).send(req())
    assert e.value.temporary is False and e.value.code == "twilio_outcome_unknown"


@pytest.mark.parametrize("body", [{}, {"status": "queued"}, "not json"])
def test_2xx_without_sid_is_not_retried(body):
    def handler(r):
        return httpx.Response(201, content=body if isinstance(body, str) else json.dumps(body))

    with pytest.raises(ProviderError) as e:
        provider(handler).send(req())
    assert e.value.temporary is False


def test_immediate_failure_status_is_permanent_with_error_code():
    p = provider(
        lambda r: httpx.Response(201, json={"sid": "SM9", "status": "failed", "error_code": 30007})
    )
    with pytest.raises(ProviderError) as e:
        p.send(req())
    assert (e.value.temporary, e.value.code) == (False, "twilio_30007")


# --- Callback-et -----------------------------------------------------------------


@pytest.fixture
def tw(raw_client_tw, monkeypatch):
    monkeypatch.setattr(settings, "twilio_auth_token", TOKEN)
    monkeypatch.setattr(settings, "public_base_url", "https://api.example.com")

    def post(path, fields, token=TOKEN, sign=True):
        params = list(fields.items())
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if sign:
            headers["X-Twilio-Signature"] = twilio_signature(
                token, "https://api.example.com" + path, params
            )
        return raw_client_tw.post(path, content=urlencode(params), headers=headers)

    return post


@pytest.fixture
def raw_client_tw():
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app())


def sent_via_twilio(db, sid="SMabc"):
    m = send(db, key="tw1")
    svc.process_one(db)  # fake → SENT
    db.expire_all()
    m = db.scalars(select(Message)).one()
    m.provider, m.provider_message_id = "twilio", sid
    db.commit()
    return m


def test_status_callback_requires_a_valid_signature(tw, db, world):  # noqa: F811
    sent_via_twilio(db)
    body = {"MessageSid": "SMabc", "MessageStatus": "delivered"}
    assert tw("/webhooks/twilio/status", body, sign=False).status_code == 401
    assert tw("/webhooks/twilio/status", body, token="wrong").status_code == 401
    db.expire_all()
    assert db.scalars(select(Message)).one().status == MessageStatus.SENT


def test_delivered_callback_captures_the_money(tw, db, world):  # noqa: F811
    w, _ = world
    sent_via_twilio(db)
    r = tw(
        "/webhooks/twilio/status",
        {"MessageSid": "SMabc", "MessageStatus": "delivered", "To": "+355691230003"},
    )
    assert (
        r.status_code == 200
        and r.headers["content-type"].startswith("text/xml")
        and "<Response/>" in r.text
    )
    db.expire_all()
    assert db.scalars(select(Message)).one().status == MessageStatus.DELIVERED
    assert wallets.balances(db, w.id) == (wallets.money("9.95"), wallets.money("0"))


def test_undelivered_callback_releases_the_hold_with_the_error_code(tw, db, world):  # noqa: F811
    w, _ = world
    sent_via_twilio(db)
    r = tw(
        "/webhooks/twilio/status",
        {"MessageSid": "SMabc", "MessageStatus": "undelivered", "ErrorCode": "30003"},
    )
    assert r.status_code == 200
    db.expire_all()
    m = db.scalars(select(Message)).one()
    assert m.status == MessageStatus.FAILED and m.error_code == "twilio_30003"
    assert wallets.balances(db, w.id) == (wallets.money("10"), wallets.money("0"))


def test_intermediate_late_and_unknown_statuses_are_handled_safely(tw, db, world):  # noqa: F811
    sent_via_twilio(db)
    assert (
        tw(
            "/webhooks/twilio/status", {"MessageSid": "SMabc", "MessageStatus": "sending"}
        ).status_code
        == 200
    )
    assert (
        tw(
            "/webhooks/twilio/status", {"MessageSid": "SMabc", "MessageStatus": "delivered"}
        ).status_code
        == 200
    )
    # një "failed" i vonuar pas "delivered" injorohet (200 që Twilio të mos e përsërisë)
    late = tw("/webhooks/twilio/status", {"MessageSid": "SMabc", "MessageStatus": "failed"})
    assert late.status_code == 200
    db.expire_all()
    assert db.scalars(select(Message)).one().status == MessageStatus.DELIVERED
    # SID i panjohur: 503 që Twilio të riprovojë (mund të mbërrijë para se SENT të ruhet)
    assert (
        tw(
            "/webhooks/twilio/status", {"MessageSid": "SMunknown", "MessageStatus": "delivered"}
        ).status_code
        == 503
    )
    assert tw("/webhooks/twilio/status", {"MessageStatus": "delivered"}).status_code == 422


NUM = "+355690000001"


@pytest.fixture
def numeric_sender(db, world):  # noqa: F811
    s = sender_ids.request(db, "c1", "AL", NUM)
    sender_ids.approve(db, s.id, "admin")
    db.commit()


def test_inbound_requires_signature_and_stores_the_message(tw, db, numeric_sender):
    body = {"MessageSid": "SMin1", "From": "+355691230003", "To": NUM, "Body": "Përshëndetje"}
    assert tw("/webhooks/twilio/inbound", body, sign=False).status_code == 401
    r = tw("/webhooks/twilio/inbound", body)
    assert r.status_code == 200 and "<Response/>" in r.text
    row = db.scalars(select(InboundMessage)).one()
    assert (row.provider, row.provider_message_id, row.text) == ("twilio", "SMin1", "Përshëndetje")
    assert row.from_number == "355691230003"
    tw("/webhooks/twilio/inbound", body)  # Twilio mund ta ridërgojë: pa dublikim
    assert len(db.scalars(select(InboundMessage)).all()) == 1


def test_inbound_stop_opts_the_person_out(tw, db, numeric_sender):
    consent.record(db, "c1", "sms", "+355691230003", "opt_in", "x", "form", "u", "evidence")
    db.commit()
    tw(
        "/webhooks/twilio/inbound",
        {"MessageSid": "SMin2", "From": "+355691230003", "To": NUM, "Body": "STOP"},
    )
    db.expire_all()
    assert (
        consent.check(db, "c1", "sms", "+355691230003", "transactional").reason
        == "blocked:stop_keyword"
    )


def test_provider_is_registered_only_when_configured(monkeypatch):
    from app import providers

    monkeypatch.setattr(providers, "_registry", dict(providers._registry))
    monkeypatch.setattr(settings, "twilio_account_sid", "")
    providers.register_configured()
    assert "twilio" not in providers._registry
    monkeypatch.setattr(settings, "twilio_account_sid", "ACx")
    monkeypatch.setattr(settings, "twilio_auth_token", "tok")
    providers.register_configured()
    assert providers._registry["twilio"].name == "twilio"
