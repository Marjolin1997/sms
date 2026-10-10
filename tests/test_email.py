import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import dkim
import pytest

import app.providers as providers
from app.core.config import settings
from app.models.email import (
    DomainStatus,
    Email,
    EmailDomain,
    EmailEvent,
    EmailEventImmutableError,
    EmailStatus,
)
from app.providers import ProviderError
from app.providers.email import FakeEmailProvider
from app.services import consent, email_domains, email_mime, emails, switches
from app.services import dns_check as dns
from app.services.wallet import Conflict, NotFound
from tests.test_pipeline import world  # noqa: F401

OWNER = "c1"
FROM = "news@example.com"
TO = "ana@customer.org"


class FakeDns:
    def __init__(self):
        self.records: dict[str, list[str]] = {}
        self.fail = False

    def txt(self, name):
        if self.fail:
            raise dns.DnsError("timeout")
        return self.records.get(name, [])

    def publish(self, d, spf=True, dkim_ok=True):
        for r in email_domains.dns_records(d):
            if r["name"] == d.domain and not spf:
                continue
            if r["name"].endswith(f"_domainkey.{d.domain}") and not dkim_ok:
                continue
            self.records.setdefault(r["name"], []).append(r["value"])


@pytest.fixture(autouse=True)
def fake_dns():
    old = dns.get_resolver()
    f = FakeDns()
    dns.set_resolver(f)
    yield f
    dns.set_resolver(old)


@pytest.fixture(autouse=True)
def fake_email_provider():
    p = FakeEmailProvider()
    providers._email_registry["fake"] = p
    return p


@pytest.fixture
def verified(db, world, fake_dns):  # noqa: F811
    d = email_domains.create(db, OWNER, "example.com")
    fake_dns.publish(d)
    email_domains.verify(db, OWNER, d.id)
    db.commit()
    return d


def send(db, key="e1", to=TO, **kw):
    kw = {"subject": "Hello", "text": "Body text"} | kw
    e = emails.submit(db, OWNER, key, kw.pop("from_email", FROM), to, **kw)
    db.commit()
    return e


def opt_in(db, addr=TO):
    consent.record(db, OWNER, "email", addr, "opt_in", "x", "form", "u", "evidence")
    db.commit()


# --- Domene ---------------------------------------------------------------------


def test_domain_keys_are_encrypted_and_records_are_published(db, world):  # noqa: F811
    d = email_domains.create(db, OWNER, "Example.COM.")
    db.commit()
    assert d.domain == "example.com"
    assert "PRIVATE KEY" not in d.dkim_private_key_enc  # Fernet, jo PEM
    assert b"BEGIN PRIVATE KEY" in email_domains.decrypt_private_key(d)
    names = {r["name"] for r in email_domains.dns_records(d)}
    assert names == {"sms1._domainkey.example.com", "example.com", "_dmarc.example.com"}
    with pytest.raises(Conflict):
        email_domains.create(db, OWNER, "example.com")
    for bad in ("localhost", "a b.com", "-x.com", "x..com", "x.c", "http://x.com"):
        with pytest.raises(email_domains.InvalidDomain):
            email_domains.create(db, OWNER, bad)


def test_wrong_secrets_key_cannot_decrypt(db, world, monkeypatch):  # noqa: F811
    d = email_domains.create(db, OWNER, "example.com")
    from cryptography.fernet import Fernet

    monkeypatch.setattr(settings, "secrets_key", Fernet.generate_key().decode())
    with pytest.raises(RuntimeError):
        email_domains.decrypt_private_key(d)
    monkeypatch.setattr(settings, "secrets_key", "")
    with pytest.raises(RuntimeError):
        email_domains.decrypt_private_key(d)


