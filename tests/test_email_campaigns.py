from datetime import UTC, datetime, timedelta

import pytest

from app.models.campaigns import CampaignRecipient, CampaignStatus, RecipientStatus
from app.models.email import Email, EmailStatus
from app.services import campaigns as svc
from app.services import consent, emails, switches
from app.services import contacts as contacts_svc
from app.services.wallet import Conflict
from tests.test_campaigns import BOOT, NOW, drive, recs, start  # noqa: F401
from tests.test_email import FROM, fake_dns, fake_email_provider, verified, world  # noqa: F401


def audience(db, n=3, base="ana", opt_in=True):
    lst = contacts_svc.create_list(db, "c1", f"elist-{base}")
    ids = []
    for i in range(n):
        c, _ = contacts_svc.upsert(db, "c1", email=f"{base}{i}@customer.org", first_name=f"N{i}")
        if opt_in:
            consent.record(db, "c1", "email", c.email, "opt_in", "x", "form", "u", "evidence")
        ids.append(c.id)
    contacts_svc.add_members(db, "c1", lst.id, ids)
    db.commit()
    return lst, ids


def ecampaign(db, lst, name="mail", **kw):
    kw = {
        "channel": "email",
        "from_email": FROM,
        "subject": "Hi {{first_name}}",
        "text": "Hello {{first_name}}",
        "created_by": "tester",
        "sender": "",
    } | kw
    c = svc.create(db, "c1", name, lst.id, **kw)
    db.commit()
    return c


def test_email_campaign_runs_and_personalizes_with_html_escaping(db, verified):  # noqa: F811
    lst, ids = audience(db, 3)
    weird, _ = contacts_svc.upsert(db, "c1", email="weird@customer.org", first_name="<b>Bob</b>")
    consent.record(db, "c1", "email", weird.email, "opt_in", "x", "form", "u", "evidence")
    noconsent, _ = contacts_svc.upsert(db, "c1", email="nc@customer.org", first_name="Nc")
    phone_only, _ = contacts_svc.upsert(db, "c1", phone="355691231555")
    contacts_svc.add_members(db, "c1", lst.id, [weird.id, noconsent.id, phone_only.id])
    db.commit()
    c = ecampaign(db, lst, html="<html><body><p>Hi {{first_name}}</p></body></html>")
    start(db, c)
    drive(db)
    db.refresh(c)
    assert c.status == CampaignStatus.COMPLETED
    rows = {e.to_email: e for e in db.query(Email)}
    assert len(rows) == 4 and rows["ana1@customer.org"].subject == "Hi N1"
    assert rows["ana1@customer.org"].text_body == "Hello N1"
    assert rows["weird@customer.org"].text_body == "Hello <b>Bob</b>"  # tekst i thjeshtë: pa escape
    assert "&lt;b&gt;Bob&lt;/b&gt;" in rows["weird@customer.org"].html_body  # html: escape (XSS)
    assert all(e.category == "marketing" for e in rows.values())
    s = svc.stats(db, c)
    assert s["channel"] == "email" and s["recipients"] == {"queued": 4, "skipped": 2}
    assert s["skipped_reasons"] == {"no_consent": 1, "no_address": 1}
    assert s["cost"] == {"delivered": "0", "in_flight": "0", "refunded": "0"}


def test_stats_after_delivery_bounce_and_complaint(db, verified):  # noqa: F811
    lst, ids = audience(db, 4)
    c = ecampaign(db, lst)
    start(db, c)
    drive(db)
    for _ in range(4):
        emails.process_one(db, NOW + timedelta(seconds=1))
    es = db.query(Email).order_by(Email.id).all()
    emails.apply_event(db, "fake", es[0].provider_message_id, "delivered")
    emails.apply_event(db, "fake", es[1].provider_message_id, "delivered")
    emails.apply_event(db, "fake", es[1].provider_message_id, "complaint")
    emails.apply_event(db, "fake", es[2].provider_message_id, "bounce_hard", "550")
    db.commit()
    s = svc.stats(db, c)
    assert s["messages"] == {"delivered": 1, "complained": 1, "bounced": 1, "sent": 1}
    assert s["delivery_rate"] == 0.6667  # (1 dorëzuar + 1 ankesë) / (2 + 1 bounce)


