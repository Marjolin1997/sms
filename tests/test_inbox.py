import hashlib
import hmac
import json

from app.core.config import settings
from app.models.inbox import InboundMessage
from app.services import consent, sender_ids
from app.services import contacts as contacts_svc
from tests.test_console_api import key, raw_client  # noqa: F401
from tests.test_pipeline import OK, send, world  # noqa: F401

NUM = "+355690000001"
PHONE = OK  # numri i marrësit


def _post(c, body, secret="sec"):
    raw = json.dumps(body).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return c.post("/webhooks/inbound/fake", content=raw, headers={"X-Signature": sig})


def _setup(db, monkeypatch):
    monkeypatch.setattr(settings, "dlr_secrets", {"fake": "sec"})
    s = sender_ids.request(db, "c1", "AL", NUM)
    sender_ids.approve(db, s.id, "admin")
    consent.record(db, "c1", "sms", PHONE, "opt_in", "x", "form", "u", "evidence")
    db.commit()


def test_inbound_stored_threaded_and_marked_read(db, world, raw_client, monkeypatch):  # noqa: F811
    _setup(db, monkeypatch)
    send(db, key="out1", text="Your code is 1234")
    h = key(raw_client, "client", "c1")
    assert _post(raw_client, {"to": NUM, "from": PHONE, "text": "Thanks!", "id": "p1"}).json() == {
        "outcome": "ignored"
    }
    _post(raw_client, {"to": NUM, "from": "+355691239999", "text": "Who is this?", "id": "p2"})
    assert raw_client.get("/v1/inbox/unread-count", headers=h).json() == {"unread": 2}

    t = raw_client.get("/v1/inbox/threads", headers=h).json()
    assert [x["number"] for x in t["items"]] == ["355691239999", PHONE.lstrip("+")]
    assert t["items"][0]["unread"] == 1 and t["items"][0]["preview"] == "Who is this?"
    assert [
        x["number"]
        for x in raw_client.get("/v1/inbox/threads", params={"q": "39999"}, headers=h).json()[
            "items"
        ]
    ] == ["355691239999"]

    conv = raw_client.get(f"/v1/inbox/threads/{PHONE}", headers=h).json()
    assert [(i["direction"], i["text"]) for i in conv["items"]] == [
        ("out", "Your code is 1234"),
        ("in", "Thanks!"),
    ]
    assert conv["reply_from"] == NUM.lstrip("+") and conv["blocked"] is None

    assert raw_client.post(f"/v1/inbox/threads/{PHONE}/read", headers=h).json() == {"marked": 1}
    assert raw_client.get("/v1/inbox/unread-count", headers=h).json() == {"unread": 1}
    only = raw_client.get("/v1/inbox/threads", params={"unread_only": True}, headers=h).json()
    assert [x["number"] for x in only["items"]] == ["355691239999"]


def test_inbound_idempotent_and_stop_is_pre_read(db, world, raw_client, monkeypatch):  # noqa: F811
    _setup(db, monkeypatch)
    h = key(raw_client, "client", "c1")
    body = {"to": NUM, "from": PHONE, "text": "hello", "id": "dup"}
    assert _post(raw_client, body).json() == {"outcome": "ignored"}
    assert _post(raw_client, body).json() == {"outcome": "duplicate"}
    assert db.query(InboundMessage).count() == 1
    assert _post(raw_client, {"to": NUM, "from": PHONE, "text": "STOP"}).json() == {
        "outcome": "opt_out"
    }
    assert raw_client.get("/v1/inbox/unread-count", headers=h).json() == {
        "unread": 1
    }  # STOP s'numërohet
    conv = raw_client.get(f"/v1/inbox/threads/{PHONE}", headers=h).json()
    assert conv["blocked"] == "blocked:stop_keyword"
    assert conv["items"][-1]["keyword_action"] == "opt_out"


def test_inbox_isolation_permissions_validation(db, world, raw_client, monkeypatch):  # noqa: F811
    _setup(db, monkeypatch)
    _post(raw_client, {"to": NUM, "from": PHONE, "text": "hi"})
    h1, h2 = key(raw_client, "client", "c1"), key(raw_client, "client", "c2")
    c = raw_client
    assert c.get("/v1/inbox/threads", headers=h2).json()["items"] == []
    assert c.get("/v1/inbox/threads", params={"owner_ref": "c1"}, headers=h2).status_code == 404
    assert c.get(f"/v1/inbox/threads/{PHONE}", headers=h2).json()["items"] == []
    assert c.post(f"/v1/inbox/threads/{PHONE}/read", headers=h2).json() == {"marked": 0}
    assert c.get("/v1/inbox/unread-count", headers=h1).json() == {"unread": 1}
    assert c.get("/v1/inbox/threads/abc", headers=h1).status_code == 422
    assert c.get("/v1/inbox/threads").status_code == 401
    assert c.get("/v1/inbox/threads", headers=key(c, "support")).status_code == 422


def test_erasure_deletes_inbound(db, world, monkeypatch):  # noqa: F811
    _setup(db, monkeypatch)
    from app.services import inbox

    inbox.record_inbound(db, "c1", "fake", NUM, PHONE, "private reply")
    c, _ = contacts_svc.upsert(db, "c1", phone=PHONE)
    db.commit()
    contacts_svc.erase(db, "c1", c.id, "admin")
    db.commit()
    assert db.query(InboundMessage).count() == 0


def test_purge_old(db, world):  # noqa: F811
    from datetime import UTC, datetime, timedelta

    from app.services import inbox

    inbox.record_inbound(db, "c1", "fake", NUM, PHONE, "old")
    db.commit()
    assert inbox.purge_old(db, 365) == 0
    assert inbox.purge_old(db, 365, now=datetime.now(UTC) + timedelta(days=400)) == 1
