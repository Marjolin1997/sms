from datetime import UTC, datetime, timedelta

from app.models.sending import Message, MessageStatus
from app.services import messages as svc
from tests.test_console_api import key, raw_client  # noqa: F401
from tests.test_pipeline import send, world  # noqa: F401


def _seed(db):
    """3 SMS: një i dorëzuar, një i dështuar (absent_subscriber), një në pritje."""
    a, b = send(db, key="a"), send(db, key="b", to="+355691230009")
    send(db, key="c")
    a, b = db.get(Message, a.id), db.get(Message, b.id)
    svc.process_one(db)
    svc.process_one(db)
    svc.apply_dlr(db, "fake", a.provider_message_id, delivered=True)
    svc.apply_dlr(db, "fake", b.provider_message_id, delivered=False, code="absent_subscriber")
    db.commit()


def test_overview_kpis_series_breakdown(db, world, raw_client):  # noqa: F811
    _seed(db)
    h = key(raw_client, "client", "c1")
    r = raw_client.get("/v1/analytics/overview", headers=h)
    assert r.status_code == 200
    d = r.json()
    cur = d["current"]
    assert cur["total"] == 3 and cur["delivered"] == 1 and cur["failed"] == 1
    assert cur["in_flight"] == 1 and cur["delivery_rate"] == 50.0
    assert cur["spend"] == [{"currency": "EUR", "amount": "0.050000"}]
    assert d["previous"]["total"] == 0 and d["previous"]["delivery_rate"] is None
    assert len(d["series"]) == 30 and d["series"][-1]["total"] == 3
    assert sum(x["total"] for x in d["series"]) == 3
    assert d["top_errors"] == [
        {"key": "absent_subscriber", "total": 1, "delivered": 0, "failed": 1, "delivery_rate": 0.0}
    ]
    assert d["by_country"][0]["key"] == "AL" and d["by_country"][0]["total"] == 3
    assert d["by_sender"][0]["key"] == "ACME"


def test_overview_validation_and_isolation(db, world, raw_client):  # noqa: F811
    _seed(db)
    h1, h2 = key(raw_client, "client", "c1"), key(raw_client, "client", "c2")
    c = raw_client
    assert c.get("/v1/analytics/overview", headers=h2).json()["current"]["total"] == 0
    assert (
        c.get("/v1/analytics/overview", params={"owner_ref": "c1"}, headers=h2).status_code == 404
    )
    assert c.get("/v1/analytics/overview", params={"channel": "fax"}, headers=h1).status_code == 422
    bad = {"date_from": "2026-01-10", "date_to": "2026-01-01"}
    assert c.get("/v1/analytics/overview", params=bad, headers=h1).status_code == 422
    long = {"date_from": "2020-01-01", "date_to": "2026-01-01"}
    assert c.get("/v1/analytics/overview", params=long, headers=h1).status_code == 422
    assert c.get("/v1/analytics/overview").status_code == 401
    # Stafi duhet të zgjedhë llogarinë
    staff = key(c, "support")
    assert c.get("/v1/analytics/overview", headers=staff).status_code == 422
    assert (
        c.get("/v1/analytics/overview", params={"owner_ref": "c1"}, headers=staff).status_code
        == 200
    )


def test_range_excludes_other_days_and_compares_previous(db, world, raw_client):  # noqa: F811
    _seed(db)
    old = datetime.now(UTC) - timedelta(days=40)
    for m in db.query(Message).all()[:2]:
        m.created_at = old  # e shmang triggerin: created_at nuk është kolonë append-only
    db.commit()
    h = key(raw_client, "client", "c1")
    today = datetime.now(UTC).date()
    r = raw_client.get(
        "/v1/analytics/overview",
        params={
            "date_from": (today - timedelta(days=29)).isoformat(),
            "date_to": today.isoformat(),
        },
        headers=h,
    ).json()
    assert r["current"]["total"] == 1
    assert r["previous"]["total"] == 2  # 30 ditët para periudhës


def test_export_csv_and_formula_injection(db, world, raw_client):  # noqa: F811
    m = send(db, key="x")
    db.get(Message, m.id).sender = "ACME"
    db.commit()
    h = key(raw_client, "client", "c1")
    r = raw_client.get("/v1/analytics/export.csv", headers=h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.lstrip("﻿").strip().splitlines()
    assert lines[0].startswith("id,created_at,to,sender") and len(lines) == 2
    assert "hello" not in r.text  # teksti nuk eksportohet
    from app.api.analytics import _safe

    assert _safe("=cmd|x") == "'=cmd|x" and _safe("+1") == "'+1" and _safe("ok") == "ok"
    assert _safe(None) == "" and MessageStatus.QUEUED == "queued"