def test_verification_needs_dkim_and_spf(db, world, fake_dns):  # noqa: F811
    d = email_domains.create(db, OWNER, "example.com")
    assert email_domains.verify(db, OWNER, d.id).status == DomainStatus.PENDING
    fake_dns.publish(d, spf=False)
    r = email_domains.verify(db, OWNER, d.id)
    assert (r.dkim_ok, r.spf_ok, r.status) == (True, False, DomainStatus.PENDING)
    fake_dns.records["example.com"] = ["v=spf1 include:other.net ~all"]
    assert not email_domains.verify(db, OWNER, d.id).spf_ok
    fake_dns.records["example.com"] = [f"v=spf1 ip4:1.2.3.4 include:{settings.spf_include} ~all"]
    r = email_domains.verify(db, OWNER, d.id)
    assert r.status == DomainStatus.VERIFIED and r.verified_at and r.dmarc_ok


def test_wrong_dkim_key_does_not_verify(db, world, fake_dns):  # noqa: F811
    d = email_domains.create(db, OWNER, "example.com")
    fake_dns.publish(d)
    other = email_domains.create(db, OWNER, "other.com")
    fake_dns.records["sms1._domainkey.example.com"] = [f"v=DKIM1; k=rsa; p={other.dkim_public_key}"]
    assert email_domains.verify(db, OWNER, d.id).status == DomainStatus.PENDING


def test_dns_failure_is_not_a_verdict(db, world, fake_dns):  # noqa: F811
    d = email_domains.create(db, OWNER, "example.com")
    fake_dns.fail = True
    with pytest.raises(Conflict):
        email_domains.verify(db, OWNER, d.id)


def test_dns_removed_revokes_verification(db, verified, fake_dns):  # noqa: F811
    fake_dns.records.clear()
    assert email_domains.verify(db, OWNER, verified.id).status == DomainStatus.PENDING
    with pytest.raises(emails.SenderDomainNotVerified):
        emails.submit(db, OWNER, "x", FROM, TO, "s", "t")


def test_domain_is_verified_by_one_owner_only(db, world, fake_dns):  # noqa: F811
    a = email_domains.create(db, OWNER, "example.com")
    fake_dns.publish(a)
    email_domains.verify(db, OWNER, a.id)
    db.commit()
    b = email_domains.create(db, "c2", "example.com")
    fake_dns.publish(b)
    with pytest.raises(Conflict):
        email_domains.verify(db, "c2", b.id)
    db.rollback()
    with pytest.raises(NotFound):  # domen i tenant-it tjetër
        email_domains.verify(db, "c2", a.id)


# --- Dërgimi --------------------------------------------------------------------


def test_submit_rules(db, verified):  # noqa: F811
    with pytest.raises(emails.SenderDomainNotVerified):
        emails.submit(db, OWNER, "k", "boss@other.com", TO, "s", "t")
    from app.models.sending import AccountPlan

    db.add(AccountPlan(owner_ref="c2", rate_card_id=db.query(AccountPlan).one().rate_card_id))
    db.commit()
    with pytest.raises(emails.SenderDomainNotVerified):  # domen i një klienti tjetër
        emails.submit(db, "c2", "k", FROM, TO, "s", "t")
    for kw in (
        {"subject": "Hi\r\nBcc: evil@x.com"},
        {"subject": ""},
        {"text": "  "},
        {"from_name": "A\nB"},
        {"to": "nope"},
        {"category": "spam"},
    ):
        args = {"to": TO, "subject": "s", "text": "t"} | kw
        with pytest.raises(emails.InvalidEmail):
            emails.submit(
                db, OWNER, "k", FROM, args.pop("to"), args.pop("subject"), args.pop("text"), **args
            )


def test_idempotent_submit(db, verified):  # noqa: F811
    a, b = send(db), send(db)
    assert a.id == b.id and db.query(Email).count() == 1
    with pytest.raises(Conflict):
        send(db, subject="Different")


def test_marketing_requires_consent_and_blocks_after_hard_events(db, verified):  # noqa: F811
    with pytest.raises(consent.RecipientSuppressed):
        send(db, key="m1", category="marketing")
    opt_in(db)
    assert send(db, key="m2", category="marketing").category == "marketing"
    assert send(db, key="t1").status == EmailStatus.QUEUED  # transactional nuk kërkon opt-in


