"""Faza 17: SMS hyrës (MO): inbox, fjalë kyçe, përgjigje automatike, idempotencë, GDPR."""

import hashlib
import hmac
import json

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.events import Event
from app.models.inbound import InboundMessage
from app.models.sending import Message
from app.services import consent, sender_ids
from app.services import contacts as contacts_svc
from tests.test_console_api import key
from tests.test_pipeline import world  # noqa: F401

NUM = "+355690000001"
PHONE = "+355691230003"
BOOT = {"X-Admin-Key": "test-key"}


@pytest.fixture
def mo(raw_client_, db, monkeypatch, world):  # noqa: F811
    monkeypatch.setattr(settings, "dlr_secrets", {"fake": "sec"})
    s = sender_ids.request(db, "c1", "AL", NUM)
    sender_ids.approve(db, s.id, "admin")
    db.commit()

    def post(text, frm=PHONE, to=NUM, id_=None, secret="sec"):
        body = {"to": to, "from": frm, "text": text} | ({"id": id_} if id_ else {})
        raw = json.dumps(body).encode()
        sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return raw_client_.post("/webhooks/inbound/fake", content=raw, headers={"X-Signature": sig})

    return post


@pytest.fixture
def raw_client_():
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app())


def h(c, owner="c1"):
    return key(c, "client", owner)


def test_every_inbound_message_is_stored_and_listed(mo, db, raw_client_):
    assert mo("Hello there").json() == {"outcome": "ignored"}
    assert mo("STOP").json() == {"outcome": "opt_out"}
    assert mo("x", to="+355690000009").json() == {"outcome": "unrouted"}
    r = raw_client_.get("/v1/inbox", headers=h(raw_client_)).json()
    assert [m["text"] for m in r["items"]] == ["STOP", "Hello there"]
    assert r["items"][0]["action"] == "opt_out" and r["items"][0]["from"] == "355691230003"
    assert r["unread"] == 2 and r["next_before_id"] is None


def test_inbox_is_owner_scoped(mo, raw_client_):
    mo("Hi")
    assert raw_client_.get("/v1/inbox", headers=h(raw_client_, "c2")).json()["items"] == []
    other = h(raw_client_, "c2")
    assert (
        raw_client_.get("/v1/inbox", params={"owner_ref": "c1"}, headers=other).status_code == 404
    )


def test_paging_search_unread_and_mark_read(mo, raw_client_):
    for t in ["one", "two apples", "three"]:
        mo(t)
    c, hd = raw_client_, h(raw_client_)
    page = c.get("/v1/inbox", params={"limit": 2}, headers=hd).json()
    assert [m["text"] for m in page["items"]] == ["three", "two apples"] and page["next_before_id"]
    rest = c.get("/v1/inbox", params={"before_id": page["next_before_id"]}, headers=hd).json()
    assert [m["text"] for m in rest["items"]] == ["one"]
    assert [
        m["text"] for m in c.get("/v1/inbox", params={"q": "apple"}, headers=hd).json()["items"]
    ] == ["two apples"]
    assert (
        c.get("/v1/inbox", params={"q": "%"}, headers=hd).json()["items"] == []
    )  # wildcard s'vepron
    first = page["items"][0]["id"]
    assert c.post("/v1/inbox/read", json={"ids": [first]}, headers=hd).json() == {"marked": 1}
    assert c.get("/v1/inbox/unread", headers=hd).json() == {"unread": 2}
    assert len(c.get("/v1/inbox", params={"unread": True}, headers=hd).json()["items"]) == 2
    assert c.post("/v1/inbox/read", json={}, headers=hd).json() == {"marked": 2}
    assert c.get("/v1/inbox/unread", headers=hd).json() == {"unread": 0}


def test_duplicate_provider_message_is_not_stored_twice(mo, db):
    assert mo("Hi", id_="prov-1").json() == {"outcome": "ignored"}
    assert mo("Hi", id_="prov-1").json() == {"outcome": "duplicate"}
    assert len(db.scalars(select(InboundMessage)).all()) == 1


def test_event_message_received_emitted(mo, db):
    mo("Where is my order?")
    ev = db.scalars(select(Event).where(Event.type == "message.received")).one()
    assert ev.data["text"] == "Where is my order?" and ev.data["from"] == "355691230003"


def test_contact_is_linked_by_phone(mo, db):
    c, _ = contacts_svc.upsert(db, "c1", phone=PHONE, first_name="Ana")
    db.commit()
    mo("Hi")
    db.expire_all()
    assert db.scalars(select(InboundMessage)).one().contact_id == c.id


