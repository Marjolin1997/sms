"""M11-I/F-07A: matrica e kontratës provider/DLR/rrugëzim/siguri. Vetëm provider të rremë dhe transport të simuluar
(httpx.MockTransport): asnjë rrjet, asnjë kredencial real. Çdo pohim përshkruan sjelljen REALE të kodit (jo një dizajn të ri)."""

import hashlib
import hmac
import inspect
import json
import logging
import secrets
import smtplib
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import httpx
import pytest
from fastapi.testclient import TestClient

import app.providers as providers
from app.api import webhooks
from app.core.config import settings
from app.main import create_app
from app.models.sending import AccountPlan, DlrReceipt, Message, MessageEvent, MessageStatus, Route
from app.providers import ProviderError, SendRequest, SendResult
from app.providers.email import EmailRequest, SmtpEmailProvider
from app.providers.http import HttpProvider
from app.providers.twilio import TwilioProvider, verify_twilio_signature
from app.services import messages as svc
from app.services import rates, sender_ids, switches
from app.services import wallet as wallets
from tests.test_pipeline import OK, PERM, TEMP, fake, send, world  # noqa: F401

REQ = SendRequest("ref-1", "ACME", "355691230003", "hi", "GSM-7", 1)
SECRET = secrets.token_hex(16)  # sentinelë sintetike e rastësishme për çdo ekzekutim
KEY = "k" + secrets.token_hex(12)
PRICE = D("0.05")


def _t(i: int = 0) -> datetime:
    return datetime.now(UTC) + timedelta(days=i + 1)


# --- ndihmës ----------------------------------------------------------------------------------------


def http_provider(handler) -> HttpProvider:
    return HttpProvider(
        "fake",
        "https://provider.invalid/send",
        KEY,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def twilio_provider(handler) -> TwilioProvider:
    return TwilioProvider(
        "ACf07", "f07-twilio-token-not-real", "https://platform.invalid/webhooks/twilio/status",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )  # fmt: skip


def reply(status: int, body=None, *, content: bytes | None = None):
    def handler(req):
        return (
            httpx.Response(status, content=content)
            if content is not None
            else httpx.Response(status, json=body)
        )

    return handler


def boom(exc: Exception):
    def handler(req):
        raise exc

    return handler


def outcome(provider, req=REQ):
    try:
        return ("ok", provider.send(req).provider_message_id)
    except ProviderError as e:
        return (e.code, e.temporary, e.ambiguous)


class Scripted:
    """Provider i shkruar hap pas hapi (përjashtim ⇒ ngrihet; ndryshe SendResult); pas skriptit pranon."""

    name = "fake"
    idempotent_by_reference = False

    def __init__(self, *steps):
        self.steps, self.calls = list(steps), []

    def send(self, req):
        self.calls.append(req)
        step = self.steps.pop(0) if self.steps else SendResult(f"s-{len(self.calls)}")
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def prov(monkeypatch):
    def install(p, name="fake"):
        monkeypatch.setitem(providers._registry, name, p)
        return p

    return install


@pytest.fixture
def wh(monkeypatch):
    monkeypatch.setattr(settings, "dlr_secrets", {"fake": SECRET})
    return TestClient(create_app())


def sign(raw: bytes, secret: str = SECRET) -> dict[str, str]:
    return {"X-Signature": "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()}


def dlr(wh, body: dict, secret: str = SECRET, headers: dict | None = None):
    raw = json.dumps(body).encode()
    return wh.post(
        "/webhooks/dlr/fake", content=raw, headers={**sign(raw, secret), **(headers or {})}
    )


def held(db, w) -> tuple[D, D]:
    db.expire_all()
    return wallets.balances(db, w.id)


# --- 1. taksonomia e dështimeve: adapteri HTTP ------------------------------------------------------

HTTP_TABLE = [
    (reply(200, {"id": "p1"}), ("ok", "p1")),
    *[(reply(s, {}), (f"http_{s}", False, False)) for s in (400, 401, 403, 404, 409, 422)],
    *[(reply(s, {}), (f"http_{s}", True, False)) for s in (408, 425, 429)],
    *[(reply(s, {}), (f"http_{s}", True, True)) for s in (500, 502, 503, 504)],
    (reply(200, content=b"<html>not json"), ("bad_response", True, True)),
    (reply(200, {}), ("bad_response", True, True)),
    (reply(200, {"id": ""}), ("bad_response", True, True)),
    (reply(200, {"id": 5}), ("bad_response", True, True)),
    (reply(200, []), ("bad_response", True, True)),
    (reply(302, content=b""), ("bad_response", True, True)),
    (boom(httpx.ConnectError("dns/connect")), ("network:ConnectError", True, False)),
    (boom(httpx.ConnectTimeout("connect timeout")), ("network:ConnectTimeout", True, False)),
    (boom(httpx.PoolTimeout("pool")), ("network:PoolTimeout", True, False)),
    (boom(httpx.ReadTimeout("slow response")), ("network:ReadTimeout", True, True)),
    (boom(httpx.RemoteProtocolError("reset")), ("network:RemoteProtocolError", True, True)),
    (boom(httpx.ReadError("connection reset")), ("network:ReadError", True, True)),
]


@pytest.mark.parametrize("handler,expected", HTTP_TABLE)
def test_http_adapter_failure_taxonomy(handler, expected):
    assert outcome(http_provider(handler)) == expected


def test_http_adapter_is_not_idempotent_and_sends_the_reference_anyway():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"id": "p9"})

    p = http_provider(handler)
    assert providers.base.is_idempotent(p) is False
    p.send(REQ)
    assert seen[0]["reference"] == "ref-1" and seen[0]["to"] == REQ.destination


