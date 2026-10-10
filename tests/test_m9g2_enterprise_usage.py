# ruff: noqa: F811
"""M9-g2 — Enterprise: prova e email-it të faturueshëm (first transition), raportet kumulative, outbox, outage, hot path."""

import inspect
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.db import SessionLocal, engine
from app.core.timeutil import as_utc
from app.models.billing_usage import (
    R_FAILED,
    R_PENDING,
    R_RETRY,
    R_SENT,
    R_SUPERSEDED,
    BillingEvidenceImmutableError,
    BillingUsageReport,
    EmailBillableEvent,
)
from app.models.control_plane import Entitlement
from app.models.email import Email, EmailStatus
from app.services import billing_usage as bu
from app.services import control_plane_client as cc
from app.services import emails
from packages.contracts.control_plane.billing import usage_v1 as bv
from tests.test_central import IS_PG, central_alembic, make_db  # noqa: F401
from tests.test_email import fake_dns, fake_email_provider, opt_in, send, verified  # noqa: F401
from tests.test_pipeline import world  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)
EMAIL_P = uuid.uuid4()


def events(db):
    db.expire_all()
    return list(db.scalars(select(EmailBillableEvent).order_by(EmailBillableEvent.id)))


def entitle(db, e):
    eid = e.enterprise_id
    if not db.scalar(
        select(Entitlement.id).where(
            Entitlement.enterprise_id == eid, Entitlement.channel == "email"
        )
    ):
        db.add(Entitlement(enterprise_id=eid, assignment_id=uuid.uuid4(), product_id=EMAIL_P, product_code="email",
                           channel="email", status="active", revision=1))  # fmt: skip
        db.commit()
    return eid


def move(db, e, *path):
    for to in path:
        emails._move(db, e, to)
    db.commit()


class Client:
    """Klient i rremë i Central: regjistron dorëzimet; mund të dështojë."""

    def __init__(self):
        self.posted, self.fail = [], None

    def post_billing_usage(self, payload):
        if self.fail:
            raise self.fail
        self.posted.append(payload)
        return {"status": "stored"}

    def close(self):
        pass


# --- capture ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,first",
    [
        ((EmailStatus.SENDING, EmailStatus.SENT), "sent"),
        ((EmailStatus.SENDING, EmailStatus.UNKNOWN, EmailStatus.DELIVERED), "delivered"),
        ((EmailStatus.SENDING, EmailStatus.UNKNOWN, EmailStatus.BOUNCED), "bounced"),
        ((EmailStatus.SENDING, EmailStatus.UNKNOWN, EmailStatus.COMPLAINED), "complained"),
    ],
)
def test_each_billable_status_creates_exactly_one_event(db, verified, path, first):
    e = send(db)
    move(db, e, *path)
    ev = events(db)
    assert (
        len(ev) == 1
        and ev[0].first_status == first
        and ev[0].email_id == e.id
        and ev[0].enterprise_id == e.enterprise_id
    )


def test_queued_sending_unknown_and_failed_create_no_event(db, verified):
    e = send(db)
    move(db, e, EmailStatus.SENDING)
    assert events(db) == []
    move(db, e, EmailStatus.UNKNOWN)
    assert events(db) == []  # UNKNOWN s'është i faturueshëm: s'dihet nëse provider-i e pranoi
    f = send(db, key="e2")
    move(db, f, EmailStatus.SENDING, EmailStatus.QUEUED, EmailStatus.SENDING, EmailStatus.FAILED)
    assert events(db) == []


def test_sent_then_delivered_then_complaint_is_still_one_unit(db, verified):
    e = send(db)
    move(db, e, EmailStatus.SENDING, EmailStatus.SENT)
    first = events(db)[0]
    move(db, e, EmailStatus.DELIVERED, EmailStatus.COMPLAINED)
    ev = events(db)
    assert len(ev) == 1 and ev[0].id == first.id and ev[0].first_status == "sent"


def test_duplicate_callback_and_unknown_resolution_never_add_units(db, verified):
    e = send(db)
    emails.process_one(db, datetime.now(UTC) + timedelta(seconds=1))
    pid = e.provider_message_id
    for _ in range(3):  # callback i dyfishtë/trefishtë
        emails.apply_event(db, "fake", pid, "delivered")
        db.commit()
    assert len(events(db)) == 1
    bu.record_first_billable(
        db, e, EmailStatus.DELIVERED
    )  # thirrje e drejtpërdrejtë e përsëritur: ON CONFLICT DO NOTHING
    bu.record_first_billable(db, e, EmailStatus.SENT)
    db.commit()
    assert len(events(db)) == 1


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_concurrent_transitions_create_exactly_one_event(db, verified):
    e = send(db)
    move(db, e, EmailStatus.SENDING, EmailStatus.SENT)
    eid, enterprise = e.id, e.enterprise_id
    barrier, errs = threading.Barrier(8), []

    def w():
        try:
            barrier.wait()
            with SessionLocal() as s:
                row = s.get(Email, eid)
                bu.record_first_billable(s, row, EmailStatus.DELIVERED)
                s.commit()
        except Exception as ex:  # noqa: BLE001
            errs.append(ex)

    ts = [threading.Thread(target=w) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    with SessionLocal() as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(EmailBillableEvent)
                .where(EmailBillableEvent.enterprise_id == enterprise)
            )
            == 1
        )


