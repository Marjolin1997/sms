"""M2-b: teste të kufirit dhe të adapterit `PostgresDispatchQueue` (të reja; baseline-i i karakterizimit
te test_queue_semantics/test_queue_concurrency_pg nuk ndryshon). Adapteri provohet me hooks regjistrues,
pra pa asnjë njohuri për statuset e SMS."""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, select

from app.core.db import SessionLocal, engine
from app.models.sending import Message, MessageStatus
from app.queue.dispatch import DispatchSpec, Outcome
from app.queue.postgres import PostgresDispatchQueue
from app.services import messages as msgsvc
from tests.test_pipeline import OK, fake, world  # noqa: F401

T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1] / "app" / "queue"


# --- Kufiri: adapteri nuk njeh domain-in ------------------------------------------------------------


def test_queue_package_imports_nothing_from_the_domain():
    banned = ("app.models", "app.services", "app.providers", "app.api", "app.core.scope",
              "app.core.context", "app.core.security")  # fmt: skip
    for f in ROOT.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            mods = []
            if isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            for m in mods:
                assert not m.startswith(banned), f"{f.name} importon {m}"


def test_queue_package_never_commits_or_rolls_back():
    for f in ROOT.glob("*.py"):
        text = f.read_text()
        assert ".commit(" not in text and ".rollback(" not in text, f.name
        for word in ("MessageStatus", "EmailStatus", "wallet", "MessageEvent", "ProviderError"):
            assert word not in text, (f.name, word)


# --- Adapteri me hooks regjistrues ---------------------------------------------------------------------


class Recorder:
    """Hooks që regjistrojnë thirrjet dhe bëjnë vetëm ndryshimin minimal që adapteri s'e njeh."""

    def __init__(self):
        self.calls = []

    def reserved(self, db, item):
        self.calls.append(("reserved", item.id, item.attempts))
        item.status = MessageStatus.SENDING

    def requeued(self, db, item, error):
        self.calls.append(("requeued", item.id, error))
        item.status = MessageStatus.QUEUED

    def sent(self, db, item, ref):
        self.calls.append(("sent", item.id, ref))
        item.status = MessageStatus.SENT

    def failed(self, db, item, reason):
        self.calls.append(("failed", item.id, reason))
        item.status = MessageStatus.FAILED


@pytest.fixture
def qr():
    rec = Recorder()
    spec = DispatchSpec(
        model=Message, pending=Message.status == MessageStatus.QUEUED,
        attempts=Message.attempts, next_attempt_at=Message.next_attempt_at, id=Message.id,
        backoff_s=30, max_attempts=5,
    )  # fmt: skip
    return PostgresDispatchQueue(spec, rec), rec


def mk(db, key="k", to=OK):
    m = msgsvc.submit(db, "c1", key, to, "ACME", text="hello")
    db.commit()
    return m


def test_reserve_calls_the_hook_before_incrementing_and_does_not_commit(qr, db, world):  # noqa: F811
    queue, rec = qr
    m = mk(db)
    got = queue.reserve(db, T0 + timedelta(days=1))
    assert got.id == m.id and got.attempts == 1
    assert rec.calls == [("reserved", m.id, 0)]  # hook-u sheh attempts para increment-it (si sot)
    assert db.in_transaction()  # asnjë commit i fshehur
    db.rollback()
    assert db.get(Message, m.id).attempts == 0


def test_reserve_returns_none_when_nothing_is_due_and_calls_no_hook(qr, db, world):  # noqa: F811
    queue, rec = qr
    m = mk(db)
    m.next_attempt_at = T0 + timedelta(seconds=50)
    db.commit()
    assert queue.reserve(db, T0) is None and rec.calls == []


def test_retry_outcomes_and_delays(qr, db, world):  # noqa: F811
    queue, rec = qr
    m = mk(db)
    delays = []
    for attempt in range(1, 5):
        m.attempts = attempt
        assert queue.retry(db, m, error="e", temporary=True, now=T0) is Outcome.RETRIED
        delays.append((m.next_attempt_at.replace(tzinfo=UTC) - T0).total_seconds())
    assert delays == [30, 60, 120, 240]
    m.attempts = 5
    assert queue.retry(db, m, error="last", temporary=True, now=T0) is Outcome.EXHAUSTED
    assert rec.calls[-1] == ("failed", m.id, "last")  # EXHAUSTED → hooks.failed, jo requeued
    m.attempts = 1
    assert queue.retry(db, m, error="perm", temporary=False, now=T0) is Outcome.FAILED
    assert rec.calls[-1] == ("failed", m.id, "perm")
    assert [c[0] for c in rec.calls].count("requeued") == 4


def test_acknowledge_and_fail_delegate_to_hooks(qr, db, world):  # noqa: F811
    queue, rec = qr
    m = mk(db)
    queue.acknowledge(db, m, "prov-1")
    queue.fail(db, m, "why")
    assert rec.calls == [("sent", m.id, "prov-1"), ("failed", m.id, "why")]


def test_cancel_if_pending_only_touches_pending_rows(qr, db, world):  # noqa: F811
    queue, rec = qr
    m = mk(db)
    assert queue.cancel_if_pending(db, 999_999, reason="x") is False
    assert queue.cancel_if_pending(db, m.id, reason="campaign_cancelled") is True
    assert rec.calls == [("failed", m.id, "campaign_cancelled")]
    m.status = MessageStatus.SENDING
    assert queue.cancel_if_pending(db, m.id, reason="x") is False  # SENDING nuk preket
    assert len(rec.calls) == 1