def test_kill_switch_rate_limit_and_disabled_account(db, verified):  # noqa: F811
    from app.models.sending import AccountPlan
    from app.services import messages

    switches.set_switch(db, switches.SUBMIT, False, "ops", "incident")
    with pytest.raises(messages.SendingPaused):
        send(db, key="k1")
    switches.set_switch(db, switches.SUBMIT, True, "ops", None)
    plan = db.query(AccountPlan).one()
    plan.email_rate_limit_per_min = 2
    db.commit()
    send(db, key="a"), send(db, key="b")
    with pytest.raises(messages.RateLimited):
        emails.submit(db, OWNER, "c", FROM, TO, "s", "t")
    assert send(db, key="a").idempotency_key == "a"  # replay lejohet
    plan.enabled = False
    with pytest.raises(messages.AccountDisabled):
        emails.submit(
            db, OWNER, "d", FROM, TO, "s", "t", now=datetime.now(UTC) + timedelta(hours=1)
        )


# --- MIME dhe DKIM ----------------------------------------------------------------


def test_signed_message_verifies_and_has_unsubscribe(db, verified, fake_email_provider):  # noqa: F811
    opt_in(db)
    e = send(db, key="m", category="marketing", html="<html><body><p>Hi</p></body></html>",
             from_name="Acme News")  # fmt: skip
    emails.process_one(db, datetime.now(UTC) + timedelta(seconds=1))
    assert e.status == EmailStatus.SENT and e.provider_message_id == f"<{e.public_id}@example.com>"
    raw = fake_email_provider.calls[0].raw
    pub = base64.b64decode(verified.dkim_public_key)
    txt = b"v=DKIM1; k=rsa; p=" + base64.b64encode(pub)
    assert dkim.verify(raw, dnsfunc=lambda name, timeout=5: txt)
    assert not dkim.verify(raw.replace(b"Hello", b"Hacked"), dnsfunc=lambda name, timeout=5: txt)
    head, _, body = raw.partition(b"\r\n\r\n")
    h = __import__("re").sub(r"\r\n[ \t]+", " ", head.decode())  # zhvillo headers e palosur
    assert "=?utf-8?" not in h  # List-Unsubscribe duhet të jetë URL e papërpunuar
    assert "List-Unsubscribe: <" in h and "List-Unsubscribe-Post: List-Unsubscribe=One-Click" in h
    assert "From: Acme News <news@example.com>" in h and "multipart/alternative" in h
    assert b"/u/" in body and b"Unsubscribe" in body
    assert body.count(b"Unsubscribe</a></p></body>") == 1  # footer para </body>
    assert TO.encode() not in raw.split(b"/u/")[1][:120]  # token pa adresë


def test_transactional_has_no_unsubscribe_footer(db, verified, fake_email_provider):  # noqa: F811
    send(db)
    emails.process_one(db, datetime.now(UTC) + timedelta(seconds=1))
    raw = fake_email_provider.calls[0].raw
    assert b"List-Unsubscribe" not in raw and b"/u/" not in raw


def test_header_injection_blocked_in_mime():
    with pytest.raises(email_mime.UnsafeHeader):
        email_mime.clean_header("a\r\nBcc: x", "subject")


# --- Retries ---------------------------------------------------------------------


def test_temporary_error_retries_then_fails(db, verified):  # noqa: F811
    e = send(db, to="temp@customer.org")
    t = datetime.now(UTC) + timedelta(seconds=1)
    for attempt in range(1, emails.MAX_ATTEMPTS + 1):
        assert emails.process_one(db, t) is e
        if attempt < emails.MAX_ATTEMPTS:
            assert e.status == EmailStatus.QUEUED
            assert emails.process_one(db, t) is None  # backoff
            t += timedelta(seconds=emails.BACKOFF_SECONDS * 2 ** (attempt - 1))
    assert e.status == EmailStatus.FAILED and e.error_code == "fake_temporary"


