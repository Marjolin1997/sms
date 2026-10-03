# ruff: noqa: F811
"""Patch i transaksionit të email: provider.send/SMTP nuk ekzekutohet kurrë me transaksion DB të hapur
dhe pa SQL midis COMMIT#1 dhe kthimit të provider-it. Delivery semantics (SENDING, attempts, retry,
Message-ID) mbeten të pandryshuara; dritaret e mbetura të dështimit dokumentohen në QUEUE_SEMANTICS.md."""

import os
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import app.providers as providers
from app.core.db import SessionLocal, engine
from app.models.email import Email, EmailDomain, EmailEvent, EmailStatus
from app.providers.base import SendResult
from app.services import emails
from tests.test_email import FROM, fake_dns, fake_email_provider, verified  # noqa: F401
from tests.test_pipeline import world  # noqa: F401

PG = engine.dialect.name == "postgresql"
T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)
NOW = lambda: datetime.now(UTC) + timedelta(seconds=5)  # noqa: E731


class Probe:
    """Provider që regjistron çdo SQL/commit si ngjarje të renditura, plus shënuesit e thirrjes."""

    name = "fake"

    def __init__(self, fn=None):
        self.log, self.calls, self.fn = [], [], fn
        event.listen(engine, "before_cursor_execute", self._sql)
        event.listen(engine, "commit", self._commit)

    def _sql(self, *a):
        self.log.append("sql")

    def _commit(self, *a):
        self.log.append("commit")

    def close(self):
        event.remove(engine, "before_cursor_execute", self._sql)
        event.remove(engine, "commit", self._commit)

    def send(self, req):
        self.log.append("SEND_START")
        self.calls.append(req)
        try:
            return self.fn(req) if self.fn else SendResult(f"msg-{len(self.calls)}")
        finally:
            self.log.append("SEND_END")


@pytest.fixture
def probe(verified):  # noqa: F811
    p = Probe()
    old = providers._email_registry["fake"]
    providers._email_registry["fake"] = p
    yield p
    providers._email_registry["fake"] = old
    p.close()


def mk(db, key="k", to="ana@customer.org"):
    e = emails.submit(db, "c1", key, FROM, to, subject="Hello", text="Body")
    db.commit()
    return e


# --- Asnjë transaksion / SQL gjatë provider-it ---------------------------------------------------------


def test_no_sql_between_commit_1_and_the_end_of_the_provider_call(db, world, probe):  # noqa: F811
    mk(db)
    probe.log.clear()
    emails.process_one(db, NOW())
    log = probe.log
    start, end = log.index("SEND_START"), log.index("SEND_END")
    assert log[start - 1] == "commit"  # COMMIT#1 është ngjarja menjëherë para provider-it
    assert "sql" not in log[start - 1 : end + 1]  # as te boshllëku commit→send, as brenda send
    assert log[end + 1] == "sql"  # SQL rifillon vetëm te ack/finalize


def test_provider_runs_outside_any_transaction_and_without_a_pooled_connection(db, world, probe):  # noqa: F811
    mk(db)
    seen = {}
    probe.fn = lambda req: (
        seen.update(in_tx=db.in_transaction(), out=engine.pool.checkedout() if PG else 0)
        or SendResult("m-1")
    )
    emails.process_one(db, NOW())
    assert seen == {"in_tx": False, "out": 0}  # as sesioni, as pool-i nuk mbajnë lidhje gjatë SMTP


def test_expire_on_commit_true_cannot_trigger_a_hidden_select_during_the_provider(db, world, probe):  # noqa: F811
    """Sesion me expire_on_commit=True (parazgjedhja e SQLAlchemy): payload-i është materializuar në
    primitive, prandaj COMMIT#1 s'shkakton asnjë lazy-load gjatë provider-it."""
    e = mk(db)
    probe.log.clear()
    with Session(bind=engine, expire_on_commit=True) as s:
        got = emails.process_one(s, NOW())
        assert got.id == e.id and got.status == EmailStatus.SENT
    start, end = probe.log.index("SEND_START"), probe.log.index("SEND_END")
    assert "sql" not in probe.log[start - 1 : end + 1]