def test_publish_flushes_without_committing_and_honours_not_before(qr, db, world):  # noqa: F811
    queue, _ = qr
    m = mk(db)
    other = Message(**{c.key: getattr(m, c.key) for c in Message.__table__.columns
                       if c.key not in ("id", "idempotency_key", "public_id")},
                    idempotency_key="pub", public_id="pub-uuid")  # fmt: skip
    queue.publish(db, other, not_before=T0)
    assert other.id is not None and db.in_transaction()
    db.rollback()
    assert db.scalar(select(Message).where(Message.idempotency_key == "pub")) is None


# --- Sjellja e SMS përmes adapterit: e njëjta me atë të para M2-b -------------------------------------


def test_sms_service_still_exposes_the_same_public_functions(db, world):  # noqa: F811
    m = mk(db)
    got = msgsvc.claim_next(db)
    assert got.id == m.id and got.status == MessageStatus.SENDING and got.attempts == 1
    db.commit()
    assert msgsvc.cancel_if_queued(db, m.id) is False


def test_sql_statement_count_of_the_hot_path_is_unchanged(db, world):  # noqa: F811
    """Para M2-b (matur mbi kodin e mëparshëm, PostgreSQL): submit+commit=21 SQL, process_one=8 SQL."""
    counts = {"n": 0}

    def count(*a):
        counts["n"] += 1

    with SessionLocal() as s:
        msgsvc.submit(s, "c1", "w1", OK, "ACME", text="hello")
        s.commit()
        msgsvc.submit(s, "c1", "w2", OK, "ACME", text="hello")
        s.commit()
    event.listen(engine, "before_cursor_execute", count)
    try:
        with SessionLocal() as s:
            msgsvc.submit(s, "c1", "w3", OK, "ACME", text="hello")
            s.commit()
        submit_n, counts["n"] = counts["n"], 0
        with SessionLocal() as s:
            msgsvc.process_one(s)
        process_n = counts["n"]
    finally:
        event.remove(engine, "before_cursor_execute", count)
    if db.get_bind().dialect.name == "postgresql":
        # M9-a: process_one 8 → 10 SQL (UPDATE marker `dispatch_started_at` + SELECT FOR UPDATE i claim-it)
        assert (submit_n, process_n) == (21, 10)
    else:  # SQLite: numra të tjerë (pa SELECT FOR UPDATE / savepoint si PG), por të ngurtë
        assert submit_n > 0 and process_n > 0


# --- M2-c: email përmes të njëjtit adapter ---------------------------------------------------------------

from app.models.email import DomainStatus, Email, EmailDomain, EmailStatus  # noqa: E402
from app.services import emails  # noqa: E402
from tests.test_email import FROM, fake_dns, fake_email_provider, verified  # noqa: E402, F401


def test_email_uses_the_same_adapter_class_as_sms():
    assert type(emails.queue) is type(msgsvc.queue) is PostgresDispatchQueue
    spec = emails.queue.spec
    assert (spec.model, spec.backoff_s, spec.max_attempts) == (Email, 30, 5)
    assert spec.pending.compare(Email.status == EmailStatus.QUEUED)


def test_email_hooks_are_the_only_place_that_knows_email_statuses():
    text = (Path(__file__).resolve().parents[1] / "app" / "queue" / "postgres.py").read_text()
    assert "Email" not in text and "SMTP" not in text and "DKIM" not in text


def test_email_domain_unverified_after_publish_is_a_permanent_failure(db, world, verified):  # noqa: F811
    e = emails.submit(db, "c1", "k", FROM, "ana@customer.org", subject="s", text="t")
    db.commit()
    db.execute(EmailDomain.__table__.update().values(status=DomainStatus.PENDING))
    db.commit()
    got = emails.process_one(db, T0 + timedelta(days=1))
    assert got.id == e.id and got.status == EmailStatus.FAILED and got.attempts == 1
    assert got.error_code == "domain_unverified"
    assert emails.process_one(db, T0 + timedelta(days=30)) is None


def test_email_sql_statement_counts_of_the_hot_path_are_unchanged(db, world, verified):  # noqa: F811
    """Para M2-c (kodi i mëparshëm, PostgreSQL): submit 10, process_one 9, retry 9, cancel 6 SQL."""
    n = {"n": 0}

    def count(*a):
        n["n"] += 1

    def measure(fn):
        n["n"] = 0
        event.listen(engine, "before_cursor_execute", count)
        try:
            with SessionLocal() as s:
                out = fn(s)
                s.commit()
        finally:
            event.remove(engine, "before_cursor_execute", count)
        return n["n"], out

    def sub(s, key, to="ana@customer.org"):
        return emails.submit(s, "c1", key, FROM, to, subject="Hello", text="Body")

    with SessionLocal() as s:
        sub(s, "w0")
        sub(s, "w1")
        s.commit()
    submit_n, _ = measure(lambda s: sub(s, "w2"))
    process_n, _ = measure(lambda s: emails.process_one(s))
    with SessionLocal() as s:
        sub(s, "t", "temp@customer.org")
        s.commit()
    retry_n, _ = measure(lambda s: emails.process_one(s, datetime.now(UTC) + timedelta(seconds=60)))
    with SessionLocal() as s:
        eid = sub(s, "c").id
        s.commit()
    cancel_n, ok = measure(lambda s: emails.cancel_if_queued(s, eid))
    assert ok is True
    if db.get_bind().dialect.name == "postgresql":
        assert (submit_n, process_n, retry_n, cancel_n) == (
            10,
            12,
            11,
            6,
        )  # M9-a: +2 SQL në process_one; M9-g2: +1 INSERT (prova e billable) vetëm kur dërgimi arrin SENT (jo te retry/cancel/submit)
    else:
        assert min(submit_n, process_n, retry_n, cancel_n) > 0
