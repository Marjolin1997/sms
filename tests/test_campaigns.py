from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from app.models.campaigns import Campaign, CampaignRecipient, CampaignStatus, RecipientStatus
from app.models.sending import Message, MessageStatus
from app.services import campaigns as svc
from app.services import consent, switches, templates
from app.services import contacts as contacts_svc
from app.services import messages as msg
from app.services import wallet as wallets
from app.services.wallet import Conflict, TopupMethod
from tests.test_pipeline import fake, world  # noqa: F401

NOW = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)


def audience(db, n=3, owner="c1", opt_in=True, base=355691231010):
    lst = contacts_svc.create_list(db, owner, f"list-{base}")
    ids = []
    for i in range(n):
        c, _ = contacts_svc.upsert(db, owner, phone=str(base + i), first_name=f"N{i}")
        if opt_in:
            consent.record(db, owner, "sms", c.phone, "opt_in", "x", "form", "u", "evidence")
        ids.append(c.id)
    contacts_svc.add_members(db, owner, lst.id, ids)
    db.commit()
    return lst, ids


def campaign(db, lst, name="promo", **kw):
    kw = {"text": "Big sale!", "sender": "ACME", "created_by": "tester"} | kw
    c = svc.create(db, "c1", name, lst.id, **kw)
    db.commit()
    return c


def start(db, c, now=NOW):
    svc.schedule(db, "c1", c.id, None, now=now)
    db.commit()


def drive(db, now=NOW, cycles=6):
    for _ in range(cycles):
        svc.run_due(db, now)


def recs(db, c, status=None):
    q = db.query(CampaignRecipient).filter_by(campaign_id=c.id)
    return q.filter_by(status=status).all() if status else q.all()


def test_full_run_with_exclusions_and_stats(db, world):  # noqa: F811
    w, _ = world
    lst, ids = audience(db, 3)
    consented_out, _ = contacts_svc.upsert(db, "c1", phone="355691231099")  # pa opt-in
    email_only, _ = contacts_svc.upsert(db, "c1", email="e@example.com")
    contacts_svc.add_members(db, "c1", lst.id, [consented_out.id, email_only.id])
    db.commit()
    c = campaign(db, lst)
    start(db, c)
    drive(db)
    db.refresh(c)
    assert c.status == CampaignStatus.COMPLETED and c.completed_at
    s = svc.stats(db, c)
    assert s["recipients"] == {"queued": 3, "skipped": 2}
    assert s["skipped_reasons"] == {"no_consent": 1, "no_address": 1}
    assert s["messages"] == {"queued": 3} and s["cost"]["in_flight"] == "0.150000"
    assert wallets.balances(db, w.id) == (D("9.85"), D("0.15"))
    assert all(m.category == "marketing" for m in db.query(Message))


def test_delivery_stats_and_cost_split(db, world):  # noqa: F811
    lst, _ = audience(db, 3)
    c = campaign(db, lst)
    start(db, c)
    drive(db)
    for _ in range(3):
        msg.process_one(db, NOW + timedelta(seconds=1))
    ms = db.query(Message).order_by(Message.id).all()
    msg.apply_dlr(db, "fake", ms[0].provider_message_id, True)
    msg.apply_dlr(db, "fake", ms[1].provider_message_id, True)
    msg.apply_dlr(db, "fake", ms[2].provider_message_id, False, "absent")
    db.commit()
    s = svc.stats(db, c)
    assert s["delivery_rate"] == 0.6667
    assert s["cost"] == {"delivered": "0.100000", "in_flight": "0", "refunded": "0.050000"}


def test_template_personalization_and_missing_variable(db, world):  # noqa: F811
    lst, ids = audience(db, 2)
    nameless, _ = contacts_svc.upsert(db, "c1", phone="355691231077")
    consent.record(db, "c1", "sms", nameless.phone, "opt_in", "x", "form", "u", "evidence")
    contacts_svc.add_members(db, "c1", lst.id, [nameless.id])
    v = templates.create(db, "c1", "hi", "Hi {{first_name}}, deal for you")
    templates.review(db, v.id, "approve", "admin")
    db.commit()
    c = campaign(db, lst, text=None, template_id=v.template_id)
    start(db, c)
    drive(db)
    texts = sorted(m.text for m in db.query(Message))
    assert texts == ["Hi N0, deal for you", "Hi N1, deal for you"]
    assert svc.stats(db, c)["skipped_reasons"] == {"missing_variable:first_name": 1}


