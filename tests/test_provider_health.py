"""Faza 21: shëndeti i provider-ave dhe mesazhet me rezultat të paqartë."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models.sending import Message, MessageStatus
from tests.test_pipeline import send, world  # noqa: F401

BOOT = {"X-Admin-Key": "test-key"}


@pytest.fixture
def api():
    return TestClient(create_app(), headers=BOOT)


def test_provider_health_aggregates_per_provider(db, world, api):  # noqa: F811
    ids = [send(db, key=f"k{i}").id for i in range(6)]
    rows = {m.id: m for m in db.query(Message).all()}
    plan = [
        ("twilio", MessageStatus.DELIVERED, None),
        ("twilio", MessageStatus.DELIVERED, None),
        ("twilio", MessageStatus.DELIVERED, None),
        ("twilio", MessageStatus.FAILED, "twilio_30003"),
        ("twilio", MessageStatus.FAILED, "twilio_outcome_unknown"),
        ("fake", MessageStatus.QUEUED, None),
    ]
    for i, (prov, st, code) in zip(ids, plan, strict=True):
        rows[i].provider, rows[i].status, rows[i].error_code = prov, st, code
    db.commit()
    r = api.get("/v1/admin/providers").json()
    by = {p["provider"]: p for p in r["providers"]}
    tw = by["twilio"]
    assert (tw["total"], tw["delivered"], tw["failed"], tw["in_flight"]) == (5, 3, 2, 0)
    assert tw["delivery_rate"] == 0.6 and tw["unknown_outcome"] == 1
    assert {e["code"] for e in tw["top_errors"]} == {"twilio_30003", "twilio_outcome_unknown"}
    assert tw["last_delivered_at"] is not None
    assert by["fake"]["in_flight"] == 1 and by["fake"]["delivery_rate"] is None
    assert by["fake"]["oldest_in_flight_seconds"] >= 0
    assert r["providers"][0]["provider"] == "twilio"  # më i ngarkuari i pari


def test_window_excludes_old_messages(db, world, api):  # noqa: F811
    from datetime import UTC, datetime, timedelta

    m = send(db, key="old")
    m.created_at = datetime.now(UTC) - timedelta(hours=48)
    db.commit()
    p24 = api.get("/v1/admin/providers", params={"hours": 24}).json()["providers"][0]
    assert p24["total"] == 0  # jashtë dritares
    assert p24["oldest_in_flight_seconds"] > 47 * 3600  # por ende në rrugë: shfaqet, është sinjal
    assert api.get("/v1/admin/providers", params={"hours": 72}).json()["providers"][0]["total"] == 1


def test_unresolved_lists_only_unknown_outcomes_with_masked_numbers(db, world, api):  # noqa: F811
    a, b = send(db, key="a"), send(db, key="b")
    for m, code in ((a, "twilio_outcome_unknown"), (b, "twilio_30003")):
        m.status, m.error_code = MessageStatus.FAILED, code
    db.commit()
    rows = api.get("/v1/admin/messages/unresolved").json()
    assert [r["id"] for r in rows] == [a.public_id]
    assert rows[0]["to"] == "+35569…03" and rows[0]["provider"] == "fake"
    assert "text" not in rows[0]  # pa përmbajtjen e mesazhit


def test_endpoints_need_monitor_permission(api):
    anon = TestClient(create_app())
    assert anon.get("/v1/admin/providers").status_code == 401
    k = api.post(
        "/v1/admin/api-keys", json={"name": "c", "role": "client", "owner_ref": "x"}
    ).json()
    h = {"Authorization": f"Bearer {k['key']}", "X-Admin-Key": ""}
    assert anon.get("/v1/admin/providers", headers=h).status_code == 403
    assert anon.get("/v1/admin/messages/unresolved", headers=h).status_code == 403