def test_evidence_is_immutable_orm_and_postgres_trigger(db, verified):
    e = send(db)
    move(db, e, EmailStatus.SENDING, EmailStatus.SENT)
    row = events(db)[0]
    row.first_status = "delivered"
    with pytest.raises(BillingEvidenceImmutableError):
        db.flush()
    db.rollback()
    db.delete(events(db)[0])
    with pytest.raises(BillingEvidenceImmutableError):
        db.flush()
    db.rollback()


def test_pg_triggers_reject_mutation_on_a_migrated_database(make_db):
    """Schema-ja e suitës vjen nga `create_all` (pa trigger-a): trigger-at provohen te një DB e migruar me alembic, me rreshta realë."""
    from sqlalchemy import create_engine

    from tests.test_central import enterprise_alembic

    url = make_db("ent")
    if not url.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    enterprise_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    eid, rid = uuid.uuid4(), uuid.uuid4()
    with (
        eng.begin() as c
    ):  # FK-ja drejt sms_emails hiqet vetëm në këtë DB provë (trigger-at nuk varen prej saj)
        c.execute(
            text(
                "ALTER TABLE sms_email_billable_events DROP CONSTRAINT fk_sms_email_billable_events_email_id_sms_emails"
            )
        )
        c.execute(
            text(
                "INSERT INTO sms_email_billable_events (email_id, enterprise_id, first_status, billable_at, created_at) VALUES (1, :e, 'sent', now(), now())"
            ),
            {"e": eid},
        )
        c.execute(
            text(
                "INSERT INTO sms_billing_usage_reports (report_id, enterprise_id, product_id, report_seq, watermark, cumulative_billable_count, generated_at, payload, payload_hash, content_hash, status, attempts, next_attempt_at, created_at, updated_at) VALUES (:r, :e, :e, 1, 1, 1, now(), '{}', 'h', 'c', 'pending', 0, now(), now(), now())"
            ),
            {"r": rid, "e": eid},
        )
    for sql in (
        "UPDATE sms_email_billable_events SET first_status = 'bounced'",
        "DELETE FROM sms_email_billable_events",
        "TRUNCATE sms_email_billable_events",
        "UPDATE sms_billing_usage_reports SET cumulative_billable_count = 0, watermark = 0",
        "UPDATE sms_billing_usage_reports SET payload = '{\"x\": 1}'",
        "DELETE FROM sms_billing_usage_reports",
    ):
        with eng.connect() as c:
            with pytest.raises(DBAPIError):
                c.execute(text(sql))
            c.rollback()
    with (
        eng.begin() as c
    ):  # delivery state (status/attempts) remains mutable: the outbox must keep working
        c.execute(
            text(
                "UPDATE sms_billing_usage_reports SET status = 'sent', attempts = 1, sent_at = now(), updated_at = now()"
            )
        )
        assert c.execute(text("SELECT count(*) FROM sms_email_billable_events")).scalar() == 1
    eng.dispose()


def test_first_billable_is_atomic_with_the_status_change(db, verified):
    """Rollback i tranzicionit heq edhe provën (e njëjta transaksion): asnjë njësi pa ndryshim statusi, asnjë ndryshim pa njësi."""
    e = send(db)
    move(db, e, EmailStatus.SENDING)
    emails._move(db, e, EmailStatus.SENT)
    db.rollback()
    assert events(db) == [] and db.get(Email, e.id).status == EmailStatus.SENDING


def test_hot_path_adds_one_insert_only_and_no_aggregate_per_transition(db, verified):
    e = send(db)
    move(db, e, EmailStatus.SENDING)
    stmts = []

    def spy(conn, cursor, statement, *a):
        stmts.append(statement.lower())

    event.listen(engine, "before_cursor_execute", spy)
    try:
        emails._move(db, e, EmailStatus.SENT)
        db.flush()
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    db.commit()
    mine = [s for s in stmts if "sms_email_billable_events" in s]
    assert (
        len(mine) == 1
        and mine[0].startswith("insert")
        and "count(" not in mine[0]
        and "max(" not in mine[0]
    )
    assert not any("count(" in s or "max(" in s for s in stmts)


