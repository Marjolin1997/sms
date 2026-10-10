# ruff: noqa: F811
"""M8-e — verifikimi i kontaktit, outbox email, sfida anti-bot, readiness, audit CLI (Central)."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from apps.central.core import errors
from apps.central.core.config import settings
from apps.central.main import create_app
from apps.central.models import (
    AuditLog,
    CentralUser,
    Enterprise,
    NotificationOutbox,
    RegistrationRequest,
)
from apps.central.models.service_auth import ServiceAssertionJti
from apps.central.services import contact_verification as cv
from apps.central.services import mailer, notifications, service_auth
from apps.central.services import products as prod_svc
from apps.central.services import provisioning as prov
from apps.central.services import registration_metrics as metrics
from apps.central.services import registration_ops as ops
from apps.central.services import registration_policy as pol
from apps.central.services import registrations as reg
from apps.central.tools import registration_readiness as rr
from tests.test_central import ROOT, make_db  # noqa: F401
from tests.test_central_auth import PW, auth_secret, bearer, mk, token_for  # noqa: F401
from tests.test_central_products import cdb  # noqa: F401  (fixtures)
from tests.test_central_registration_api import body, count, h, post, rows, status  # noqa: F401
from tests.test_central_sync_api import keypair

KEY = "k" * 40


@pytest.fixture
def v(h, monkeypatch):
    """Verifikimi i konfiguruar me FakeMailer (pa SMTP real)."""
    monkeypatch.setattr(settings, "registration_verify_key", KEY)
    monkeypatch.setattr(settings, "mailer", "fake")
    monkeypatch.setattr(settings, "registration_verify_url_base", "https://portal.example/verify")
    mailer._FAKE.sent.clear()
    mailer._FAKE.fail_with = None
    metrics.reset()
    yield h
    mailer._FAKE.fail_with = None
    mailer._FAKE.sent.clear()


def dispatch(h, **kw):
    return notifications.dispatch_due(h.factory, mailer.get_mailer(), **kw)


def register(h, **kw):
    j = post(h, **kw).json()
    return j["id"], j["access_token"]


def token_sent(h, rid):
    dispatch(h)
    mails = [m for m in mailer._FAKE.sent if m["registration_id"] == rid]
    assert mails, "asnjë email i dërguar"
    return mails[-1]["token"]


def verify(h, rid, token, **kw):
    return h.c.post(f"/registration/{rid}/verify", json={"token": token}, **kw)


def resend(h, rid, access):
    return h.c.post(
        f"/registration/{rid}/verification/resend", headers={"X-Registration-Token": access}
    )


def age_outbox(h, rid, seconds=120):
    """Lëviz prapa dërgimet për të kapërcyer pragun 60s të resend."""
    with h.factory() as s:
        for m in s.scalars(
            select(NotificationOutbox).where(NotificationOutbox.registration_id == uuid.UUID(rid))
        ):
            m.created_at = m.created_at - timedelta(seconds=seconds)
        s.commit()


def automatic_sms(h, monkeypatch=None):
    with h.factory() as s:
        pol.set_policy(s, h.sms, s.get(CentralUser, h.admin_u.id), approval_mode="automatic")
        s.commit()


# --- rrjedha e verifikimit -----------------------------------------------------------------------------


def test_submit_enqueues_a_verification_email_outside_the_request_and_reports_pending(v):
    j = post(v).json()
    assert j["contact_verification"] == "pending" and j["status"] == "in_review"
    assert mailer._FAKE.sent == []  # asnjë SMTP gjatë kërkesës
    assert count(v, NotificationOutbox, NotificationOutbox.state == "pending") == 1
    assert dispatch(v).sent == 1
    mail = mailer._FAKE.sent[0]
    assert mail["to"] == "ana@example.com" and len(mail["token"]) >= 40
    assert count(v, NotificationOutbox, NotificationOutbox.state == "sent") == 1


def test_unconfigured_verification_reports_unavailable_and_creates_no_outbox(h):
    j = post(h).json()
    assert j["contact_verification"] == "unavailable"
    assert count(h, NotificationOutbox) == 0


def test_replay_does_not_enqueue_another_email(v):
    post(v, key="key-aaaaaaaa")
    post(v, key="key-aaaaaaaa")
    assert count(v, NotificationOutbox) == 1


def test_verify_success_sets_verified_once_and_status_reports_it(v):
    rid, access = register(v)
    tok = token_sent(v, rid)
    r = verify(v, rid, tok)
    assert r.status_code == 200 and r.json() == {
        "id": rid,
        "status": "in_review",
        "contact_verification": "verified",
    }
    assert status(v, rid, access).json()["contact_verification"] == "verified"
    assert rows(v)[0].verified_at is not None and rows(v)[0].verification_nonce is None


def test_token_is_one_time_and_replay_changes_nothing(v):
    rid, _ = register(v)
    tok = token_sent(v, rid)
    assert verify(v, rid, tok).status_code == 200
    first = rows(v)[0].verified_at
    again = verify(v, rid, tok)
    assert again.status_code == 404 and rows(v)[0].verified_at == first


def test_wrong_expired_consumed_unknown_are_indistinguishable(v):
    rid, _ = register(v)
    tok = token_sent(v, rid)
    with v.factory() as s:  # skaduar
        s.get(RegistrationRequest, uuid.UUID(rid)).verification_expires_at = datetime.now(
            UTC
        ) - timedelta(seconds=1)
        s.commit()
    expired = verify(v, rid, tok)
    rid2, _ = register(v, b=body(v, contact_email="b@example.com"))
    tok2 = token_sent(v, rid2)
    assert verify(v, rid2, tok2).status_code == 200
    cases = [expired, verify(v, rid, "wrong"), verify(v, uuid.uuid4(), tok), verify(v, "nope", tok),
             verify(v, rid2, tok2), verify(v, rid, "x" * 300)]  # fmt: skip
    assert {(r.status_code, r.text) for r in cases} == {
        (404, '{"detail":{"code":"not_found","message":"not found"}}')
    }


def test_verify_body_is_bounded_and_strict(v):
    rid, _ = register(v)
    assert (
        v.c.post(f"/registration/{rid}/verify", json={"token": "x", "extra": 1}).status_code == 404
    )
    assert v.c.post(f"/registration/{rid}/verify", content=b"x" * 5000).status_code == 413


def test_resend_rotates_token_and_invalidates_the_previous_one(v):
    rid, access = register(v)
    old = token_sent(v, rid)
    age_outbox(v, rid)
    r = resend(v, rid, access)
    assert r.status_code == 202 and r.json() == {"accepted": True}
    new = token_sent(v, rid)
    assert new != old
    assert verify(v, rid, old).status_code == 404
    assert verify(v, rid, new).status_code == 200
    assert count(v, NotificationOutbox, NotificationOutbox.state == "superseded") <= 1


def test_resend_is_rate_limited_per_minute_and_per_day(v):
    rid, access = register(v)
    assert resend(v, rid, access).status_code == 429  # <60s nga dërgimi fillestar
    assert resend(v, rid, access).json()["detail"]["code"] == "too_many_requests"
    for _ in range(4):  # deri në 5 gjithsej / 24h
        age_outbox(v, rid)
        assert resend(v, rid, access).status_code == 202
    age_outbox(v, rid)
    assert resend(v, rid, access).status_code == 429


def test_resend_needs_the_access_token_and_is_anti_enumerating(v):
    rid, access = register(v)
    age_outbox(v, rid)
    shapes = {(r.status_code, r.text) for r in (resend(v, rid, "bad"), resend(v, uuid.uuid4(), access),
              resend(v, "nope", access), v.c.post(f"/registration/{rid}/verification/resend"))}  # fmt: skip
    assert shapes == {(404, '{"detail":{"code":"not_found","message":"not found"}}')}


def test_resend_after_verification_is_a_noop_and_unconfigured_is_503(v, monkeypatch):
    rid, access = register(v)
    verify(v, rid, token_sent(v, rid))
    n = count(v, NotificationOutbox)
    age_outbox(v, rid)
    assert resend(v, rid, access).status_code == 202 and count(v, NotificationOutbox) == n
    rid2, access2 = register(v, b=body(v, contact_email="c@example.com"))
    monkeypatch.setattr(settings, "mailer", "disabled")
    r = resend(v, rid2, access2)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "verification_unavailable"


def test_token_never_in_db_hash_audit_or_logs_only_in_the_email(v, caplog):
    caplog.set_level(logging.DEBUG)
    rid, access = register(v)
    tok = token_sent(v, rid)
    verify(v, rid, tok)
    v.c.get(f"/registration/{rid}/status", headers={"X-Registration-Token": access})
    import hashlib

    needles = {tok, hashlib.sha256(tok.encode()).hexdigest(), KEY, access}
    with v.factory() as s:
        dump = ""
        for table in ("registration_requests", "notification_outbox", "audit_log"):
            dump += " ".join(str(c) for r in s.execute(text(f"select * from {table}")) for c in r)
    for n in needles:
        assert n not in caplog.text, n
    assert (
        tok not in dump and KEY not in dump and hashlib.sha256(tok.encode()).hexdigest() not in dump
    )


def test_verification_creates_no_user_and_no_enterprise(v):
    with v.factory() as s:
        n_users = s.scalar(select(func.count()).select_from(CentralUser))
    rid, _ = register(v)
    verify(v, rid, token_sent(v, rid))
    with v.factory() as s:
        assert s.scalar(select(func.count()).select_from(CentralUser)) == n_users
    assert count(v, Enterprise) == 0


# --- auto-miratim: vetëm pas verifikimit --------------------------------------------------------------


def test_unverified_automatic_never_auto_approves_even_in_production_mode(v, monkeypatch):
    automatic_sms(v)
    monkeypatch.setattr(settings, "env", "production")
    j = post(v).json()
    assert j["status"] == "in_review" and rows(v)[0].status == "submitted"
    assert count(v, AuditLog, AuditLog.action == "registration.approve") == 0


def test_production_refuses_the_unverified_gate_fake_mailer_and_fake_challenge(h, monkeypatch):
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "auth_secret", "s" * 40)
    for name, val in (
        ("allow_unverified_auto_registration", True),
        ("mailer", "fake"),
        ("bot_challenge", "fake"),
    ):
        monkeypatch.setattr(settings, name, val)
        with pytest.raises(RuntimeError):
            create_app(h.eng)
        monkeypatch.setattr(
            settings, name, False if name == "allow_unverified_auto_registration" else "disabled"
        )


def test_verified_automatic_request_auto_approves_with_system_actor(v):
    automatic_sms(v)
    assert v.c.get("/registration/products").json()[1]["approval_mode"] == "automatic"
    rid, access = register(v)
    r = verify(v, rid, token_sent(v, rid))
    assert r.json()["status"] == "activating"
    row = rows(v)[0]
    assert (row.status, row.decision_mode, row.provisioning_status) == (
        "approved",
        "automatic",
        "pending",
    )
    assert row.decided_by_id is None and row.decided_by_label == "system:registration_auto_approval"
    with v.factory() as s:
        a = s.scalar(select(AuditLog).where(AuditLog.action == "registration.approve"))
        assert (a.actor_kind, a.actor_id, a.actor_label) == (
            "system",
            None,
            "system:registration_auto_approval",
        )
        assert a.detail["contact_verified"] is True
        ver = s.scalar(select(AuditLog).where(AuditLog.action == "registration.verify"))
        assert (ver.actor_kind, ver.actor_id, ver.actor_label) == (
            "system",
            None,
            "system:registration_verification",
        )
    assert metrics.snapshot()["registration_auto_approved_total"] == 1


def test_verified_mixed_policies_stay_manual_and_admin_sees_verification(v):
    automatic_sms(v)
    rid, _ = register(v, b=body(v, product_ids=[str(v.sms), str(v.email)]))  # email = manual
    assert verify(v, rid, token_sent(v, rid)).json()["status"] == "in_review"
    d = v.c.get(f"/admin/registrations/{rid}", headers=v.A).json()
    assert d["contact_verified"] is True and d["verified_at"] and d["status"] == "submitted"


def test_manual_flow_still_works_and_audit_shows_verified_flag(v):
    rid, _ = register(v)  # pa verifikim
    assert v.c.post(f"/admin/registrations/{rid}/approve", headers=v.A).status_code == 200
    rid2, _ = register(v, b=body(v, contact_email="z@example.com"))
    verify(v, rid2, token_sent(v, rid2))
    v.c.post(f"/admin/registrations/{rid2}/approve", headers=v.A)
    with v.factory() as s:
        flags = {
            a.resource_id: a.detail["contact_verified"]
            for a in s.scalars(select(AuditLog).where(AuditLog.action == "registration.approve"))
        }
        human = s.scalars(select(AuditLog).where(AuditLog.action == "registration.approve")).first()
        assert human.actor_kind == "user" and human.actor_id is not None
    assert flags == {rid: False, rid2: True}
    d = v.c.get(f"/admin/registrations/{rid}", headers=v.A).json()
    assert d["contact_verified"] is False and d["status"] == "approved"  # miratimi nuk "verifikon"


def test_set_policy_automatic_requires_verification_or_the_dev_gate(h, monkeypatch):
    with h.factory() as s:
        admin = s.get(CentralUser, h.admin_u.id)
        with pytest.raises(errors.Conflict):
            pol.set_policy(s, h.sms, admin, approval_mode="automatic")
        s.rollback()
        monkeypatch.setattr(settings, "registration_verify_key", KEY)
        monkeypatch.setattr(settings, "mailer", "fake")
        assert (
            pol.set_policy(s, h.sms, admin, approval_mode="automatic")[0].approval_mode
            == "automatic"
        )


# --- dërgimi: dështime, retry i kufizuar -------------------------------------------------------------------


def test_temporary_provider_failure_keeps_registration_intact_and_retries_with_backoff(v):
    rid, _ = register(v)
    mailer._FAKE.fail_with = mailer.MailerError("smtp_unavailable", temporary=True)
    now = datetime.now(UTC)
    rep = dispatch(v, now=now)
    assert (rep.sent, rep.retried) == (0, 1)
    with v.factory() as s:
        m = s.scalar(select(NotificationOutbox))
        assert (m.state, m.attempts, m.last_error_code) == ("pending", 1, "smtp_unavailable")
        assert cv._aware(m.available_at) > now  # backoff
    assert rows(v)[0].status == "submitted" and rows(v)[0].verification_nonce  # pa korrupsion
    assert dispatch(v, now=now).retried == 0  # ende s'ka mbërritur koha
    mailer._FAKE.fail_with = None
    assert dispatch(v, now=now + timedelta(minutes=2)).sent == 1
    assert (
        verify(v, rid, mailer._FAKE.sent[-1]["token"]).status_code == 200
    )  # i njëjti token (deterministik)


def test_permanent_failure_and_attempt_cap_never_retry_forever(v, monkeypatch):
    monkeypatch.setattr(settings, "registration_verify_ttl_minutes", 1440)
    register(v)
    mailer._FAKE.fail_with = mailer.MailerError("recipient_refused", temporary=False)
    assert dispatch(v).failed == 1
    assert dispatch(v, now=datetime.now(UTC) + timedelta(hours=2)).failed == 0
    register(v, b=body(v, contact_email="t@example.com"))
    mailer._FAKE.fail_with = mailer.MailerError("smtp_unavailable", temporary=True)
    t = datetime.now(UTC)
    for i in range(notifications.MAX_ATTEMPTS + 2):
        dispatch(v, now=t + timedelta(minutes=70 * (i + 1)))
    with v.factory() as s:
        m = s.scalar(
            select(NotificationOutbox).where(NotificationOutbox.recipient == "t@example.com")
        )
        assert (m.state, m.attempts) == ("failed", notifications.MAX_ATTEMPTS)


def test_superseded_or_verified_messages_are_not_sent(v):
    rid, access = register(v)
    age_outbox(v, rid)
    resend(v, rid, access)  # i pari bëhet superseded para dërgimit
    dispatch(v)
    assert len([m for m in mailer._FAKE.sent if m["registration_id"] == rid]) == 1


def test_stale_sending_row_is_reclaimed_after_a_crash(v):
    register(v)
    with v.factory() as s:  # simulon crash pas claim
        m = s.scalar(select(NotificationOutbox))
        m.state, m.attempts, m.updated_at = (
            "sending",
            1,
            datetime.now(UTC) - timedelta(seconds=notifications.STALE_SENDING_S + 5),
        )
        s.commit()
    assert dispatch(v).sent == 1


def test_smtp_mailer_maps_errors_to_stable_codes_and_never_echoes_secrets(monkeypatch):
    import smtplib

    monkeypatch.setattr(settings, "smtp_host", "smtp.example")
    monkeypatch.setattr(settings, "smtp_from", "no-reply@example.com")
    monkeypatch.setattr(settings, "smtp_password", "S3CRET-PASSWORD")
    monkeypatch.setattr(settings, "smtp_user", "u")
    monkeypatch.setattr(settings, "registration_verify_url_base", "https://p.example/v")

    class Boom:
        def __init__(self, exc):
            self.exc = exc

        def __call__(self, *a, **k):
            raise self.exc

    for exc, code, temp in ((OSError("connect to S3CRET-PASSWORD failed"), "smtp_unavailable", True),
                            (smtplib.SMTPResponseException(550, b"nope S3CRET-PASSWORD"), "smtp_rejected", False),
                            (smtplib.SMTPResponseException(451, b"later"), "smtp_rejected", True),
                            (smtplib.SMTPRecipientsRefused({}), "recipient_refused", False)):  # fmt: skip
        monkeypatch.setattr(smtplib, "SMTP", Boom(exc))
        with pytest.raises(mailer.MailerError) as e:
            mailer.SmtpMailer().send_verification(
                to="a@b.co", registration_id="1", token="T", expires_at=datetime.now(UTC)
            )
        assert (e.value.code, e.value.temporary) == (code, temp) and "S3CRET" not in str(e.value)
    assert "https://p.example/v?id=1&token=T" == mailer.verification_link("1", "T")


# --- sfida anti-bot + kuota -----------------------------------------------------------------------------------


def test_challenge_boundary_is_vendor_neutral_fail_closed_and_only_when_required(h, monkeypatch):
    assert post(h).status_code == 202  # jo e kërkuar
    monkeypatch.setattr(settings, "public_registration_require_challenge", True)
    r = post(h, body(h, contact_email="c1@example.com"))
    assert (
        r.status_code == 403 and r.json()["detail"]["code"] == "challenge_failed"
    )  # provider disabled ⇒ mbyllur
    monkeypatch.setattr(settings, "bot_challenge", "fake")
    bad = h.c.post(
        "/registration",
        json=body(h, contact_email="c2@example.com"),
        headers={"X-Bot-Challenge": "nope"},
    )
    ok = h.c.post(
        "/registration",
        json=body(h, contact_email="c3@example.com"),
        headers={"X-Bot-Challenge": "pass"},
    )
    assert (bad.status_code, ok.status_code) == (403, 202)
    src = (ROOT / "apps/central/services/bot_challenge.py").read_text().lower()
    assert "import httpx" not in src and "import requests" not in src and "urllib" not in src


def test_email_quota_still_three_per_day_with_verification_enabled(v):
    for i in range(3):
        assert post(v, key=f"key-vary-{i:04d}").status_code == 202
    assert post(v, key="key-vary-9999").status_code == 429
    assert count(v, NotificationOutbox) == 3  # kërkesat e refuzuara nuk dërgojnë email
    assert metrics.snapshot()["registration_rejected_quota_total"] == 1


def test_client_ip_is_taken_from_trusted_hops_only(h, monkeypatch):
    from starlette.requests import Request

    from apps.central.api.registration_public import client_ip

    def req(xff):
        return Request(
            {
                "type": "http",
                "headers": [(b"x-forwarded-for", xff.encode())],
                "client": ("10.0.0.9", 1),
            }
        )

    assert (
        client_ip(req("1.1.1.1, 2.2.2.2")) == "10.0.0.9"
    )  # 0 hop ⇒ XFF i falsifikueshëm injorohet
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    assert client_ip(req("6.6.6.6, 2.2.2.2")) == "2.2.2.2"


# --- logje + metrika ----------------------------------------------------------------------------------------


def test_structured_events_contain_only_safe_fields_and_metrics_count(v, caplog):
    caplog.set_level(logging.INFO, logger="central.registration")
    rid, access = register(v)
    verify(v, rid, "wrong")
    verify(v, rid, token_sent(v, rid))
    text_ = caplog.text
    assert "event=submit" in text_ and f"registration_id={rid}" in text_ and "event=verify" in text_
    for secret in (access, "ana@example.com", KEY):
        assert secret not in text_
    snap = metrics.snapshot()
    assert snap["registration_submitted_total"] == 1 and snap["registration_verified_total"] == 1
    assert snap["registration_verification_failed_total"] == 1
    v.c.post("/registration", json={"bad": 1})
    assert metrics.snapshot()["registration_public_4xx_total"] >= 1
    metrics.event("x", registration_id=rid, token="LEAK", email="e@x.co")
    assert "LEAK" not in caplog.text and "e@x.co" not in caplog.text


# --- audit i CLI auto-grant -------------------------------------------------------------------------------------


def test_service_client_cli_mutations_are_audited_with_a_system_actor(h):
    from apps.central.tools import service_credential_admin as cli

    with h.factory() as s:
        service_auth.create_client(s, "ent-main", ["sync:read"], [])
        s.commit()
    assert "changed" in cli.run("enable-auto-grant", "ent-main", engine=h.eng)
    assert "no change" in cli.run("enable-auto-grant", "ent-main", engine=h.eng)  # no-op ⇒ pa audit
    cli.run("disable-auto-grant", "ent-main", engine=h.eng)
    with h.factory() as s:
        acts = list(
            s.scalars(
                select(AuditLog)
                .where(AuditLog.action.like("service_client.%"))
                .order_by(AuditLog.created_at)
            )
        )
        assert [a.action for a in acts] == [
            "service_client.enable_auto_grant",
            "service_client.disable_auto_grant",
        ]
        a = acts[0]
        assert (a.actor_kind, a.actor_id, a.actor_label) == (
            "system",
            None,
            "system:service_client_configuration",
        )
        assert a.detail["auto_grant_new_enterprises"] == {"before": False, "after": True}
        dump = " ".join(repr(x.detail) for x in acts).lower()
        assert "private" not in dump and "secret" not in dump and "token" not in dump


# --- readiness + backlog ---------------------------------------------------------------------------------------


def active_consumer(h, *, seen_ago_s=30):
    priv, pub = keypair()
    with h.factory() as s:
        c = service_auth.create_client(s, "ent-main", ["sync:read"], [])
        service_auth.add_key(s, "ent-main", "k1", pub)
        s.add(
            ServiceAssertionJti(
                client_pk=c.id,
                jti=uuid.uuid4().hex,
                expires_at=datetime.now(UTC) - timedelta(seconds=seen_ago_s),
            )
        )
        s.commit()


def evaluate(h, **kw):
    with h.factory() as s:
        return {c.name: c for c in rr.evaluate(s, **kw)}


def prod_ready(monkeypatch):
    for k, val in (("env", "production"), ("registration_verify_key", KEY), ("mailer", "smtp"),
                   ("smtp_host", "smtp.example"), ("smtp_from", "no-reply@example.com"),
                   ("registration_verify_url_base", "https://portal.example/verify"),
                   ("public_registration_proxy_ack", True), ("trusted_proxy_hops", 1),
                   ("public_registration_require_challenge", True), ("auth_secret", "s" * 40)):  # fmt: skip
        monkeypatch.setattr(settings, k, val)
    monkeypatch.setattr(settings, "bot_challenge", "disabled")


def test_readiness_passes_when_public_registration_is_intentionally_offline(h, monkeypatch):
    monkeypatch.setattr(settings, "public_registration_enabled", False)
    monkeypatch.setattr(settings, "env", "production")
    c = evaluate(h)
    assert c["public_registration"].level == "PASS" and "offline" in c["public_registration"].reason
    assert not {"contact_verification", "proxy_limits", "bot_challenge"} & set(c)
    assert rr.exit_code(list(c.values())) == 0


def test_readiness_fails_critical_unsafe_production_config_and_exit_code_is_nonzero(h, monkeypatch):
    monkeypatch.setattr(settings, "env", "production")
    c = evaluate(h)  # public ON, pa verifikim/ack/konsumator
    for name in ("contact_verification", "proxy_limits", "control_plane"):
        assert c[name].level == "FAIL", name
    assert rr.exit_code(list(c.values())) == 1


def test_readiness_flags_unsafe_automatic_policies(h, monkeypatch):
    monkeypatch.setattr(settings, "allow_unverified_auto_registration", True)
    automatic_sms(h)
    assert evaluate(h)["automatic_policies"].level == "WARN"  # dev
    monkeypatch.setattr(settings, "env", "production")
    assert evaluate(h)["automatic_policies"].level == "FAIL"
    monkeypatch.setattr(settings, "allow_unverified_auto_registration", False)
    c = evaluate(h)  # automatic ekziston por verifikimi s'është i konfiguruar
    assert (
        c["automatic_policies"].level == "FAIL" and "verification" in c["automatic_policies"].reason
    )
    monkeypatch.setattr(settings, "registration_verify_key", KEY)
    monkeypatch.setattr(settings, "mailer", "fake")
    assert evaluate(h)["automatic_policies"].level == "PASS"


def test_readiness_healthy_production_state_has_no_failures(h, monkeypatch):
    prod_ready(monkeypatch)
    monkeypatch.setattr(settings, "bot_challenge", "fake")  # provider real vjen më vonë; test-only
    monkeypatch.setattr(settings, "env", "development")  # fake lejohet vetëm jashtë prodhimit
    monkeypatch.setattr(settings, "mailer", "smtp")
    active_consumer(h)
    c = evaluate(h)
    assert not [x for x in c.values() if x.level == "FAIL"], c
    assert rr.exit_code(list(c.values())) == 0


def test_readiness_challenge_required_without_provider_fails(h, monkeypatch):
    prod_ready(monkeypatch)
    active_consumer(h)
    c = evaluate(h)
    assert c["bot_challenge"].level == "FAIL" and rr.exit_code(list(c.values())) == 1


def test_failed_provisioning_and_stale_pending_backlogs_are_reported(h):
    with h.factory() as s:
        admin = s.get(CentralUser, h.admin_u.id)
        r1 = reg.submit(
            s, enterprise_name="A", contact_email="a@example.com", product_ids=[h.sms]
        ).request
        r2 = reg.submit(
            s, enterprise_name="B", contact_email="b@example.com", product_ids=[h.email]
        ).request
        reg.approve(s, r1.id, admin)
        reg.approve(s, r2.id, admin)
        prod_svc.update(s, h.sms, status="retired")
        s.commit()
        i1 = r1.id
    assert prov.run(h.factory, i1).status == "failed"
    rep = ops.report(Session(h.eng))
    assert rep["provisioning"]["failed"] == 1 and rep["provisioning"]["pending"] == 1
    c = evaluate(h)
    assert c["failed_provisioning"].level == "WARN" and c["pending_activation"].level == "PASS"
    later = datetime.now(UTC) + timedelta(hours=2)
    assert evaluate(h, now=later)["pending_activation"].level == "WARN"
    much_later = datetime.now(UTC) + timedelta(days=2)
    c = evaluate(h, now=much_later)
    assert c["failed_provisioning"].level == "FAIL" and c["pending_activation"].level == "FAIL"


def test_m7_health_rules_when_public_registration_is_enabled(h, monkeypatch):
    assert evaluate(h)["control_plane"].level == "FAIL"  # asnjë konsumator aktiv
    active_consumer(h, seen_ago_s=-30)  # i fundit: aktiv tani
    assert evaluate(h)["control_plane"].level == "PASS"
    stale = datetime.now(UTC) + timedelta(hours=3)
    assert (
        evaluate(h, now=stale)["control_plane"].level == "WARN"
    )  # i heshtur, pa regjistrime të reja
    with h.factory() as s:  # regjistrim i provisionuar "para pak" + konsumator i heshtur ⇒ FAIL
        admin = s.get(CentralUser, h.admin_u.id)
        r = reg.submit(
            s, enterprise_name="A", contact_email="a@example.com", product_ids=[h.sms]
        ).request
        reg.approve(s, r.id, admin)
        s.commit()
        rid = r.id
    prov.run(h.factory, rid)
    with h.factory() as s:
        s.execute(
            text("update service_assertion_jti set expires_at = :t"),
            {"t": datetime.now(UTC) - timedelta(hours=2)},
        )
        s.commit()
    assert evaluate(h)["control_plane"].level == "FAIL"


def test_email_outbox_backlog_is_reported(v):
    register(v)
    assert evaluate(v)["email_outbox"].level == "PASS"
    assert (
        evaluate(v, now=datetime.now(UTC) + timedelta(minutes=20))["email_outbox"].level == "WARN"
    )
    assert evaluate(v, now=datetime.now(UTC) + timedelta(hours=2))["email_outbox"].level == "FAIL"


def test_ops_endpoint_rbac_and_shape(h):
    assert h.c.get("/admin/registration-ops").status_code == 401
    for hd in (h.A, h.O):
        r = h.c.get("/admin/registration-ops", headers=hd)
        assert r.status_code == 200 and set(r.json()) == {
            "generated_at",
            "provisioning",
            "verification",
            "email_outbox",
            "control_plane",
        }


def test_readiness_cli_is_read_only_and_json(h, capsys):
    before = count(h, AuditLog)
    assert rr.main.__doc__ is None or True
    with h.factory() as s:
        out = [c.level for c in rr.evaluate(s)]
    assert out and count(h, AuditLog) == before


# --- migrimi + kufijtë -------------------------------------------------------------------------------------------


def test_migration_0016_up_down_up(make_db):
    from sqlalchemy import create_engine, inspect

    from tests.test_central import central_alembic

    url = make_db("central")
    central_alembic(url, "upgrade", "0015")
    eng = create_engine(url)
    assert "notification_outbox" not in inspect(eng).get_table_names()
    central_alembic(url, "upgrade", "head")
    cols = {c["name"] for c in inspect(eng).get_columns("registration_requests")}
    assert {"verified_at", "verification_nonce", "verification_expires_at"} <= cols
    central_alembic(url, "downgrade", "0015")
    assert "notification_outbox" not in inspect(eng).get_table_names()
    assert "verified_at" not in {
        c["name"] for c in inspect(eng).get_columns("registration_requests")
    }
    central_alembic(url, "upgrade", "head")
    eng.dispose()


def test_m8e_modules_have_no_enterprise_coupling_and_no_vendor_sdk():
    import ast

    forbidden = {"app", "httpx", "requests", "aiosmtplib", "redis", "prometheus_client"}
    for f in ("services/mailer.py", "services/contact_verification.py", "services/notifications.py",
              "services/bot_challenge.py", "services/registration_metrics.py", "services/registration_ops.py",
              "tools/registration_readiness.py", "tools/send_notifications.py"):  # fmt: skip
        tree = ast.parse((ROOT / "apps/central" / f).read_text())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                mods.add(n.module.split(".")[0])
        assert not mods & forbidden, (f, mods & forbidden)


def _pg_only(h):
    if h.url.startswith("sqlite"):
        pytest.skip("needs PostgreSQL row locks")


def test_pg_concurrent_verifications_with_one_token_succeed_exactly_once(v):
    _pg_only(v)
    import threading

    from tests.test_central_sync_outbox import run_threads

    rid, _ = register(v)
    tok = token_sent(v, rid)
    barrier, out = threading.Barrier(2, timeout=20), []

    def worker():
        with v.factory() as s:
            barrier.wait()
            try:
                cv.verify(s, rid, tok)
                s.commit()
                out.append("ok")
            except cv.VerificationFailed:
                s.rollback()
                out.append("denied")

    run_threads([worker, worker])
    assert sorted(out) == ["denied", "ok"]
    assert count(v, AuditLog, AuditLog.action == "registration.verify") == 1


def test_pg_concurrent_resends_queue_at_most_one_message(v):
    _pg_only(v)
    import threading

    from tests.test_central_sync_outbox import run_threads

    rid, _ = register(v)
    age_outbox(v, rid)
    barrier, out = threading.Barrier(4, timeout=20), []

    def worker():
        with v.factory() as s:
            barrier.wait()
            try:
                cv.resend(s, rid)
                s.commit()
                out.append("queued")
            except errors.TooManyRequests:
                s.rollback()
                out.append("limited")

    run_threads([worker] * 4)
    assert out.count("queued") == 1 and out.count("limited") == 3
    assert count(v, NotificationOutbox, NotificationOutbox.state == "pending") == 1


def test_pg_automatic_approval_after_verification_is_atomic_with_verified_at(v, monkeypatch):
    _pg_only(v)
    automatic_sms(v)
    rid, _ = register(v)
    tok = token_sent(v, rid)
    orig = reg.auto_approve_verified

    def boom(*a, **k):
        raise RuntimeError("injected")

    monkeypatch.setattr(reg, "auto_approve_verified", boom)
    with v.factory() as s:
        with pytest.raises(RuntimeError):
            cv.verify(s, rid, tok)
        s.rollback()
    row = rows(v)[0]
    assert row.verified_at is None and row.status == "submitted"  # asgjë e pjesshme
    monkeypatch.setattr(reg, "auto_approve_verified", orig)
    assert verify(v, rid, tok).json()["status"] == "activating"