def test_stop_after_materialization_is_respected(db, world):  # noqa: F811
    lst, _ = audience(db, 3)
    c = campaign(db, lst)
    start(db, c)
    svc.run_due(db, NOW)  # SCHEDULED → PREPARING
    svc.run_due(db, NOW)  # audienca materializohet: 3 PENDING
    assert len(recs(db, c, RecipientStatus.PENDING)) == 3
    consent.apply_inbound_keyword(db, "c1", "355691231011", "STOP")
    db.commit()
    drive(db)
    s = svc.stats(db, c)
    assert s["recipients"] == {"queued": 2, "skipped": 1}
    assert s["skipped_reasons"] == {"recipient_suppressed": 1}


def test_rate_per_minute(db, world):  # noqa: F811
    lst, _ = audience(db, 5)
    c = campaign(db, lst, rate_per_minute=2)
    start(db, c)
    drive(db)
    assert len(recs(db, c, RecipientStatus.QUEUED)) == 2
    drive(db, NOW + timedelta(seconds=30))  # ende brenda minutës
    assert len(recs(db, c, RecipientStatus.QUEUED)) == 2
    drive(db, NOW + timedelta(seconds=61))
    assert len(recs(db, c, RecipientStatus.QUEUED)) == 4
    drive(db, NOW + timedelta(seconds=125))
    db.refresh(c)
    assert len(recs(db, c, RecipientStatus.QUEUED)) == 5 and c.status == CampaignStatus.COMPLETED


def test_budget_pauses_before_overspending(db, world):  # noqa: F811
    lst, _ = audience(db, 4)
    c = campaign(db, lst, max_cost="0.10")  # 2 mesazhe × 0.05
    start(db, c)
    drive(db)
    db.refresh(c)
    assert c.status == CampaignStatus.PAUSED and c.pause_reason == "budget_exhausted"
    assert len(recs(db, c, RecipientStatus.QUEUED)) == 2
    assert svc._reserved_cost(db, c.id) == D("0.10")
    svc.resume(db, "c1", c.id)
    db.commit()
    drive(db)  # buxheti s'u rrit: pauzohet përsëri pa shpenzuar më shumë
    db.refresh(c)
    assert c.status == CampaignStatus.PAUSED and svc._reserved_cost(db, c.id) == D("0.10")


def test_insufficient_funds_pauses_then_resumes_after_topup(db, world):  # noqa: F811
    w, _ = world
    wallets.adjustment(db, w.id, "-9.93", "drain", "test")  # mbeten 0.07
    lst, _ = audience(db, 3)
    c = campaign(db, lst)
    start(db, c)
    drive(db)
    db.refresh(c)
    assert c.status == CampaignStatus.PAUSED and c.pause_reason == "insufficient_funds"
    assert len(recs(db, c, RecipientStatus.QUEUED)) == 1
    assert len(recs(db, c, RecipientStatus.PENDING)) == 2  # asnjë marrës nuk u dështua
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "5", TopupMethod.CASH).id)
    svc.resume(db, "c1", c.id)
    db.commit()
    drive(db)
    db.refresh(c)
    assert c.status == CampaignStatus.COMPLETED and len(recs(db, c, RecipientStatus.QUEUED)) == 3
    assert wallets.verify_wallet(db, w.id)