def test_bounced_and_unsubscribed_addresses_excluded_from_next_campaign(db, verified):  # noqa: F811
    lst, ids = audience(db, 3)
    c1 = ecampaign(db, lst, name="first")
    start(db, c1)
    drive(db)
    for _ in range(3):
        emails.process_one(db, NOW + timedelta(seconds=1))
    es = db.query(Email).order_by(Email.id).all()
    emails.apply_event(db, "fake", es[0].provider_message_id, "bounce_hard")
    emails.unsubscribe(db, emails.unsubscribe_token(es[1].public_id))
    db.commit()
    c2 = ecampaign(db, lst, name="second")
    start(db, c2)
    drive(db, NOW + timedelta(minutes=5))
    s = svc.stats(db, c2)
    assert s["recipients"] == {"queued": 1, "skipped": 2}
    assert s["skipped_reasons"] == {"blocked:bounce_hard": 1, "opted_out": 1}


def test_schedule_needs_verified_domain_and_validation(db, world, fake_dns):  # noqa: F811
    lst, _ = audience(db, 1)
    c = ecampaign(db, lst)
    with pytest.raises(svc.InvalidCampaign):  # domeni s'është verifikuar
        svc.schedule(db, "c1", c.id, None, now=NOW)
    for kw in (
        {"subject": None}, {"text": None}, {"from_email": "nope"}, {"template_id": 1},
        {"max_cost": "1"}, {"subject": "x" * 201},
    ):  # fmt: skip
        with pytest.raises((svc.InvalidCampaign, Conflict)):
            ecampaign(db, lst, name="bad", **kw)
    with pytest.raises(svc.InvalidCampaign):  # fusha email në një campaign SMS
        svc.create(db, "c1", "sms-x", lst.id, "ACME", "t", text="hi", subject="nope")
    with pytest.raises(svc.InvalidCampaign):  # SMS pa sender
        svc.create(db, "c1", "sms-y", lst.id, "", "t", text="hi")


def test_rate_limit_kill_switch_and_crash_recovery(db, verified):  # noqa: F811
    lst, _ = audience(db, 4)
    c = ecampaign(db, lst, rate_per_minute=2)
    start(db, c)
    drive(db)
    assert len(recs(db, c, RecipientStatus.QUEUED)) == 2
    switches.set_switch(db, switches.SUBMIT, False, "ops", "incident")
    db.commit()
    drive(db, NOW + timedelta(seconds=61))
    assert len(recs(db, c, RecipientStatus.QUEUED)) == 2
    switches.set_switch(db, switches.SUBMIT, True, "ops", None)
    db.commit()
    for r in recs(db, c):  # crash: marrësit humbin gjendjen QUEUED
        r.status, r.email_id, r.queued_at = RecipientStatus.PENDING, None, None
    c.status = CampaignStatus.RUNNING
    db.commit()
    drive(db, NOW + timedelta(minutes=5))
    drive(db, NOW + timedelta(minutes=10))
    assert db.query(Email).count() == 4  # asnjë email i dyfishtë
    assert len(recs(db, c, RecipientStatus.QUEUED)) == 4


def test_cancel_cancels_unclaimed_emails(db, verified):  # noqa: F811
    lst, _ = audience(db, 4)
    c = ecampaign(db, lst, rate_per_minute=3)
    start(db, c)
    drive(db)
    claimed = emails.claim_next(db, NOW + timedelta(seconds=1))
    db.commit()
    svc.cancel(db, "c1", c.id)
    db.commit()
    status = {e.id: e.status for e in db.query(Email)}
    assert status[claimed.id] == EmailStatus.SENDING
    assert sorted(s.value for s in status.values()) == ["failed", "failed", "sending"]
    assert len(recs(db, c, RecipientStatus.CANCELLED)) == 1


def test_estimate_and_erasure(db, verified):  # noqa: F811
    lst, ids = audience(db, 3)
    contacts_svc.upsert(db, "c1", email="nc@customer.org")
    c = ecampaign(db, lst)
    e = svc.estimate(db, "c1", c.id, now=NOW)
    assert (e.recipients, e.segments, e.total, e.currency) == (3, 0, 0, None)
    start(db, c)
    svc.run_due(db, NOW)
    svc.run_due(db, NOW)
    contacts_svc.erase(db, "c1", ids[0], "dpo")
    db.commit()
    drive(db)
    r0 = db.query(CampaignRecipient).filter_by(contact_id=ids[0]).one()
    assert r0.address is None and r0.status == RecipientStatus.CANCELLED
    assert len(recs(db, c, RecipientStatus.QUEUED)) == 2


def test_end_to_end_signed_email_from_campaign(db, verified, fake_email_provider):  # noqa: F811
    lst, _ = audience(db, 1)
    c = ecampaign(db, lst)
    start(db, c)
    drive(db)
    emails.process_one(db, datetime.now(UTC) + timedelta(days=3650))
    raw = fake_email_provider.calls[0].raw
    assert raw.startswith(b"DKIM-Signature:") and b"List-Unsubscribe-Post" in raw