# --- Fjalë kyçe -----------------------------------------------------------------


def test_keyword_crud_and_validation(raw_client_):
    c, hd = raw_client_, h(raw_client_)
    ok = c.put("/v1/keywords", json={"keyword": "HELP", "reply_text": "Call us"}, headers=hd)
    assert ok.status_code == 200 and ok.json()["keyword"] == "help"
    assert (
        c.put("/v1/keywords", json={"keyword": "help", "reply_text": "New"}, headers=hd).json()[
            "reply_text"
        ]
        == "New"
    )
    assert len(c.get("/v1/keywords", headers=hd).json()) == 1  # ndryshim, jo dublikatë
    for bad in ["stop", "START", "two words", "a", "!!"]:
        assert c.put("/v1/keywords", json={"keyword": bad}, headers=hd).status_code == 422, bad
    kid = c.get("/v1/keywords", headers=hd).json()[0]["id"]
    assert c.delete(f"/v1/keywords/{kid}", headers=h(c, "c2")).status_code == 404
    assert c.delete(f"/v1/keywords/{kid}", headers=hd).status_code == 204
    assert c.get("/v1/keywords", headers=hd).json() == []


def test_keyword_auto_reply_sends_and_charges(mo, db, raw_client_):
    consent.record(db, "c1", "sms", PHONE, "opt_in", "x", "form", "u", "evidence")
    db.commit()
    raw_client_.put(
        "/v1/keywords",
        json={"keyword": "help", "reply_text": "Call 0800 123"},
        headers=h(raw_client_),
    )
    assert mo("HELP me please").json() == {"outcome": "keyword"}
    db.expire_all()
    inbound = db.scalars(select(InboundMessage)).one()
    assert (
        inbound.keyword == "help" and inbound.reply_status == "queued" and inbound.reply_message_id
    )
    reply = db.scalars(select(Message)).one()
    assert reply.text == "Call 0800 123" and reply.destination == "355691230003"
    assert reply.sender == "355690000001"


def test_auto_reply_failure_does_not_lose_the_inbound_message(mo, db, raw_client_):
    raw_client_.put(
        "/v1/keywords", json={"keyword": "info", "reply_text": "hi"}, headers=h(raw_client_)
    )
    from app.services import messages as msg_svc

    def boom(*a, **k):
        raise msg_svc.SendingPaused("paused")

    import app.services.inbox as inbox_mod

    original = inbox_mod.msg_svc.submit
    inbox_mod.msg_svc.submit = boom
    try:
        assert mo("info").json() == {"outcome": "keyword"}
    finally:
        inbox_mod.msg_svc.submit = original
    db.expire_all()
    row = db.scalars(select(InboundMessage)).one()
    assert row.reply_status.startswith("failed:") and row.text == "info"


def test_stop_word_is_never_a_custom_keyword_reply(mo, db, raw_client_):
    assert mo("stop").json() == {"outcome": "opt_out"}
    db.expire_all()
    assert consent.check(db, "c1", "sms", PHONE, "transactional").reason == "blocked:stop_keyword"


# --- GDPR -----------------------------------------------------------------------


def test_erase_scrubs_inbox_and_export_includes_it(mo, db, raw_client_):
    c, _ = contacts_svc.upsert(db, "c1", phone=PHONE)
    db.commit()
    mo("my secret question")
    hd = h(raw_client_)
    exp = raw_client_.get(f"/v1/contacts/{c.id}/export", headers=hd).json()
    assert exp["inbound_sms"][0]["text"] == "my secret question"
    assert raw_client_.delete(f"/v1/contacts/{c.id}", headers=hd).status_code == 204
    db.expire_all()
    row = db.scalars(select(InboundMessage)).one()
    assert row.text == "[erased]" and row.from_number == "erased"


def test_permissions(raw_client_):
    assert raw_client_.get("/v1/inbox").status_code == 401
    approver = key(raw_client_, "approver")
    assert (
        raw_client_.get("/v1/inbox", params={"owner_ref": "c1"}, headers=approver).status_code
        == 403
    )


def test_auto_reply_cooldown_per_number(mo, db, raw_client_):
    raw_client_.put(
        "/v1/keywords", json={"keyword": "help", "reply_text": "hi"}, headers=h(raw_client_)
    )
    mo("help", id_="a")
    mo("help", id_="b")
    db.expire_all()
    rows = db.scalars(select(InboundMessage).order_by(InboundMessage.id)).all()
    assert [r.reply_status for r in rows] == ["queued", "skipped:cooldown"]
    assert len(db.scalars(select(Message)).all()) == 1