def test_permanent_error_and_unknown_provider(db, verified):  # noqa: F811
    e = send(db, to="reject@customer.org")
    emails.process_one(db, datetime.now(UTC) + timedelta(seconds=1))
    assert e.status == EmailStatus.FAILED and e.attempts == 1
    e2 = send(db, key="g")
    e2.provider = "ghost"
    db.commit()
    emails.process_one(db, datetime.now(UTC) + timedelta(seconds=1))
    assert e2.error_code == "unknown_provider"
    with pytest.raises(ProviderError):
        providers.get_email_provider("ghost")


def test_dispatch_switch_pauses_sending(db, verified):  # noqa: F811
    e = send(db)
    switches.set_switch(db, switches.DISPATCH, False, "ops", "provider down")
    db.commit()
    assert emails.process_one(db, datetime.now(UTC) + timedelta(seconds=1)) is None
    assert e.status == EmailStatus.QUEUED


# --- Events ----------------------------------------------------------------------


def sent(db, **kw):
    e = send(db, **kw)
    emails.process_one(db, datetime.now(UTC) + timedelta(seconds=1))
    return e


def test_delivered_then_complaint_blocks_address(db, verified):  # noqa: F811
    e = sent(db)
    pid = e.provider_message_id
    emails.apply_event(db, "fake", pid, "delivered")
    emails.apply_event(db, "fake", pid, "delivered")  # idempotent
    emails.apply_event(db, "fake", pid, "complaint", "spam report")
    db.commit()
    assert e.status == EmailStatus.COMPLAINED
    assert consent.check(db, OWNER, "email", TO, "transactional").reason == "blocked:complaint"
    with pytest.raises(consent.RecipientSuppressed):
        send(db, key="after")
    trail = [x.to_status for x in db.query(EmailEvent).order_by(EmailEvent.id)]
    assert trail == ["queued", "sending", "sent", "delivered", "complained"]


def test_hard_bounce_blocks_soft_bounce_does_not(db, verified):  # noqa: F811
    soft = sent(db, key="s", to="soft@customer.org")
    emails.apply_event(db, "fake", soft.provider_message_id, "bounce_soft", "mailbox full")
    assert soft.status == EmailStatus.SENT
    assert consent.check(db, OWNER, "email", "soft@customer.org", "transactional").allowed
    hard = sent(db, key="h", to="gone@customer.org")
    emails.apply_event(db, "fake", hard.provider_message_id, "bounce_hard", "550 no such user")
    assert hard.status == EmailStatus.BOUNCED
    assert consent.check(db, OWNER, "email", "gone@customer.org", "transactional").reason == (
        "blocked:bounce_hard"
    )
    with pytest.raises(Conflict):  # bounce është përfundimtar
        emails.apply_event(db, "fake", hard.provider_message_id, "delivered")
    with pytest.raises(NotFound):
        emails.apply_event(db, "fake", "<nope@x>", "delivered")
    with pytest.raises(Conflict):
        emails.apply_event(db, "fake", hard.provider_message_id, "opened")


def test_email_events_are_append_only(db, verified):  # noqa: F811
    send(db)
    ev = db.query(EmailEvent).first()
    ev.detail = "forged"
    with pytest.raises(EmailEventImmutableError):
        db.flush()
    db.rollback()


# --- Unsubscribe -----------------------------------------------------------------


@pytest.fixture
def raw_client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app())