def test_send_window_including_midnight_wrap(db, world):  # noqa: F811
    lst, _ = audience(db, 2)
    c = campaign(db, lst, window_start_hour=9, window_end_hour=17)
    start(db, c, now=datetime(2030, 6, 1, 3, tzinfo=UTC))
    drive(db, datetime(2030, 6, 1, 3, tzinfo=UTC))
    assert not recs(db, c, RecipientStatus.QUEUED)
    drive(db, datetime(2030, 6, 1, 10, tzinfo=UTC))
    assert len(recs(db, c, RecipientStatus.QUEUED)) == 2
    night = Campaign(window_start_hour=22, window_end_hour=6, utc_offset_minutes=120)
    assert svc._in_window(night, datetime(2030, 6, 1, 21, tzinfo=UTC))  # 23:00 lokale
    assert svc._in_window(night, datetime(2030, 6, 1, 3, tzinfo=UTC))  # 05:00 lokale
    assert not svc._in_window(night, datetime(2030, 6, 1, 12, tzinfo=UTC))


def test_kill_switch_holds_campaign(db, world):  # noqa: F811
    lst, _ = audience(db, 2)
    c = campaign(db, lst)
    start(db, c)
    svc.run_due(db, NOW)
    svc.run_due(db, NOW)
    svc.run_due(db, NOW)
    switches.set_switch(db, switches.SUBMIT, False, "ops", "incident")
    db.commit()
    drive(db)
    assert not recs(db, c, RecipientStatus.QUEUED)
    switches.set_switch(db, switches.SUBMIT, True, "ops", None)
    db.commit()
    drive(db)
    assert len(recs(db, c, RecipientStatus.QUEUED)) == 2


def test_crash_recovery_never_double_charges(db, world):  # noqa: F811
    w, _ = world
    lst, _ = audience(db, 3)
    c = campaign(db, lst)
    start(db, c)
    drive(db)
    before = wallets.balances(db, w.id)
    # simulim: procesi vdiq para se të shënonte marrësit si QUEUED
    for r in recs(db, c):
        r.status, r.message_id, r.queued_at = RecipientStatus.PENDING, None, None
    c.status = CampaignStatus.RUNNING
    db.commit()
    drive(db, NOW + timedelta(minutes=5))
    assert wallets.balances(db, w.id) == before
    assert db.query(Message).count() == 3 and len(recs(db, c, RecipientStatus.QUEUED)) == 3


def test_cancel_stops_pending_and_refunds_unclaimed(db, world):  # noqa: F811
    w, _ = world
    lst, _ = audience(db, 4)
    c = campaign(db, lst, rate_per_minute=3)
    start(db, c)
    drive(db)  # 3 të radhitur, 1 pending
    claimed = msg.claim_next(db, NOW + timedelta(seconds=1))  # worker-i e ka marrë një
    db.commit()
    svc.cancel(db, "c1", c.id)
    db.commit()
    db.refresh(c)
    assert c.status == CampaignStatus.CANCELLED
    assert len(recs(db, c, RecipientStatus.CANCELLED)) == 1
    by = {m.id: m.status for m in db.query(Message)}
    assert by[claimed.id] == MessageStatus.SENDING
    assert sorted(by.values()).count(MessageStatus.FAILED) == 2
    assert wallets.balances(db, w.id) == (D("9.95"), D("0.05"))  # vetëm SENDING mbetet i rezervuar
    assert wallets.verify_wallet(db, w.id)
    with pytest.raises(Conflict):
        svc.cancel(db, "c1", c.id)


def test_lifecycle_guards(db, world):  # noqa: F811
    lst, _ = audience(db, 1)
    c = campaign(db, lst)
    with pytest.raises(Conflict):
        svc.pause(db, "c1", c.id)
    with pytest.raises(Conflict):
        svc.resume(db, "c1", c.id)
    with pytest.raises(svc.InvalidCampaign):  # e kaluara
        svc.schedule(db, "c1", c.id, NOW - timedelta(hours=1), now=NOW)
    svc.schedule(db, "c1", c.id, NOW + timedelta(hours=1), now=NOW)
    db.commit()
    with pytest.raises(Conflict):
        svc.schedule(db, "c1", c.id, None, now=NOW)
    drive(db, NOW)  # ende para kohës
    assert not recs(db, c)
    drive(db, NOW + timedelta(hours=2))
    assert recs(db, c)


