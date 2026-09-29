import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal as D

import pytest

from app.core.config import settings
from app.models.sending import DlrReceipt, MessageStatus
from app.services import messages as svc
from app.services import wallet as wallets
from tests.test_pipeline import OK, fake, send, world  # noqa: F401

SECRET = "s3cret"


@pytest.fixture(autouse=True)
def secrets(monkeypatch):
    monkeypatch.setattr(settings, "dlr_secrets", {"fake": SECRET})


def post(client, body: dict | bytes, secret=SECRET, provider="fake"):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        f"/webhooks/dlr/{provider}", content=raw, headers={"X-Signature": f"sha256={sig}"}
    )


def sent_message(db):
    m = send(db)
    svc.process_one(db)
    return m


def test_signature_required(client, db, world):  # noqa: F811
    m = sent_message(db)
    body = {"provider_message_id": m.provider_message_id, "status": "delivered"}
    assert client.post("/webhooks/dlr/fake", json=body).status_code == 401
    assert post(client, body, secret="wrong").status_code == 401
    assert post(client, body, provider="unconfigured").status_code == 401
    db.refresh(m)
    assert m.status == MessageStatus.SENT


def test_delivered_and_duplicate(client, db, world):  # noqa: F811
    w, _ = world
    m = sent_message(db)
    body = {"provider_message_id": m.provider_message_id, "status": "delivered"}
    assert post(client, body).json() == {"outcome": "applied"}
    assert post(client, body).status_code == 200  # retry i provider-it
    db.expire_all()
    assert m.status == MessageStatus.DELIVERED
    assert wallets.balances(db, w.id) == (D("9.95"), D("0"))
    assert db.query(DlrReceipt).count() == 2


def test_failed_dlr_releases(client, db, world):  # noqa: F811
    w, _ = world
    m = sent_message(db)
    body = {"provider_message_id": m.provider_message_id, "status": "failed", "code": "expired"}
    assert post(client, body).status_code == 200
    db.expire_all()
    assert m.error_code == "expired"
    assert wallets.balances(db, w.id) == (D("10"), D("0"))


def test_unknown_message_asks_provider_to_retry(client, db, world):  # noqa: F811
    r = post(client, {"provider_message_id": "nope", "status": "delivered"})
    assert r.status_code == 503
    assert db.query(DlrReceipt).one().outcome == "unknown_message"


def test_conflicting_dlr_rejected_but_recorded(client, db, world):  # noqa: F811
    m = sent_message(db)
    pid = m.provider_message_id
    assert post(client, {"provider_message_id": pid, "status": "failed"}).status_code == 200
    assert post(client, {"provider_message_id": pid, "status": "delivered"}).status_code == 409
    outcomes = [r.outcome for r in db.query(DlrReceipt).order_by(DlrReceipt.id)]
    assert outcomes == ["applied", "conflict"]


def test_bad_payload(client, db, world):  # noqa: F811
    assert post(client, b"not json").status_code == 422
    assert post(client, {"provider_message_id": "x", "status": "maybe"}).status_code == 422


def test_expire_stale_releases_held_money(db, world):  # noqa: F811
    w, _ = world
    m = sent_message(db)
    from datetime import UTC, datetime

    assert svc.expire_stale(db, timedelta(hours=72)) == 0  # ende e freskët
    later = datetime.now(UTC) + timedelta(hours=73)
    assert svc.expire_stale(db, timedelta(hours=72), now=later) == 1
    db.commit()
    assert m.status == MessageStatus.FAILED and m.error_code == "dlr_timeout"
    assert wallets.balances(db, w.id) == (D("10"), D("0"))
    assert svc.expire_stale(db, timedelta(hours=72), now=later) == 0


def test_stuck_sending_is_reported_not_touched(db, world):  # noqa: F811
    from datetime import UTC, datetime

    m = send(db)
    svc.claim_next(db)  # crash simulim: SENDING pa rezultat
    db.commit()
    later = datetime.now(UTC) + timedelta(hours=1)
    assert svc.stuck_sending(db, timedelta(minutes=10), now=later) == [m]
    assert svc.process_one(db, later) is None  # nuk ridërgohet automatikisht