def test_unsubscribe_get_is_safe_post_acts(db, verified, raw_client):  # noqa: F811
    opt_in(db)
    e = send(db, category="marketing")
    tok = emails.unsubscribe_token(e.public_id)
    page = raw_client.get(f"/u/{tok}")
    assert page.status_code == 200 and "<form" in page.text and "method=post" in page.text
    assert consent.check(db, OWNER, "email", TO, "marketing").allowed  # GET nuk çregjistron
    r = raw_client.post(f"/u/{tok}")
    assert r.status_code == 200 and "unsubscribed" in r.text
    db.expire_all()
    assert consent.check(db, OWNER, "email", TO, "marketing").reason == "opted_out"
    assert consent.check(db, OWNER, "email", TO, "transactional").allowed  # vetëm marketing
    assert raw_client.post(f"/u/{tok}").status_code == 200  # idempotent
    for bad in (f"{e.public_id}.{'0' * 32}", "garbage", f"{e.public_id}."):
        assert raw_client.post(f"/u/{bad}").status_code == 404
        assert raw_client.get(f"/u/{bad}").status_code == 404


# --- Webhook --------------------------------------------------------------------


def test_email_webhook(db, verified, raw_client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "dlr_secrets", {"fake": "sec"})
    e = sent(db)

    def post(body, secret="sec"):
        raw = json.dumps(body).encode()
        sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return raw_client.post("/webhooks/email/fake", content=raw, headers={"X-Signature": sig})

    ev = {"provider_message_id": e.provider_message_id, "event": "bounce_hard"}
    assert post(ev, secret="bad").status_code == 401
    assert post({**ev, "event": "opened"}).status_code == 422
    assert post({"provider_message_id": "<x@y>", "event": "delivered"}).status_code == 503
    assert post(ev).json() == {"outcome": "applied"}
    assert post(ev).status_code == 200
    assert post({**ev, "event": "delivered"}).status_code == 409
    db.expire_all()
    assert e.status == EmailStatus.BOUNCED


# --- API -----------------------------------------------------------------------

BOOT = {"X-Admin-Key": "test-key"}


def test_api_flow_and_isolation(db, world, fake_dns, raw_client):  # noqa: F811
    c = raw_client

    def key(owner):
        r = c.post(
            "/v1/admin/api-keys",
            json={"name": "k", "role": "client", "owner_ref": owner},
            headers=BOOT,
        )
        return {"Authorization": f"Bearer {r.json()['key']}"}

    h1, h2 = key("c1"), key("c2")
    r = c.post("/v1/email/domains", json={"domain": "example.com"}, headers=h1)
    assert r.status_code == 201 and len(r.json()["dns_records"]) == 3
    assert "PRIVATE" not in r.text and "dkim_private" not in r.text
    did = r.json()["id"]
    body = {"from_email": FROM, "to": TO, "subject": "Hi", "text": "Hello"}
    h = {"Idempotency-Key": "api-1"}
    assert (
        c.post("/v1/email/messages", json=body, headers={**h1, **h}).status_code == 403
    )  # s'është verifikuar
    d = db.get(EmailDomain, did)
    fake_dns.publish(d)
    assert c.post(f"/v1/email/domains/{did}/verify", headers=h2).status_code == 404
    v = c.post(f"/v1/email/domains/{did}/verify", headers=h1).json()
    assert v["status"] == "verified" and v["spf_ok"] and v["dkim_ok"]
    sent_ = c.post("/v1/email/messages", json=body, headers={**h1, **h})
    assert sent_.status_code == 202 and sent_.json()["status"] == "queued"
    assert (
        c.post("/v1/email/messages", json=body, headers={**h1, **h}).json()["id"]
        == sent_.json()["id"]
    )
    mid = sent_.json()["id"]
    assert c.get(f"/v1/email/messages/{mid}", headers=h2).status_code == 404
    assert c.get(f"/v1/email/messages/{mid}/events", headers=h1).json()[0]["to"] == "queued"
    assert (
        c.post("/v1/email/messages", json=body, headers=h1).status_code == 422
    )  # pa Idempotency-Key
    assert c.get("/v1/email/domains", headers=h2).json() == []
    actions = {a["action"] for a in c.get("/v1/admin/audit", headers=BOOT).json()}
    assert {"email_domain.add", "email_domain.verify"} <= actions