def test_validation(db, world):  # noqa: F811
    lst, _ = audience(db, 1)
    for kw in (
        {"text": None},
        {"template_id": 1},
        {"rate_per_minute": 0},
        {"window_start_hour": 9},
        {"window_start_hour": 9, "window_end_hour": 9},
        {"max_cost": "0"},
        {"category": "spam"},
    ):
        with pytest.raises((svc.InvalidCampaign, wallets.InvalidAmount)):
            svc.create(db, "c1", "x", lst.id, "ACME", "t", **({"text": "hi"} | kw))
    campaign(db, lst, name="dup")
    with pytest.raises(Conflict):
        campaign(db, lst, name="dup")
    with pytest.raises(wallets.NotFound):  # listë e tenant-it tjetër
        svc.create(db, "c2", "x", lst.id, "ACME", "t", text="hi")
    other = svc.create(db, "c1", "unapproved", lst.id, "NOPE", "t", text="hi")
    with pytest.raises(svc.InvalidCampaign):
        svc.schedule(db, "c1", other.id, None, now=NOW)


def test_estimate_is_exact(db, world):  # noqa: F811
    lst, _ = audience(db, 3)
    contacts_svc.upsert(db, "c1", phone="355691231088")  # pa consent
    c = campaign(db, lst, text="x" * 161)  # 2 segmente × 0.05
    e = svc.estimate(db, "c1", c.id, now=NOW)
    assert (e.recipients, e.segments, e.total, e.currency) == (3, 6, D("0.30"), "EUR")


def test_erasure_cancels_pending_and_drops_address(db, world):  # noqa: F811
    lst, ids = audience(db, 2)
    c = campaign(db, lst)
    start(db, c)
    svc.run_due(db, NOW)
    svc.run_due(db, NOW)
    contacts_svc.erase(db, "c1", ids[0], "dpo")
    db.commit()
    drive(db)
    r0 = db.query(CampaignRecipient).filter_by(contact_id=ids[0]).one()
    assert r0.address is None and r0.status == RecipientStatus.CANCELLED
    assert len(recs(db, c, RecipientStatus.QUEUED)) == 1


# --- API -----------------------------------------------------------------------

BOOT = {"X-Admin-Key": "test-key"}


def test_api_flow_and_isolation(db, world):  # noqa: F811
    from fastapi.testclient import TestClient

    from app.main import create_app

    c = TestClient(create_app())

    def key(owner):
        r = c.post(
            "/v1/admin/api-keys",
            json={"name": "k", "role": "client", "owner_ref": owner},
            headers=BOOT,
        )
        return {"Authorization": f"Bearer {r.json()['key']}"}

    h1, h2 = key("c1"), key("c2")
    lst, _ = audience(db, 2)
    body = {"name": "api", "list_id": lst.id, "sender": "ACME", "text": "hello", "max_cost": "1"}
    r = c.post("/v1/campaigns", json=body, headers=h1)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert (
        c.post("/v1/campaigns", json=body | {"name": "x"}, headers=h2).status_code == 404
    )  # lista s'është e tij
    assert c.get(f"/v1/campaigns/{cid}", headers=h2).status_code == 404
    assert c.post(f"/v1/campaigns/{cid}/schedule", json={}, headers=h2).status_code == 404
    est = c.get(f"/v1/campaigns/{cid}/estimate", headers=h1).json()
    assert est["recipients"] == 2 and est["total"] == "0.100000"
    assert (
        c.post(f"/v1/campaigns/{cid}/schedule", json={}, headers=h1).json()["status"] == "scheduled"
    )
    assert c.post(f"/v1/campaigns/{cid}/schedule", json={}, headers=h1).status_code == 409
    drive(db, datetime.now(UTC))
    got = c.get(f"/v1/campaigns/{cid}", headers=h1).json()
    assert got["status"] == "completed" and got["stats"]["recipients"] == {"queued": 2}
    rows = c.get(f"/v1/campaigns/{cid}/recipients", params={"status": "queued"}, headers=h1).json()
    assert len(rows) == 2
    assert c.get("/v1/campaigns", headers=h1).json()[0]["id"] == cid
    assert c.get("/v1/campaigns", headers=h2).json() == []
    actions = {a["action"] for a in c.get("/v1/admin/audit", headers=BOOT).json()}
    assert {"campaign.create", "campaign.schedule"} <= actions