# --- 2. taksonomia e dështimeve: Twilio -------------------------------------------------------------

TWILIO_TABLE = [
    (reply(201, {"sid": "SM1", "status": "queued"}), ("ok", "SM1")),
    (reply(401, {}), ("twilio_auth", False, False)),  # M11-J/G-04: përhershëm
    (reply(403, {}), ("twilio_forbidden", False, False)),
    (reply(403, {"code": 20003}), ("twilio_20003", False, False)),
    *[(reply(s, {}), (f"http_{s}", True, False)) for s in (429, 502, 503, 504)],
    (reply(500, {}), ("twilio_outcome_unknown", False, True)),
    (reply(400, {"code": 21211}), ("twilio_21211", False, False)),
    (reply(400, {"code": 21408}), ("twilio_21408", False, False)),
    (reply(404, {}), ("http_404", False, False)),
    (reply(409, {}), ("http_409", False, False)),
    (reply(400, content=b"<html>"), ("http_400", False, False)),
    (
        reply(201, {"sid": "SM2", "status": "failed", "error_code": 30003}),
        ("twilio_30003", False, False),
    ),
    (reply(201, content=b"not json"), ("twilio_outcome_unknown", False, True)),
    (reply(201, {}), ("twilio_outcome_unknown", False, True)),
    (boom(httpx.ConnectError("dns")), ("network:ConnectError", True, False)),
    (boom(httpx.ReadTimeout("slow")), ("twilio_outcome_unknown", False, True)),
]


@pytest.mark.parametrize("handler,expected", TWILIO_TABLE)
def test_twilio_adapter_failure_taxonomy(handler, expected):
    assert outcome(twilio_provider(handler)) == expected


# --- 3. taksonomia e dështimeve: SMTP ---------------------------------------------------------------


class _Smtp:
    def __init__(self, exc=None, on_connect=None):
        self.exc, self.on_connect = exc, on_connect

    def __call__(self, *a, **k):
        if self.on_connect:
            raise self.on_connect
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, **k):
        pass

    def login(self, *a):
        pass

    def sendmail(self, *a):
        if self.exc:
            raise self.exc


EMAIL_REQ = EmailRequest("ref", "<m@x>", "from@x.org", "to@y.org", b"raw")
SMTP_TABLE = [
    (_Smtp(), ("ok", "<m@x>")),
    (
        _Smtp(on_connect=ConnectionRefusedError()),
        ("smtp_connect:ConnectionRefusedError", True, False),
    ),
    (
        _Smtp(smtplib.SMTPRecipientsRefused({"to@y.org": (550, b"no")})),
        ("smtp_recipient_refused", False, False),
    ),
    (
        _Smtp(smtplib.SMTPRecipientsRefused({"to@y.org": (450, b"later")})),
        ("smtp_recipient_refused", True, False),
    ),
    (_Smtp(smtplib.SMTPResponseException(451, b"try later")), ("smtp_451", True, False)),
    (_Smtp(smtplib.SMTPResponseException(550, b"rejected")), ("smtp_550", False, False)),
    (
        _Smtp(smtplib.SMTPServerDisconnected("lost")),
        ("smtp_error:SMTPServerDisconnected", True, True),
    ),
]