def test_send_payload_contains_only_primitives():
    from dataclasses import fields

    assert {f.name for f in fields(emails.EmailSendPayload)} >= {"dkim_private_pem", "domain"}
    for f in fields(emails.EmailSendPayload):
        assert str(f.type) in ("<class 'str'>", "<class 'bytes'>", "str | None"), f.name  # pa ORM


# --- Delivery semantics të pandryshuara ----------------------------------------------------------------


def test_success_keeps_queued_sending_sent_message_id_and_dkim(db, world, probe):  # noqa: F811
    e = mk(db)
    got = emails.process_one(db, NOW())
    assert (
        got.status == EmailStatus.SENT and got.attempts == 1 and got.provider_message_id == "msg-1"
    )
    req = probe.calls[0]
    assert req.reference == e.public_id and b"DKIM-Signature:" in req.raw
    assert req.message_id in req.raw.decode() and req.from_email == FROM
    trail = [x.to_status for x in db.scalars(select(EmailEvent).where(EmailEvent.email_id == e.id))]
    assert trail == ["queued", "sending", "sent"]


def test_temporary_and_permanent_failures_keep_their_semantics(db, world, probe):  # noqa: F811
    from app.providers import ProviderError

    def fail(req):
        local = req.to_email.split("@")[0]
        raise ProviderError("temp_err" if local == "t" else "perm_err", temporary=local == "t")

    probe.fn = fail
    t, p = mk(db, "t", "t@customer.org"), mk(db, "p", "p@customer.org")
    got_t = emails.process_one(db, T0)
    assert got_t.id == t.id and got_t.status == EmailStatus.QUEUED
    assert got_t.error_code == "temp_err" and got_t.attempts == 1
    assert (got_t.next_attempt_at.replace(tzinfo=UTC) - T0).total_seconds() == 30
    got_p = emails.process_one(db, T0)
    assert got_p.id == p.id and got_p.status == EmailStatus.FAILED and got_p.attempts == 1


def test_undecryptable_dkim_key_is_still_a_temporary_provider_exception(db, world, probe):  # noqa: F811
    e = mk(db)
    db.execute(EmailDomain.__table__.update().values(dkim_private_key_enc=b"garbage"))
    db.commit()
    db.expire_all()  # update masiv: identity map s'duhet të japë domenin e vjetër
    got = emails.process_one(db, T0)  # e njëjta klasifikim si para patch-it (deferred pas COMMIT#1)
    assert got.id == e.id and got.status == EmailStatus.QUEUED
    assert got.error_code == "provider_exception" and got.attempts == 1 and probe.calls == []


# --- Dështime transaksioni -------------------------------------------------------------------------------


def _fail_nth_commit(session, n):
    count = {"n": 0}

    def before_commit(s):
        count["n"] += 1
        if count["n"] == n:
            raise OperationalError("COMMIT", {}, Exception("simulated commit failure"))

    event.listen(session, "before_commit", before_commit)


def test_if_commit_1_fails_the_provider_is_never_called(db, world, probe):  # noqa: F811
    e = mk(db)
    with SessionLocal() as s:
        _fail_nth_commit(s, 1)
        with pytest.raises(OperationalError):
            emails.process_one(s, NOW())
        s.rollback()
    assert probe.calls == []  # DB commit dështoi → asnjë email i dërguar
    row = db.get(Email, e.id)
    db.refresh(row)
    assert row.status == EmailStatus.QUEUED and row.attempts == 0  # claim-i s'është i qëndrueshëm


