"""Faza 15: raport përdorimi, eksport CSV, njoftim për bilancë të ulët."""

import csv
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.models.events import Event
from app.models.sending import Message
from app.services import messages as svc
from app.services import wallet as wallets
from tests.test_console_api import key
from tests.test_email import fake_dns, fake_email_provider, verified  # noqa: F401
from tests.test_pipeline import OK, send, world  # noqa: F401

BOOT = {"X-Admin-Key": "test-key"}


@pytest.fixture
def c(raw_client_fixture=None):
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app())


def deliver_all(db):
    while svc.process_one(db):
        pass
    for m in db.scalars(select(Message)).all():
        if m.provider_message_id and m.status.value == "sent":
            svc.apply_dlr(db, "fake", m.provider_message_id, delivered=True)
    db.commit()


# --- Përdorimi ------------------------------------------------------------------


def test_usage_counts_days_and_cost(db, world, c):  # noqa: F811
    h = key(c, "client", "c1")
    send(db, key="a", to=OK)
    send(db, key="b", to="+355691230004")
    deliver_all(db)
    today = datetime.now(UTC).date()
    r = c.get("/v1/reports/usage", params={"from": str(today - timedelta(days=2))}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [d["date"] for d in body["days"]][-1] == str(today) and len(body["days"]) == 3
    last = body["days"][-1]["sms"]
    assert last["count"] == 2 and last["delivered"] == 2 and last["segments"] == 2
    assert D(last["cost"]) == D("0.10")
    assert body["days"][0]["sms"]["count"] == 0  # ditët pa aktivitet dalin me zero
    assert body["totals"]["sms"]["count"] == 2 and D(body["totals"]["sms"]["cost"]) == D("0.10")


def test_usage_isolated_and_validates_range(db, world, c):  # noqa: F811
    send(db, key="a")
    other = key(c, "client", "c2")
    assert c.get("/v1/reports/usage", headers=other).json()["totals"]["sms"]["count"] == 0
    mine = key(c, "client", "c1")
    assert c.get("/v1/reports/usage", params={"owner_ref": "c1"}, headers=other).status_code == 404
    bad = c.get(
        "/v1/reports/usage", params={"from": "2030-01-02", "to": "2030-01-01"}, headers=mine
    )
    assert bad.status_code == 422
    huge = c.get(
        "/v1/reports/usage", params={"from": "2020-01-01", "to": "2026-01-01"}, headers=mine
    )
    assert huge.status_code == 422


def test_usage_requires_permission(c):
    assert c.get("/v1/reports/usage").status_code == 401
    approver = key(c, "approver")
    assert (
        c.get("/v1/reports/usage", params={"owner_ref": "c1"}, headers=approver).status_code == 403
    )


# --- CSV ------------------------------------------------------------------------


def test_messages_csv(db, world, c):  # noqa: F811
    h = key(c, "client", "c1")
    send(db, key="a", to=OK)
    r = c.get("/v1/reports/messages.csv", headers=h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text.lstrip("﻿"))))
    assert rows[0][:4] == ["id", "created_at", "to", "sender"]
    assert rows[1][2] == "+355691230003" and rows[1][3] == "ACME" and rows[1][4] == "queued"
    assert len(rows) == 2
    # audit
    logs = c.get("/v1/admin/audit", headers=BOOT).json()
    assert any(x["action"] == "report.export" for x in logs)


def test_csv_is_owner_scoped(db, world, c):  # noqa: F811
    send(db, key="a")
    other = key(c, "client", "c2")
    rows = list(
        csv.reader(io.StringIO(c.get("/v1/reports/messages.csv", headers=other).text.lstrip("﻿")))
    )
    assert len(rows) == 1  # vetëm titujt


def test_csv_neutralizes_formulas():
    from app.api.reports import _safe

    assert _safe("=HYPERLINK(1)") == "'=HYPERLINK(1)" and _safe("@x") == "'@x"
    assert (
        _safe("+1+1") == "'+1+1"
        and _safe("+35569") == "+35569"
        and _safe("plain") == "plain"
        and _safe(None) == ""
    )


def test_emails_csv(db, verified, c):  # noqa: F811
    from tests.test_email import send as esend

    esend(db, key="e1", to="ana@customer.org", subject="=cmd()")
    r = c.get("/v1/reports/emails.csv", headers=key(c, "client", "c1"))
    rows = list(csv.reader(io.StringIO(r.text.lstrip("\ufeff"))))
    assert r.status_code == 200 and rows[1][2] == "ana@customer.org"
    assert rows[1][4] == "'=cmd()"  # formula e neutralizuar


# --- Bilanc i ulët -------------------------------------------------------------


def events_of(db, kind="wallet.low_balance"):
    return db.scalars(select(Event).where(Event.type == kind)).all()


def test_low_balance_event_fires_once_and_rearms(db, world):  # noqa: F811
    w, _ = world  # 10.00 EUR
    wallets.set_low_balance_threshold(db, w.id, "9.90")
    db.commit()
    assert events_of(db) == []
    send(db, key="a")  # 9.95 (mbi prag)
    assert events_of(db) == []
    send(db, key="b")  # 9.90 (jo më pak)
    assert events_of(db) == []
    send(db, key="c")  # 9.85 → bie nën prag
    evs = events_of(db)
    assert len(evs) == 1 and evs[0].data["threshold"] == "9.900000"
    send(db, key="d")  # bie më tej: pa event të dytë
    assert len(events_of(db)) == 1
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "0.2", wallets.TopupMethod.CASH).id)
    db.commit()
    assert db.get(type(w), w.id).low_balance_notified is False  # rifutet
    for i in range(10):
        send(db, key=f"x{i}")
        if len(events_of(db)) == 2:
            break
    assert len(events_of(db)) == 2


def test_setting_threshold_above_balance_notifies_immediately(db, world):  # noqa: F811
    w, _ = world
    wallets.set_low_balance_threshold(db, w.id, "50")
    db.commit()
    assert len(events_of(db)) == 1


def test_alert_endpoint_owner_scoped(db, world, c):  # noqa: F811
    w, _ = world
    mine, other = key(c, "client", "c1"), key(c, "client", "c2")
    assert (
        c.put(f"/v1/wallets/{w.id}/alert", json={"threshold": "5"}, headers=other).status_code
        == 404
    )
    r = c.put(f"/v1/wallets/{w.id}/alert", json={"threshold": "5"}, headers=mine)
    assert r.status_code == 200 and D(r.json()["low_balance_threshold"]) == D("5")
    listed = c.get("/v1/wallets", headers=mine).json()[0]
    assert D(listed["low_balance_threshold"]) == D("5") and listed["low_balance"] is False
    assert (
        c.put(f"/v1/wallets/{w.id}/alert", json={"threshold": "-1"}, headers=mine).status_code
        == 422
    )
    off = c.put(f"/v1/wallets/{w.id}/alert", json={"threshold": None}, headers=mine)
    assert off.json()["low_balance_threshold"] is None


def test_webhook_filter_accepts_wallet_events():
    from app.services.events import valid_filter

    assert valid_filter("wallet.low_balance") and valid_filter("wallet.*")