@pytest.mark.parametrize("fake_smtp,expected", SMTP_TABLE)
def test_smtp_adapter_failure_taxonomy(monkeypatch, fake_smtp, expected):
    monkeypatch.setattr(smtplib, "SMTP", fake_smtp)
    p = SmtpEmailProvider("smtp", "smtp.invalid", 587, "u", "p")
    assert outcome(p, EMAIL_REQ) == expected
    assert providers.base.is_idempotent(p) is False


# --- 4. pipeline: rezultati i provider-it → gjendja + paratë ----------------------------------------

PIPELINE = [
    (reply(401, {}), MessageStatus.FAILED, False),  # HTTP 4xx i përhershëm: dështim + release
    (reply(429, {}), MessageStatus.QUEUED, True),  # definitiv i përkohshëm: retry, hold mbahet
    (boom(httpx.ConnectError("x")), MessageStatus.QUEUED, True),
    (reply(503, {}), MessageStatus.UNKNOWN, True),  # i paqartë + jo-idempotent: UNKNOWN
    (boom(httpx.ReadTimeout("x")), MessageStatus.UNKNOWN, True),
    (reply(200, content=b"junk"), MessageStatus.UNKNOWN, True),  # 2xx i palexueshëm
    (reply(200, {}), MessageStatus.UNKNOWN, True),  # ID e munguar
    (reply(200, {"id": "p-ok"}), MessageStatus.SENT, True),
]


@pytest.mark.parametrize("handler,status,money_held", PIPELINE)
def test_provider_outcome_maps_to_state_and_money_with_exactly_one_call(
    db, world, prov, handler, status, money_held
):  # noqa: F811
    calls = []
    p = http_provider(lambda r: (calls.append(r), handler(r))[1])
    prov(p)
    w, _ = world
    m = send(db)
    svc.process_one(db)
    db.refresh(m)
    assert m.status == status and len(calls) == 1
    assert held(db, w) == ((D("9.95"), PRICE) if money_held else (D("10"), D("0")))


def test_retry_schedule_and_exhaustion_release_the_hold_after_five_calls(db, world, prov):  # noqa: F811
    w, _ = world
    p = prov(Scripted(*[ProviderError("http_429", temporary=True) for _ in range(5)]))
    m = send(db)
    delays = []
    for i in range(5):
        now = _t(i)
        svc.process_one(db, now)
        db.refresh(m)
        if m.status == MessageStatus.QUEUED:
            delays.append(int((rates.as_utc(m.next_attempt_at) - now).total_seconds()))
    assert delays == [30, 60, 120, 240]
    assert m.status == MessageStatus.FAILED and m.error_code == "http_429" and len(p.calls) == 5
    assert held(db, w) == (D("10"), D("0"))
    assert svc.process_one(db, _t(9)) is None and len(p.calls) == 5  # terminal: s'riprovohet më


def test_bad_twilio_credentials_fail_fast_with_release_and_no_retries(db, world, prov, caplog):  # noqa: F811
    """401/403 trajtohen si të përkohshme (konfigurim i rregullueshëm): P1 te matrica e alarmeve, jo heshtje."""
    calls = []
    prov(
        twilio_provider(
            lambda r: (
                calls.append(r),
                httpx.Response(401, json={"message": "bad f07-twilio-token-not-real"}),
            )[1]
        )
    )
    w, _ = world
    m = send(db)
    for i in range(5):
        svc.process_one(db, _t(i))
    db.refresh(m)
    assert len(calls) == 1 and m.status == MessageStatus.FAILED and m.error_code == "twilio_auth"
    assert "bad f07-twilio-token-not-real" not in (m.error_code or "")
    assert held(db, w) == (D("10"), D("0"))