# --- raportet --------------------------------------------------------------------------------------------------


def test_cumulative_report_is_exact_and_watermark_comes_from_the_same_snapshot(db, verified):
    e1, e2, e3 = (
        send(db, key="a"),
        send(db, key="b", to="b@customer.org"),
        send(db, key="c", to="c@customer.org"),
    )
    eid = entitle(db, e1)
    move(db, e1, EmailStatus.SENDING, EmailStatus.SENT)
    move(db, e2, EmailStatus.SENDING)  # nuk numërohet
    assert bu.generate(engine, SessionLocal, now=T0)
    r = db.scalars(select(BillingUsageReport).order_by(BillingUsageReport.report_seq)).all()
    assert (
        len(r) == 1 and r[0].cumulative_billable_count == 1 and r[0].watermark == events(db)[-1].id
    )
    assert (
        r[0].enterprise_id == eid
        and r[0].product_id == EMAIL_P
        and r[0].report_seq == 1
        and r[0].status == R_PENDING
    )
    parsed = bv.BillingUsageReportV1.parse(r[0].payload)
    assert parsed.count == 1 and parsed.watermark <= 10**9
    move(db, e2, EmailStatus.SENT)
    move(db, e3, EmailStatus.SENDING, EmailStatus.SENT)
    bu.generate(engine, SessionLocal, now=T0 + timedelta(minutes=5))
    r = db.scalars(select(BillingUsageReport).order_by(BillingUsageReport.report_seq)).all()
    assert (
        [x.cumulative_billable_count for x in r] == [1, 3]
        and r[1].watermark >= r[0].watermark
        and r[1].cumulative_billable_count <= r[1].watermark
    )


def test_unchanged_state_is_not_re_reported_until_heartbeat(db, verified):
    e = send(db)
    entitle(db, e)
    move(db, e, EmailStatus.SENDING, EmailStatus.SENT)
    assert len(bu.generate(engine, SessionLocal, now=T0)) == 1
    assert bu.generate(engine, SessionLocal, now=T0 + timedelta(seconds=30)) == []
    assert (
        len(bu.generate(engine, SessionLocal, now=T0 + timedelta(hours=1))) == 1
    )  # heartbeat: Central i duhet gjithmonë një raport i freskët


def test_outbox_delivery_retry_and_supersede(db, verified):
    e = send(db)
    entitle(db, e)
    move(db, e, EmailStatus.SENDING, EmailStatus.SENT)
    bu.generate(engine, SessionLocal, now=T0)
    c = Client()
    c.fail = cc.CpTransportError("down")
    out = bu.deliver(SessionLocal, c, now=T0)
    assert out.kind == "network_error" and out.retry == 1
    row = db.scalars(select(BillingUsageReport)).one()
    db.refresh(row)
    assert row.status == R_RETRY and row.attempts == 1 and as_utc(row.next_attempt_at) > T0
    c.fail = None
    assert bu.deliver(SessionLocal, c, now=T0).sent == 0  # backoff ende aktiv
    out = bu.deliver(SessionLocal, c, now=T0 + timedelta(hours=1))
    assert out.sent == 1 and c.posted[0]["report_id"] == str(row.report_id)
    db.refresh(row)
    assert row.status == R_SENT and row.sent_at is not None
    # dy raporte të papërcjella ⇒ vetëm më i riu dërgohet
    move(db, send(db, key="z", to="z@customer.org"), EmailStatus.SENDING, EmailStatus.SENT)
    bu.generate(engine, SessionLocal, now=T0 + timedelta(hours=2))
    move(db, send(db, key="y", to="y@customer.org"), EmailStatus.SENDING, EmailStatus.SENT)
    bu.generate(engine, SessionLocal, now=T0 + timedelta(hours=3))
    c2 = Client()
    out = bu.deliver(SessionLocal, c2, now=T0 + timedelta(hours=3))
    assert (
        out.superseded == 1
        and len(c2.posted) == 1
        and c2.posted[0]["cumulative_billable_count"] == 3
    )
    st = {r.status for r in db.scalars(select(BillingUsageReport))}
    assert R_SUPERSEDED in st


def test_permanent_rejection_marks_failed_and_is_visible_in_stats(db, verified):
    e = send(db)
    entitle(db, e)
    move(db, e, EmailStatus.SENDING, EmailStatus.SENT)
    bu.generate(engine, SessionLocal, now=T0)
    c = Client()
    c.fail = cc.CpReportRejected(409, "conflict")
    out = bu.deliver(SessionLocal, c, now=T0)
    assert out.failed == 1 and not out.ok
    s = bu.stats(db, T0 + timedelta(minutes=1))
    assert (
        s["outbox"].get(R_FAILED) == 1
        and s["cumulative_billable_count"] == 1
        and s["oldest_unsent_age_seconds"] is not None
    )
    assert "@" not in repr(s)