def test_a_database_error_while_reading_send_inputs_propagates_and_undoes_the_claim(
    db,
    world,
    probe,
    monkeypatch,  # noqa: F811
):
    e = mk(db)

    def boom(db_, e_):
        raise OperationalError("SELECT", {}, Exception("db down"))

    monkeypatch.setattr(emails, "_read_send_inputs", boom)
    with SessionLocal() as s:
        with pytest.raises(OperationalError):
            emails.process_one(s, NOW())
        s.rollback()
    assert probe.calls == []
    db.expire_all()
    assert db.get(Email, e.id).status == EmailStatus.QUEUED


def test_final_commit_failure_after_provider_success_leaves_sending_REMAINING_RISK(
    db, world, probe
):  # noqa: F811
    """Dritare e mbetur që patch-i NUK e zgjidh: provider-i e dërgoi, COMMIT#2 dështon → SENDING pa
    provider_message_id dhe pa ngjarje `sent`; s'ka lease/auto-recovery; dërgimi mund të ketë ndodhur.
    Nuk pretendohet exactly-once; rikuperimi është manual."""
    e = mk(db)
    with SessionLocal() as s:
        _fail_nth_commit(s, 2)
        with pytest.raises(OperationalError):
            emails.process_one(s, NOW())
        s.rollback()
    assert len(probe.calls) == 1  # u dërgua
    db.expire_all()
    row = db.get(Email, e.id)
    assert (
        row.status == EmailStatus.SENDING and row.attempts == 1 and row.provider_message_id is None
    )
    sent = [x.to_status for x in db.scalars(select(EmailEvent).where(EmailEvent.email_id == e.id))]
    assert "sent" not in sent
    assert emails.process_one(db, T0 + timedelta(days=365)) is None  # nuk rimerret kurrë vetvetiu


def test_crash_after_commit_1_before_the_provider_still_leaves_sending(db, world, probe):  # noqa: F811
    e = mk(db)
    probe.fn = lambda req: (_ for _ in ()).throw(SystemExit)  # BaseException: vdekje proçesi
    with pytest.raises(SystemExit):
        emails.process_one(db, NOW())
    db.rollback()
    db.expire_all()
    assert db.get(Email, e.id).status == EmailStatus.SENDING and len(probe.calls) == 1


# --- Pool nën stres (PostgreSQL) -------------------------------------------------------------------------


@pytest.mark.skipif(not PG, reason="needs PostgreSQL")
def test_sixteen_workers_waiting_on_the_provider_hold_no_pool_connections(db, world, probe):  # noqa: F811
    n = 16
    for i in range(n):
        mk(db, f"k{i}", f"u{i}@customer.org")
    barrier = threading.Barrier(n, timeout=30)
    out = []

    def slow(req):
        barrier.wait()  # të 16 janë njëkohësisht brenda provider-it ("SMTP i ngadaltë")
        out.append(engine.pool.checkedout())
        barrier.wait()
        return SendResult("m")

    probe.fn = slow
    errors = []

    def worker():
        try:
            with SessionLocal() as s:
                emails.process_one(s, NOW())
        except BaseException as ex:  # noqa: BLE001
            errors.append(repr(ex))

    ts = [threading.Thread(target=worker) for _ in range(n)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert not errors, errors
    assert out and max(out) == 0  # para patch-it: 16 lidhje të zëna njëkohësisht
    db.expire_all()
    assert db.scalar(select(Email).where(Email.status != EmailStatus.SENT).limit(1)) is None


@pytest.mark.skipif(not PG, reason="needs PostgreSQL")
def test_pg_stat_activity_shows_no_idle_in_transaction_during_the_provider(db, world, probe):  # noqa: F811
    from sqlalchemy import text

    mk(db)
    seen = {}

    def send(req):
        with engine.connect() as other:
            seen["states"] = [
                r[0]
                for r in other.execute(
                    text(
                        "select state from pg_stat_activity where datname = current_database()"
                        " and pid <> pg_backend_pid()"
                    )
                )
            ]
        return SendResult("m")

    probe.fn = send
    emails.process_one(db, NOW())
    assert "idle in transaction" not in seen["states"]
    _ = os