def test_provider_secrets_never_reach_state_events_logs_or_repr(db, world, prov, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    prov(http_provider(reply(500, {"error": f"echo {KEY} Authorization: Bearer {KEY}"})))
    m = send(db)
    svc.process_one(db)
    db.refresh(m)
    events = " ".join(e.detail or "" for e in db.query(MessageEvent).all())
    assert KEY not in (m.error_code or "") and KEY not in events and KEY not in caplog.text
    assert KEY not in repr(providers._registry["fake"]) and m.status == MessageStatus.UNKNOWN


# --- 5. rrugëzim, fallback, sender/shtet, rate-limit, kill switch -----------------------------------


def test_no_automatic_route_fallback_and_queued_messages_stay_bound_to_their_provider(
    db, world, prov
):  # noqa: F811
    bad = prov(Scripted(*[ProviderError("fake_temporary", temporary=True)] * 5))
    good = prov(Scripted(), name="second")
    db.add(Route(prefix="355", country="AL", provider="second", priority=50))
    db.commit()
    m = send(db)
    assert m.provider == "fake"  # prioriteti më i lartë fiton
    for i in range(5):
        svc.process_one(db, _t(i))
    db.refresh(m)
    assert (
        m.status == MessageStatus.FAILED and len(bad.calls) == 5 and good.calls == []
    )  # asnjë fallback
    db.add(Route(prefix="355", country="AL", provider="third", priority=1))
    queued = send(db, key="q1")  # në radhë për "fake"
    route = db.query(Route).filter_by(provider="fake").one()
    route.enabled = False
    db.commit()
    later = send(db, key="q2", to=OK)
    db.refresh(queued)
    assert (queued.provider, later.provider) == (
        "fake",
        "second",
    )  # çaktivizimi s'e ridrejton të radhitur
    assert queued.status == MessageStatus.QUEUED


def test_dispatch_switch_pauses_the_queue_without_touching_state_or_money(db, world, prov):  # noqa: F811
    w, _ = world
    p = prov(Scripted())
    m = send(db)
    switches.set_switch(db, switches.DISPATCH, False, "ops", "rollback drill")
    db.commit()
    assert svc.process_one(db) is None
    db.refresh(m)
    assert m.status == MessageStatus.QUEUED and m.attempts == 0 and p.calls == []
    assert held(db, w) == (D("9.95"), PRICE)
    switches.set_switch(db, switches.DISPATCH, True, "ops", None)
    db.commit()
    svc.process_one(db)
    db.refresh(m)
    assert m.status == MessageStatus.SENT and len(p.calls) == 1


def test_removing_the_provider_configuration_fails_permanently_with_release(db, world, monkeypatch):  # noqa: F811
    w, _ = world
    m = send(db)
    monkeypatch.delitem(providers._registry, "fake")
    svc.process_one(db)
    db.refresh(m)
    assert m.status == MessageStatus.FAILED and m.error_code == "unknown_provider"
    assert held(db, w) == (D("10"), D("0"))


def test_unknown_outcome_is_never_resent_after_a_route_change(db, world, prov):  # noqa: F811
    w, _ = world
    p = prov(Scripted(ProviderError("network:ReadTimeout", temporary=True, ambiguous=True)))
    m = send(db)
    svc.process_one(db)
    db.refresh(m)
    assert m.status == MessageStatus.UNKNOWN
    db.query(Route).filter_by(provider="fake").one().enabled = False
    db.add(Route(prefix="355", country="AL", provider="second"))
    db.commit()
    prov(Scripted(), name="second")
    for i in range(3):
        svc.process_one(db, _t(i))
        svc.recover_stuck(db, lease=timedelta(0), now=_t(i))
    db.commit()
    db.refresh(m)
    assert (
        len(p.calls) == 1
        and m.status == MessageStatus.UNKNOWN
        and held(db, w) == (D("9.95"), PRICE)
    )


def test_unapproved_sender_wrong_country_and_missing_route_are_refused_before_money_and_provider(
    db, world, prov
):  # noqa: F811
    p = prov(Scripted())
    w, _ = world
    db.add(Route(prefix="1", country="US", provider="fake"))
    db.commit()
    for dest, sender in ((OK, "NOPE"), ("+12025550123", "ACME"), ("+999990000003", "ACME")):
        with pytest.raises((sender_ids.SenderNotAllowed, svc.NoRoute)):
            svc.submit(db, "c1", f"k-{dest}-{sender}", dest, sender, text="x")
        db.rollback()
    assert db.query(Message).count() == 0 and p.calls == [] and held(db, w) == (D("10"), D("0"))


def test_per_account_rate_limit_refuses_before_any_hold_or_provider_call(db, world, prov):  # noqa: F811
    p = prov(Scripted())
    w, _ = world
    plan = db.query(AccountPlan).filter_by(owner_ref="c1").one()
    plan.rate_limit_per_min = 2
    db.commit()
    send(db, key="r1")
    send(db, key="r2")
    with pytest.raises(svc.RateLimited):
        svc.submit(db, "c1", "r3", OK, "ACME", text="x")
    db.rollback()
    assert (
        db.query(Message).count() == 2 and held(db, w) == (D("9.90"), D("0.10")) and p.calls == []
    )


# --- 6. DLR: replay, siguri, para ------------------------------------------------------------------


def _sent(db, prov, *steps):
    p = prov(Scripted(*steps))
    m = send(db)
    svc.process_one(db)
    db.refresh(m)
    return p, m


def test_replayed_signed_callback_is_idempotent_and_leaves_history_and_money_alone(
    db, world, prov, wh
):  # noqa: F811
    w, _ = world
    _, m = _sent(db, prov)
    body = {"provider_message_id": m.provider_message_id, "status": "delivered"}
    assert dlr(wh, body).status_code == 200
    events, money = db.query(MessageEvent).count(), held(db, w)
    assert all(dlr(wh, body).status_code == 200 for _ in range(3))
    old_failure = {"provider_message_id": m.provider_message_id, "status": "failed", "code": "late"}
    assert dlr(wh, old_failure).status_code == 409  # replay i një DLR të vjetër kontradiktor
    db.expire_all()
    assert db.query(MessageEvent).count() == events and held(db, w) == money
    assert db.get(Message, m.id).status == MessageStatus.DELIVERED
    # M11-J/G-05: replay-et nuk shtojnë rreshta; numërohen në `seen_count` (1 origjinali + 3 replay)
    applied = db.query(DlrReceipt).filter_by(outcome="applied").one()
    assert applied.seen_count == 4
    assert db.query(DlrReceipt).filter_by(outcome="conflict").one().seen_count == 1


def test_forwarded_headers_admin_key_and_bearer_never_replace_the_signature(db, world, prov, wh):  # noqa: F811
    _, m = _sent(db, prov)
    raw = json.dumps({"provider_message_id": m.provider_message_id, "status": "delivered"}).encode()
    for h in (
        {"X-Admin-Key": "test-key"},
        {"X-Forwarded-For": "127.0.0.1"},
        {"Authorization": "Bearer x"},
        {"X-Signature": ""},
    ):
        r = wh.post("/webhooks/dlr/fake", content=raw, headers=h)
        assert r.status_code == 401 and SECRET not in r.text
    db.expire_all()
    assert db.get(Message, m.id).status == MessageStatus.SENT and db.query(DlrReceipt).count() == 0


def test_stored_receipts_never_contain_secrets_or_signatures(db, world, prov, wh):  # noqa: F811
    _, m = _sent(db, prov)
    dlr(wh, {"provider_message_id": m.provider_message_id, "status": "delivered"})
    raw = " ".join(r.raw_body for r in db.query(DlrReceipt).all())
    assert SECRET not in raw and "sha256=" not in raw


def test_signature_comparisons_are_constant_time_and_fail_closed():
    assert "compare_digest" in inspect.getsource(webhooks.verify_signature)
    assert "compare_digest" in inspect.getsource(verify_twilio_signature)
    assert webhooks.verify_signature(SECRET, b"x", "sha256=") is False
    assert verify_twilio_signature("", "https://u", [], "x") is False
    assert verify_twilio_signature("tok", "https://u", [], "") is False


def test_provider_registration_reads_only_settings_never_literals(monkeypatch):
    monkeypatch.setattr(settings, "http_provider_url", "https://provider.invalid/send")
    monkeypatch.setattr(settings, "http_provider_key", KEY)
    monkeypatch.setattr(settings, "http_provider_name", "f07-http")
    monkeypatch.setattr(providers, "_registry", dict(providers._registry))
    providers.register_configured()
    assert "f07-http" in providers._registry and KEY not in repr(providers._registry["f07-http"])
    src = inspect.getsource(providers.http) + inspect.getsource(providers.twilio)
    assert "Bearer " in src and KEY not in src


def test_a_provider_that_returns_a_duplicate_id_is_prevented_and_fails_safe(db, world, prov):  # noqa: F811
    """M11-J/G-01: id e dyfishtë nuk i jepet mesazhit të dytë: ai bëhet UNKNOWN (hold i mbajtur, pa ridërgim)."""
    w, _ = world
    p = prov(Scripted(SendResult("dup-1"), SendResult("dup-1")))
    a, b = send(db, key="a"), send(db, key="b")
    svc.process_one(db)
    svc.process_one(db)
    db.refresh(a), db.refresh(b)
    assert (a.status, a.provider_message_id) == (MessageStatus.SENT, "dup-1")
    assert (b.status, b.provider_message_id) == (MessageStatus.UNKNOWN, None)
    assert b.error_code == svc.DUPLICATE_PROVIDER_ID and len(p.calls) == 2
    svc.process_one(db), svc.recover_stuck(db, lease=timedelta(0))
    db.commit()
    assert len(p.calls) == 2  # asnjë ridërgim
    svc.apply_dlr(db, "fake", "dup-1", True)
    db.commit()
    db.refresh(a), db.refresh(b)
    assert (a.status, b.status) == (MessageStatus.DELIVERED, MessageStatus.UNKNOWN)
    assert held(db, w) == (D("9.90"), PRICE)  # a u kap një herë; hold-i i b mbetet
    assert wallets.verify_wallet(db, w.id)


def test_money_moves_once_across_every_resolution_path(db, world, prov, wh):  # noqa: F811
    w, _ = world
    _, m = _sent(db, prov)
    body = {"provider_message_id": m.provider_message_id, "status": "delivered"}
    dlr(wh, body)
    after_first = held(db, w)
    n_entries = db.execute(
        wallets.select(wallets.func.count()).select_from(wallets.LedgerEntry)
    ).scalar()
    dlr(wh, body)
    svc.apply_dlr(db, "fake", m.provider_message_id, True)
    svc.resolve_unknown(
        db, m.public_id, "billable_delivered", actor="ops", role="superadmin", reason="replay"
    )
    with pytest.raises(wallets.Conflict):
        svc.resolve_unknown(
            db,
            m.public_id,
            "non_billable_failed",
            actor="ops",
            role="superadmin",
            reason="contradiction",
        )
    db.rollback()
    assert held(db, w) == after_first == (D("9.95"), D("0"))
    assert (
        db.execute(wallets.select(wallets.func.count()).select_from(wallets.LedgerEntry)).scalar()
        == n_entries
    )
    assert wallets.verify_wallet(db, w.id)


def test_late_dlr_after_expiry_is_refused_and_the_customer_stays_uncharged(db, world, prov):  # noqa: F811
    w, _ = world
    _, m = _sent(db, prov)
    assert svc.expire_stale(db, timedelta(hours=72), now=_t(5)) == 1
    db.commit()
    with pytest.raises(wallets.Conflict):
        svc.apply_dlr(db, "fake", m.provider_message_id, True)
    db.rollback()
    db.refresh(m)
    assert (
        m.status == MessageStatus.FAILED
        and m.error_code == "dlr_timeout"
        and held(db, w) == (D("10"), D("0"))
    )


def test_hard_failure_terminal_codes_use_the_fake_provider_contract(db, world, fake):  # noqa: F811
    w, _ = world
    perm, temp = send(db, key="p", to=PERM), send(db, key="t", to=TEMP)
    svc.process_one(db)
    svc.process_one(db)
    db.refresh(perm), db.refresh(temp)
    assert perm.status == MessageStatus.FAILED and perm.error_code == "fake_rejected"
    assert temp.status == MessageStatus.QUEUED and temp.error_code == "fake_temporary"
    assert held(db, w) == (D("9.95"), PRICE)