def test_reporter_outage_does_not_stop_email_sending_and_reporting_is_off_the_send_path(
    db, verified, monkeypatch
):
    """Dështimi/ndalesa e raportuesit s'ndikon dërgimin: emaili kalon QUEUED→SENT dhe prova ruhet; asgjë në dërgim nuk thërret Central."""
    c = Client()
    c.fail = cc.CpTransportError("central down")
    e = send(db)
    entitle(db, e)
    emails.process_one(db, datetime.now(UTC) + timedelta(seconds=1))
    assert db.get(Email, e.id).status == EmailStatus.SENT and len(events(db)) == 1
    bu.run_once(engine, SessionLocal, c, now=T0)  # outage: kthen outcome, nuk ngre
    assert db.scalars(select(BillingUsageReport)).one().status == R_RETRY
    e2 = send(db, key="after", to="d@customer.org")
    emails.process_one(db, datetime.now(UTC) + timedelta(seconds=2))
    assert (
        db.get(Email, e2.id).status == EmailStatus.SENT and len(events(db)) == 2
    )  # dërgimi vazhdon
    src = inspect.getsource(emails)
    assert (
        "post_billing_usage" not in src and "ControlPlaneClient" not in src and "httpx" not in src
    )
    for rel in ("app/services/messages.py", "app/queue/dispatch.py"):
        assert "billing_usage" not in (ROOT / rel).read_text()


def test_worker_role_is_separate_and_idle_when_disabled():
    from app import worker

    src = Path(worker.__file__).read_text()
    assert '"billing_usage_reporter"' in src and "run_billing_usage_reporter" in src
    assert "billing_usage_reporting" in inspect.getsource(worker.run_billing_usage_reporter)
    assert cc.BILLING_REPORT_SCOPE == "billing:report"


# --- Enterprise → Central (e2e) ---------------------------------------------------------------------------------


def test_e2e_enterprise_reports_are_ingested_by_central_with_scope_billing_report(
    db, verified, make_db
):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from apps.central.main import create_app
    from apps.central.models.billing_usage import BillingUsageReport as CentralReport
    from apps.central.services import enterprise_products as cep
    from apps.central.services import products as cprod
    from apps.central.services import service_auth
    from tests.test_central_sync_api import assertion, auth, keypair

    e = send(db)
    eid = entitle(db, e)
    move(db, e, EmailStatus.SENDING, EmailStatus.SENT)
    url = make_db()
    central_alembic(url, "upgrade", "head")
    ceng = create_engine(url)
    private, public = keypair()
    with Session(ceng, expire_on_commit=False) as s:
        from apps.central.models.enterprise import Enterprise as CEnterprise

        s.add(CEnterprise(id=eid, name="Acme", status="active"))
        s.flush()
        prod_ = cprod.create(s, "email", "Email", "email")
        cep.assign_product(s, eid, prod_.id)
        service_auth.create_client(s, "ent", ["billing:report"], [eid])
        service_auth.add_key(s, "ent", "k1", public)
        s.commit()
        pid = prod_.id
    # kthe produktin Central si entitlement Enterprise (cp.v1) — product_id i njëjtë
    ent_row = db.scalars(select(Entitlement).where(Entitlement.enterprise_id == eid)).one()
    ent_row.product_id = pid
    db.commit()
    http = TestClient(create_app(ceng))

    class Http(Client):
        def post_billing_usage(self, payload):
            tok = assertion(private, client="ent", kid="k1", scope="billing:report")
            r = http.post("/internal/billing/usage-reports", json=payload, headers=auth(tok))
            if r.status_code in (409, 413, 422):
                raise cc.CpReportRejected(r.status_code, "rejected")
            assert r.status_code in (200, 201), r.text
            return r.json()

    now = datetime.now(UTC)
    out = bu.run_once(engine, SessionLocal, Http(), now=now)
    assert out.ok and out.sent == 1
    with Session(ceng) as s:
        row = s.scalars(select(CentralReport)).one()
        assert (
            row.cumulative_billable_count == 1
            and row.enterprise_id == eid
            and row.product_id == pid
        )
    # i njëjti raport dërguar sërish (rikuperim pas crash) ⇒ dublikat, pa rresht të dytë
    again = db.scalars(select(BillingUsageReport)).one()
    assert Http().post_billing_usage(again.payload)["status"] == "duplicate"
    ceng.dispose()
