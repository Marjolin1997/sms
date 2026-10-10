# ruff: noqa: F811
"""M9-a — S1/E1: rezultat i panjohur i provider-it. UNKNOWN mban hold-in; asnjë ridërgim/release/capture
automatik; zgjidhje vetëm nga DLR autoritativ ose stafi (audit atomik). SMS + email."""

import ast
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError

import app.providers as providers
from app.core.db import SessionLocal, engine
from app.models.admin import AuditLog
from app.models.email import Email, EmailStatus
from app.models.sending import Message, MessageEvent, MessageStatus
from app.models.wallet import Hold, HoldStatus, LedgerEntry
from app.providers.base import ProviderError, SendResult, is_idempotent
from app.providers.http import HttpProvider
from app.providers.twilio import TwilioProvider
from app.services import emails
from app.services import messages as svc
from app.services import wallet as wallets
from app.services.wallet import Conflict
from tests.test_email import FROM, fake_dns, verified  # noqa: F401
from tests.test_pipeline import OK, fake, send, world  # noqa: F401

PG = engine.dialect.name == "postgresql"
APP = Path(__file__).resolve().parents[1] / "app"
LEASE = timedelta(minutes=10)


def later(hours=1):
    return datetime.now(UTC) + timedelta(hours=hours)


class Stub:
    """Provider i kontrollueshëm; `idempotent_by_reference` default FALSE si çdo adapter real."""

    name = "fake"

    def __init__(self, fn=None, idempotent=False):
        self.calls, self.fn = [], fn
        self.idempotent_by_reference = idempotent

    def send(self, req):
        self.calls.append(req)
        return self.fn(req) if self.fn else SendResult(f"stub-{len(self.calls)}")


@pytest.fixture
def stub(fake):
    s = Stub()
    providers._registry["fake"] = s
    return s


def hold_of(db, m):
    db.expire_all()
    return db.get(Hold, m.hold_id)


def bal(db, m):
    return wallets.balances(db, m.wallet_id)


def ledger_n(db):
    return db.scalar(select(func.count()).select_from(LedgerEntry))


def audits(db, action):
    return list(db.scalars(select(AuditLog).where(AuditLog.action == action)))


def to_unknown(db, stub, key="k1", exc=None):
    """Mesazh SMS që përfundon UNKNOWN përmes një gabimi të paqartë (provider jo-idempotent)."""

    def boom(req):
        raise exc or ProviderError("network:ReadTimeout", temporary=True, ambiguous=True)

    stub.fn = boom
    m = send(db, key)
    svc.process_one(db)
    db.refresh(m)
    assert m.status == MessageStatus.UNKNOWN
    return m


# --- skema/capability ---------------------------------------------------------------------------


def test_provider_capability_defaults_false_and_only_proven_adapters_are_true():
    assert is_idempotent(object()) is False and is_idempotent(Stub()) is False
    assert providers.FakeProvider.idempotent_by_reference is True  # provuar nga kodi/testet
    assert HttpProvider.idempotent_by_reference is False  # pa provë kontrate të vendorit
    assert TwilioProvider.idempotent_by_reference is False  # Twilio s'ka çelës idempotence
    from app.providers.email import FakeEmailProvider, SmtpEmailProvider

    assert FakeEmailProvider.idempotent_by_reference is True
    assert SmtpEmailProvider.idempotent_by_reference is False
    assert ProviderError("x", True).ambiguous is False  # default i sigurt për adapterët e vjetër


def test_unknown_is_not_terminal_and_only_exits_via_delivered_or_failed():
    from app.models.sending import TERMINAL, TRANSITIONS

    assert MessageStatus.UNKNOWN not in TERMINAL
    assert TRANSITIONS[MessageStatus.UNKNOWN] == {MessageStatus.DELIVERED, MessageStatus.FAILED}
    assert MessageStatus.QUEUED not in TRANSITIONS[MessageStatus.UNKNOWN]  # kurrë riradhitje
    assert MessageStatus.UNKNOWN in TRANSITIONS[MessageStatus.SENDING]


# --- klasifikimi i adapterëve -------------------------------------------------------------------


def _http(handler):
    return HttpProvider("http", "https://p.example/send", "k",
                        client=httpx.Client(transport=httpx.MockTransport(handler)))  # fmt: skip


REQ = providers.SendRequest("ref-1", "ACME", "355691230003", "hi", "gsm7", 1)


@pytest.mark.parametrize(
    "exc,ambiguous",
    [(httpx.ConnectError("x"), False), (httpx.ConnectTimeout("x"), False),
     (httpx.ReadTimeout("x"), True), (httpx.RemoteProtocolError("x"), True)],
)  # fmt: skip
def test_http_provider_network_errors_distinguish_before_and_after_connect(exc, ambiguous):
    def handler(request):
        raise exc

    with pytest.raises(ProviderError) as e:
        _http(handler).send(REQ)
    assert (e.value.temporary, e.value.ambiguous) == (True, ambiguous)


@pytest.mark.parametrize(
    "status,temporary,ambiguous",
    [
        (429, True, False),
        (408, True, False),
        (503, True, True),
        (500, True, True),
        (400, False, False),
    ],
)
def test_http_provider_status_classification(status, temporary, ambiguous):
    with pytest.raises(ProviderError) as e:
        _http(lambda r: httpx.Response(status)).send(REQ)
    assert (e.value.temporary, e.value.ambiguous) == (temporary, ambiguous)


def test_http_provider_unreadable_2xx_is_ambiguous_not_a_clean_retry():
    with pytest.raises(ProviderError) as e:
        _http(lambda r: httpx.Response(200, text="not json")).send(REQ)
    assert e.value.code == "bad_response" and e.value.ambiguous is True


def test_twilio_outcome_unknown_cases_are_ambiguous():
    def tw(handler):
        return TwilioProvider("AC1", "tok", "https://x/cb",
                              client=httpx.Client(transport=httpx.MockTransport(handler)))  # fmt: skip

    def readtimeout(request):
        raise httpx.ReadTimeout("x")

    for p in (tw(readtimeout), tw(lambda r: httpx.Response(500)),
              tw(lambda r: httpx.Response(201, json={"status": "queued"}))):  # fmt: skip
        with pytest.raises(ProviderError) as e:
            p.send(REQ)
        assert e.value.code == "twilio_outcome_unknown" and e.value.ambiguous is True

    def connect(request):
        raise httpx.ConnectError("x")

    with pytest.raises(ProviderError) as e:
        tw(connect).send(REQ)
    assert e.value.ambiguous is False and e.value.temporary is True


# --- SMS: rrugët e dërgimit ---------------------------------------------------------------------


def test_ambiguous_outcome_on_a_non_idempotent_provider_becomes_unknown_and_keeps_the_hold(
    db, world, stub
):
    w, _ = world
    m = to_unknown(db, stub)
    assert (m.attempts, m.error_code) == (1, "network:ReadTimeout")
    h = hold_of(db, m)
    assert h.status == HoldStatus.ACTIVE and h.captured_amount == 0
    assert bal(db, m) == (D("9.95"), D("0.05"))  # asnjë lëvizje
    assert svc.process_one(db) is None and len(stub.calls) == 1  # pa ridërgim
    assert svc.expire_stale(db, timedelta(hours=72), later(24 * 365)) == 0  # pa auto-release


def test_definite_failures_keep_the_old_behaviour_retry_and_fail_release(db, world, stub):
    m = send(db, "a")
    stub.fn = lambda req: (_ for _ in ()).throw(ProviderError("http_429", temporary=True))
    svc.process_one(db)
    db.refresh(m)
    assert m.status == MessageStatus.QUEUED and m.error_code == "http_429"
    stub.fn = lambda req: (_ for _ in ()).throw(ProviderError("http_400", temporary=False))
    svc.process_one(db, later(1))
    db.refresh(m)
    assert m.status == MessageStatus.FAILED
    assert hold_of(db, m).status == HoldStatus.RELEASED and bal(db, m) == (D("10"), D("0"))


def test_idempotent_provider_retries_ambiguity_with_the_exact_same_reference(db, world, stub):
    stub.idempotent_by_reference = True
    seen = {"n": 0}

    def fn(req):
        seen["n"] += 1
        if seen["n"] == 1:
            raise ProviderError("network:ReadTimeout", temporary=True, ambiguous=True)
        return SendResult("ok-1")

    stub.fn = fn
    m = send(db)
    svc.process_one(db)
    db.refresh(m)
    assert m.status == MessageStatus.QUEUED  # retry i lejuar: provider idempotent
    svc.process_one(db, later(1))
    db.refresh(m)
    assert m.status == MessageStatus.SENT and m.provider_message_id == "ok-1"
    assert [c.reference for c in stub.calls] == [m.public_id, m.public_id]


def test_idempotent_provider_exhausting_ambiguous_retries_ends_unknown_not_failed(db, world, stub):
    stub.idempotent_by_reference = True
    stub.fn = lambda req: (_ for _ in ()).throw(
        ProviderError("network:ReadTimeout", temporary=True, ambiguous=True)
    )
    m = send(db)
    for i in range(svc.MAX_ATTEMPTS):
        svc.process_one(db, later(i + 1))
    db.refresh(m)
    assert m.status == MessageStatus.UNKNOWN and m.attempts == svc.MAX_ATTEMPTS
    assert hold_of(db, m).status == HoldStatus.ACTIVE


def test_unexpected_exception_after_invocation_is_unknown_never_a_blind_retry(db, world, stub):
    m = to_unknown(db, stub, exc=RuntimeError("connection reset after send"))
    assert m.error_code == "provider_exception" and len(stub.calls) == 1
    assert svc.process_one(db) is None and len(stub.calls) == 1


def test_unexpected_exception_before_invocation_is_a_safe_retry(db, world, stub, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("cannot build request")

    monkeypatch.setattr(svc, "SendRequest", boom)
    m = send(db)
    svc.process_one(db)
    db.refresh(m)
    assert m.status == MessageStatus.QUEUED and m.error_code == "provider_exception"
    assert stub.calls == [] and m.dispatch_started_at is None  # provider-i s'u thirr kurrë


def test_twilio_ambiguity_goes_unknown_with_the_hold_retained(db, world):
    def readtimeout(request):
        raise httpx.ReadTimeout("x")

    providers._registry["fake"] = TwilioProvider(
        "AC1",
        "tok",
        "https://x/cb",
        client=httpx.Client(transport=httpx.MockTransport(readtimeout)),
    )
    m = send(db)
    svc.process_one(db)
    db.refresh(m)
    assert m.status == MessageStatus.UNKNOWN and m.error_code == "twilio_outcome_unknown"
    assert hold_of(db, m).status == HoldStatus.ACTIVE and bal(db, m) == (D("9.95"), D("0.05"))


# --- SMS: dritaret e crash-it + sweeper --------------------------------------------------------------


def test_crash_before_the_provider_marker_is_requeued_not_unknown(db, world, stub):
    """W1/W2: claim i commit-uar, marker NULL ⇒ provider-i definitivisht s'u thirr ⇒ riradhitje e sigurt."""
    m = send(db)
    svc.claim_next(db)
    db.commit()  # COMMIT#1 pa COMMIT#1b: vdekja këtu
    db.refresh(m)
    assert m.status == MessageStatus.SENDING and m.dispatch_started_at is None
    assert svc.recover_stuck(db, LEASE, datetime.now(UTC)).requeued == 0  # i freskët: i paprekur
    rep = svc.recover_stuck(db, LEASE, later())
    db.commit()
    db.refresh(m)
    assert (rep.requeued, rep.unknown) == (1, 0) and m.status == MessageStatus.QUEUED
    assert m.error_code == "recovered:not_dispatched"
    svc.process_one(db, later(2))
    db.refresh(m)
    assert m.status == MessageStatus.SENT and len(stub.calls) == 1


def test_crash_right_after_the_provider_call_begins_is_unknown(db, world, stub):
    """W3: marker i commit-uar, provider-i thirret, procesi vdes ⇒ UNKNOWN (jo-idempotent)."""
    stub.fn = lambda req: (_ for _ in ()).throw(SystemExit)
    m = send(db)
    with pytest.raises(SystemExit):
        svc.process_one(db)
    db.rollback()
    db.expire_all()
    m = db.get(Message, m.id)
    assert m.status == MessageStatus.SENDING and m.dispatch_started_at is not None
    rep = svc.recover_stuck(db, LEASE, later())
    db.commit()
    db.refresh(m)
    assert (rep.unknown, rep.requeued) == (1, 0) and m.status == MessageStatus.UNKNOWN
    assert m.error_code == "stuck_sending" and len(stub.calls) == 1
    assert hold_of(db, m).status == HoldStatus.ACTIVE


def _fail_nth_commit(session, n):
    count = {"n": 0}

    def before_commit(s):
        count["n"] += 1
        if count["n"] == n:
            raise OperationalError("COMMIT", {}, Exception("simulated commit failure"))

    event.listen(session, "before_commit", before_commit)


def test_provider_accepted_then_finalize_crash_never_double_sends(db, world, stub):
    """W4: provider-i pranoi, COMMIT#2 dështon ⇒ SENDING pa id ⇒ sweeper ⇒ UNKNOWN; asnjë ridërgim."""
    m = send(db)
    with SessionLocal() as s:
        _fail_nth_commit(s, 3)  # COMMIT#1, #1b, #2
        with pytest.raises(OperationalError):
            svc.process_one(s)
        s.rollback()
    db.expire_all()
    m = db.get(Message, m.id)
    assert m.status == MessageStatus.SENDING and m.provider_message_id is None
    svc.recover_stuck(db, LEASE, later())
    db.commit()
    db.refresh(m)
    assert m.status == MessageStatus.UNKNOWN
    for i in range(3):  # asnjë punonjës s'e merr më
        assert svc.process_one(db, later(2 + i)) is None
    assert len(stub.calls) == 1


def test_idempotent_provider_after_the_same_crash_is_requeued_and_dedupes_by_reference(
    db, world, fake
):
    m = send(db)
    with SessionLocal() as s:
        _fail_nth_commit(s, 3)
        with pytest.raises(OperationalError):
            svc.process_one(s)
        s.rollback()
    rep = svc.recover_stuck(db, LEASE, later())
    db.commit()
    assert (rep.requeued, rep.unknown) == (1, 0)
    svc.process_one(db, later(2))
    db.refresh(m)
    assert m.status == MessageStatus.SENT and m.provider_message_id == "fake-1"
    assert [c.reference for c in fake.calls] == [m.public_id, m.public_id]
    assert len(fake.accepted) == 1  # dedup nga provider-i sipas reference-s


def test_sweeper_leaves_recent_sending_alone_is_idempotent_and_never_releases(db, world, stub):
    stub.fn = lambda req: (_ for _ in ()).throw(SystemExit)
    m = send(db)
    with pytest.raises(SystemExit):
        svc.process_one(db)
    db.rollback()
    assert svc.recover_stuck(db, LEASE, datetime.now(UTC)).unknown == 0  # brenda lease-it
    first = svc.recover_stuck(db, LEASE, later())
    db.commit()
    again = svc.recover_stuck(db, LEASE, later(2))
    db.commit()
    assert (first.unknown, again.unknown) == (1, 0)
    n_events = db.scalar(select(func.count()).select_from(MessageEvent).where(
        MessageEvent.message_id == m.id, MessageEvent.to_status == "unknown"))  # fmt: skip
    assert n_events == 1
    for k in range(3):
        svc.expire_stale(db, timedelta(hours=1), later(24 * 365 + k))
        svc.recover_stuck(db, LEASE, later(24 * 365 + k))
        db.commit()
    db.expire_all()
    assert db.get(Message, m.id).status == MessageStatus.UNKNOWN
    assert hold_of(db, m).status == HoldStatus.ACTIVE and bal(db, m) == (D("9.95"), D("0.05"))


def test_worker_that_lost_its_claim_to_the_sweeper_never_overrides_unknown(db, world, stub):
    """Sweeper vs finalizim i worker-it: provider-i kthen sukses pasi sweeper-i e bëri UNKNOWN."""

    def slow(req):  # gjatë thirrjes, një sweeper tjetër e sheh SENDING-un si të ngecur
        with SessionLocal() as other:
            svc.recover_stuck(other, timedelta(seconds=0), later())
            other.commit()
        return SendResult("late-1")

    stub.fn = slow
    m = send(db)
    svc.process_one(db)
    db.expire_all()
    m = db.get(Message, m.id)
    assert m.status == MessageStatus.UNKNOWN  # finalizimi s'e mbishkroi
    assert m.provider_message_id == "late-1"  # id u ruajt që DLR ta mbyllë vetë
    assert hold_of(db, m).status == HoldStatus.ACTIVE
    svc.apply_dlr(db, "fake", "late-1", delivered=True)
    db.commit()
    db.refresh(m)
    assert m.status == MessageStatus.DELIVERED and bal(db, m) == (D("9.95"), D("0"))


def test_requeue_attempt_is_not_finalized_by_a_stale_worker(db, world, fake):
    """Sweeper e riradhit (provider idempotent) dhe tjetër worker e kap; worker-i i vjetër s'ka të drejtë."""
    from app.services import dispatch_outcome as outcome

    m = send(db)
    svc.claim_next(db)
    db.commit()
    first_claim = m.attempts
    svc.recover_stuck(db, LEASE, later())  # A: s'u thirr ⇒ QUEUED
    db.commit()
    svc.claim_next(db, later(2))
    db.commit()
    assert not outcome.lock_claim(db, m, first_claim, MessageStatus.SENDING)  # attempts ndryshoi
    assert outcome.lock_claim(db, m, m.attempts, MessageStatus.SENDING)


def test_not_dispatched_exhaustion_fails_with_release_for_non_idempotent_providers(db, world, stub):
    m = send(db)
    for i in range(svc.MAX_ATTEMPTS):
        svc.claim_next(db, later(10 * (i + 1)))
        db.commit()  # claim pa marker (s'u thirr kurrë)
        svc.recover_stuck(db, LEASE, later(10 * (i + 1) + 1))
        db.commit()
    db.refresh(m)
    assert m.status == MessageStatus.FAILED and m.error_code == "recovery_exhausted"
    assert hold_of(db, m).status == HoldStatus.RELEASED and stub.calls == []
    assert bal(db, m) == (D("10"), D("0"))


# --- DLR pas UNKNOWN ---------------------------------------------------------------------------------


def test_late_delivered_dlr_by_provider_id_captures_once_with_a_system_audit(db, world, stub):
    m = to_unknown(db, stub)
    svc.attach_provider_message_id(db, m.public_id, "P-1", actor="root", role="superadmin",
                                   reason="found in the provider console")  # fmt: skip
    db.commit()
    n = ledger_n(db)
    svc.apply_dlr(db, "fake", "P-1", delivered=True)
    db.commit()
    db.refresh(m)
    assert m.status == MessageStatus.DELIVERED and bal(db, m) == (D("9.95"), D("0"))
    assert hold_of(db, m).status == HoldStatus.CAPTURED and ledger_n(db) == n + 1
    (a,) = audits(db, "message.unknown_auto_resolve")
    assert a.actor == "system:dlr_reconciliation" and a.role == "system"
    assert '"source": "dlr"' in a.detail and '"outcome": "billable_delivered"' in a.detail


def test_late_dlr_by_reference_binds_the_id_and_resolves_an_unknown_without_a_stored_id(
    db, world, stub
):
    m = to_unknown(db, stub)
    assert m.provider_message_id is None
    svc.apply_dlr(db, "fake", "P-9", delivered=True, reference=m.public_id)
    db.commit()
    db.refresh(m)
    assert m.status == MessageStatus.DELIVERED and m.provider_message_id == "P-9"
    # reference s'zgjidh mesazhe që s'janë UNKNOWN, as të një provider-i tjetër
    other = send(db, "k2")
    with pytest.raises(svc.NotFound):
        svc.apply_dlr(db, "fake", "P-10", delivered=True, reference=other.public_id)


def test_late_failure_dlr_releases_once(db, world, stub):
    m = to_unknown(db, stub)
    svc.apply_dlr(db, "fake", "P-2", delivered=False, code="absent", reference=m.public_id)
    db.commit()
    db.refresh(m)
    assert m.status == MessageStatus.FAILED and m.error_code == "absent"
    assert hold_of(db, m).status == HoldStatus.RELEASED and bal(db, m) == (D("10"), D("0"))
    assert audits(db, "message.unknown_auto_resolve")


def test_duplicate_and_contradicting_dlrs_after_unknown_move_no_money_twice(db, world, stub):
    m = to_unknown(db, stub)
    svc.apply_dlr(db, "fake", "P-3", delivered=True, reference=m.public_id)
    db.commit()
    n = ledger_n(db)
    svc.apply_dlr(db, "fake", "P-3", delivered=True)  # dublikat: no-op
    with pytest.raises(Conflict):
        svc.apply_dlr(db, "fake", "P-3", delivered=False)  # kontradiktor
    db.commit()
    assert ledger_n(db) == n and bal(db, m) == (D("9.95"), D("0"))
    assert len(audits(db, "message.unknown_auto_resolve")) == 1


def test_dlr_id_conflicting_with_the_recorded_one_is_refused(db, world, stub):
    m = to_unknown(db, stub)
    svc.attach_provider_message_id(
        db, m.public_id, "P-A", actor="r", role="superadmin", reason="why"
    )
    db.commit()
    with pytest.raises(Conflict):
        svc.apply_dlr(db, "fake", "P-B", delivered=True, reference=m.public_id)
    db.rollback()
    db.refresh(m)
    assert m.status == MessageStatus.UNKNOWN and m.provider_message_id == "P-A"


# --- zgjidhja manuale (SMS) ----------------------------------------------------------------------------


def resolve(db, m, outcome, reason="verified with provider report", **kw):
    r = svc.resolve_unknown(db, m.public_id, outcome, actor="root", role="superadmin",
                            reason=reason, **kw)  # fmt: skip
    db.commit()
    return r


def held_invariant(db, m):
    active = db.scalar(select(func.coalesce(func.sum(Hold.amount), 0)).where(
        Hold.wallet_id == m.wallet_id, Hold.status == HoldStatus.ACTIVE))  # fmt: skip
    assert bal(db, m)[1] == active  # held_after = Σ hold-e aktive
    assert wallets.verify_wallet(db, m.wallet_id)


def test_resolve_billable_captures_exactly_once_and_audits_atomically(db, world, stub):
    m = to_unknown(db, stub)
    resolve(db, m, "billable_delivered", provider_message_id="P-7")
    db.refresh(m)
    assert m.status == MessageStatus.DELIVERED and m.provider_message_id == "P-7"
    assert hold_of(db, m).status == HoldStatus.CAPTURED and bal(db, m) == (D("9.95"), D("0"))
    n = ledger_n(db)
    resolve(db, m, "billable_delivered")  # replay: no-op
    assert ledger_n(db) == n and len(audits(db, "message.unknown_resolve")) == 1
    (a,) = audits(db, "message.unknown_resolve")
    assert (a.actor, a.role) == ("root", "superadmin")
    for needle in ('"previous_state": "unknown"', '"outcome": "billable_delivered"',
                   '"hold_id"', '"amount": "0.050000"', '"currency": "EUR"', '"P-7"',
                   "verified with provider report"):  # fmt: skip
        assert needle in a.detail, needle
    held_invariant(db, m)


def test_resolve_non_billable_releases_exactly_once(db, world, stub):
    m = to_unknown(db, stub)
    resolve(db, m, "non_billable_failed")
    db.refresh(m)
    assert m.status == MessageStatus.FAILED and m.error_code == "unknown_resolved_non_billable"
    assert hold_of(db, m).status == HoldStatus.RELEASED and bal(db, m) == (D("10"), D("0"))
    n = ledger_n(db)
    resolve(db, m, "non_billable_failed")
    assert ledger_n(db) == n and len(audits(db, "message.unknown_resolve")) == 1
    held_invariant(db, m)


def test_resolution_cannot_capture_after_release_or_vice_versa(db, world, stub):
    m = to_unknown(db, stub)
    resolve(db, m, "non_billable_failed")
    with pytest.raises(Conflict):
        resolve(db, m, "billable_delivered")
    db.rollback()
    m2 = to_unknown(db, stub, key="k2")
    resolve(db, m2, "billable_delivered")
    with pytest.raises(Conflict):
        resolve(db, m2, "non_billable_failed")
    db.rollback()
    held_invariant(db, m2)


def test_only_unknown_can_be_resolved_and_inputs_are_validated(db, world, stub):
    queued = send(db, "q")
    for bad in ("delivered", "", None, "BILLABLE_DELIVERED"):
        with pytest.raises(svc.InvalidMessage):
            svc.resolve_unknown(
                db, queued.public_id, bad, actor="r", role="superadmin", reason="why"
            )
    with pytest.raises(Conflict):  # QUEUED, jo UNKNOWN
        svc.resolve_unknown(db, queued.public_id, "billable_delivered", actor="r",
                            role="superadmin", reason="why")  # fmt: skip
    with pytest.raises(svc.NotFound):
        svc.resolve_unknown(
            db, "nope", "billable_delivered", actor="r", role="superadmin", reason="why"
        )
    assert queued.status == MessageStatus.QUEUED and bal(db, queued) == (D("9.95"), D("0.05"))


@pytest.mark.parametrize("reason", ["", "   ", None, "x" * 501, "bad\x00reason", 5])
def test_reason_is_mandatory_and_bounded(db, world, stub, reason):
    m = to_unknown(db, stub)
    with pytest.raises(svc.InvalidMessage):
        svc.resolve_unknown(db, m.public_id, "billable_delivered", actor="r", role="superadmin",
                            reason=reason)  # fmt: skip
    db.rollback()
    db.refresh(m)
    assert m.status == MessageStatus.UNKNOWN and hold_of(db, m).status == HoldStatus.ACTIVE


def test_provider_message_id_is_never_silently_overwritten_or_duplicated(db, world, stub):
    a = to_unknown(db, stub, key="a")
    b = to_unknown(db, stub, key="b")
    svc.attach_provider_message_id(
        db, a.public_id, "P-1", actor="r", role="superadmin", reason="why"
    )
    db.commit()
    with pytest.raises(Conflict):  # i ndryshëm nga i ruajturi
        resolve(db, a, "billable_delivered", provider_message_id="P-2")
    db.rollback()
    with pytest.raises(Conflict):  # i zënë nga mesazh tjetër
        svc.attach_provider_message_id(db, b.public_id, "P-1", actor="r", role="superadmin",
                                       reason="why")  # fmt: skip
    db.rollback()
    db.refresh(a)
    assert a.status == MessageStatus.UNKNOWN and a.provider_message_id == "P-1"
    resolve(db, a, "billable_delivered", provider_message_id="P-1")  # i njëjti: lejohet


def test_resolution_and_money_roll_back_together_if_the_audit_fails(db, world, stub, monkeypatch):
    m = to_unknown(db, stub)

    def boom(*a, **k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(svc.audit, "_append", boom)
    with pytest.raises(RuntimeError):
        svc.resolve_unknown(db, m.public_id, "billable_delivered", actor="r", role="superadmin",
                            reason="why")  # fmt: skip
    db.rollback()
    db.expire_all()
    m = db.get(Message, m.id)
    assert m.status == MessageStatus.UNKNOWN and hold_of(db, m).status == HoldStatus.ACTIVE
    assert bal(db, m) == (D("9.95"), D("0.05"))


# --- email (pa para) ---------------------------------------------------------------------------------------


class EStub(Stub):
    pass


@pytest.fixture
def estub(verified):
    s = EStub()
    old = providers._email_registry["fake"]
    providers._email_registry["fake"] = s
    yield s
    providers._email_registry["fake"] = old


def mk_email(db, key="e1", to="ana@customer.org"):
    e = emails.submit(db, "c1", key, FROM, to, subject="Hi", text="Body")
    db.commit()
    return e


def email_unknown(db, estub, key="e1"):
    estub.fn = lambda req: (_ for _ in ()).throw(
        ProviderError("smtp_error:TimeoutError", temporary=True, ambiguous=True)
    )
    e = mk_email(db, key)
    emails.process_one(db)
    db.refresh(e)
    assert e.status == EmailStatus.UNKNOWN
    return e


def test_email_ambiguous_outcome_is_unknown_no_duplicate_and_no_wallet_effect(db, world, estub):
    n = ledger_n(db)
    email_unknown(db, estub)
    assert emails.process_one(db) is None and len(estub.calls) == 1
    assert ledger_n(db) == n  # email s'prek wallet
    assert emails.recover_stuck(db, LEASE, later()).unknown == 0


def test_email_idempotent_provider_retries_with_the_same_reference(db, world, estub):
    estub.idempotent_by_reference = True
    calls = {"n": 0}

    def fn(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderError("smtp_error:X", temporary=True, ambiguous=True)
        return SendResult(req.message_id)

    estub.fn = fn
    e = mk_email(db)
    emails.process_one(db)
    emails.process_one(db, later())
    db.refresh(e)
    assert e.status == EmailStatus.SENT
    assert [c.reference for c in estub.calls] == [e.public_id, e.public_id]


def test_email_sweeper_classifies_by_phase(db, world, estub):
    a = mk_email(db, "a")
    emails.claim_next(db)
    db.commit()  # claim pa marker
    estub.fn = lambda req: (_ for _ in ()).throw(SystemExit)
    b = mk_email(db, "b", "bob@customer.org")
    with pytest.raises(SystemExit):
        emails.process_one(db, later(0.01))
    db.rollback()
    rep = emails.recover_stuck(db, LEASE, later(5))
    db.commit()
    db.expire_all()
    assert (rep.requeued, rep.unknown) == (1, 1)
    assert db.get(Email, a.id).status == EmailStatus.QUEUED
    assert db.get(Email, b.id).status == EmailStatus.UNKNOWN


def test_email_resolution_is_honest_about_the_available_states_and_idempotent(db, world, estub):
    n = ledger_n(db)
    e = email_unknown(db, estub)
    emails.resolve_unknown(db, e.public_id, "confirmed_sent", actor="root", role="superadmin",
                           reason="found in the provider log", provider_message_id="M-1")  # fmt: skip
    db.commit()
    db.refresh(e)
    assert e.status == EmailStatus.SENT and e.provider_message_id == "M-1"  # jo "delivered"
    emails.resolve_unknown(db, e.public_id, "confirmed_sent", actor="root", role="superadmin",
                           reason="replay")  # fmt: skip
    db.commit()
    assert len(audits(db, "email.unknown_resolve")) == 1
    with pytest.raises(Conflict):
        emails.resolve_unknown(
            db, e.public_id, "not_sent", actor="r", role="superadmin", reason="x"
        )
    db.rollback()
    f = email_unknown(db, estub, "e2")
    emails.resolve_unknown(
        db, f.public_id, "not_sent", actor="root", role="superadmin", reason="no"
    )
    db.commit()
    db.refresh(f)
    assert f.status == EmailStatus.FAILED and f.error_code == "unknown_resolved_not_sent"
    assert ledger_n(db) == n  # asnjë hyrje wallet
    with pytest.raises(emails.InvalidEmail):
        emails.resolve_unknown(db, f.public_id, "x", actor="r", role="superadmin", reason="x")
    with pytest.raises(emails.InvalidEmail):
        emails.resolve_unknown(db, f.public_id, "not_sent", actor="r", role="superadmin", reason="")


def test_email_provider_event_resolves_an_unknown_with_a_system_audit(db, world, estub):
    e = email_unknown(db, estub)
    emails.attach_provider_message_id(
        db, e.public_id, "M-5", actor="r", role="superadmin", reason="id"
    )
    db.commit()
    emails.apply_event(db, "fake", "M-5", "delivered")
    db.commit()
    db.refresh(e)
    assert e.status == EmailStatus.DELIVERED
    (a,) = audits(db, "email.unknown_auto_resolve")
    assert a.role == "system" and "provider_event" in a.detail


# --- kampanjat: të njëjtën rrugë dërgimi --------------------------------------------------------------------


def test_campaign_messages_use_the_same_unknown_path_without_a_bypass(db, world, stub):
    from app.services import campaigns as csvc
    from tests.test_campaigns import NOW as CNOW
    from tests.test_campaigns import audience, campaign, drive, start

    lst, _ = audience(db, 2)
    c = campaign(db, lst)
    start(db, c)
    drive(db)
    stub.fn = lambda req: (_ for _ in ()).throw(
        ProviderError("network:ReadTimeout", temporary=True, ambiguous=True)
    )
    while svc.process_one(db, CNOW):
        pass
    msgs = list(db.scalars(select(Message)))
    assert msgs and {m.status for m in msgs} == {MessageStatus.UNKNOWN}
    assert {hold_of(db, m).status for m in msgs} == {HoldStatus.ACTIVE}
    stats = csvc.stats(db, c)
    assert stats["messages"] == {"sending": len(msgs)} and "unknown" not in str(stats)
    assert len(stub.calls) == len(msgs)  # një thirrje për mesazh, pa ridërgim


def test_provider_send_is_only_invoked_from_the_two_worker_functions():
    """S'ka rrugë anashkalimi: `.send(` mbi provider vetëm te process_one (SMS/email)."""
    hits = []
    for f in APP.rglob("*.py"):
        rel = f.relative_to(APP).as_posix()
        if rel.startswith("providers/"):
            continue
        tree = ast.parse(f.read_text())
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            src = ast.unparse(fn)
            if "provider.send(" in src or "get_provider(" in src or "get_email_provider(" in src:
                hits.append((rel, fn.name))
    assert sorted(set(hits)) == [
        ("services/emails.py", "process_one"),
        ("services/emails.py", "recover_stuck"),
        ("services/messages.py", "process_one"),
        ("services/messages.py", "recover_stuck"),
    ]


# --- dukshmëria: API, klienti, readiness ---------------------------------------------------------------------------


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app())


BOOT = {"X-Admin-Key": "test-key"}


def _key(client, role, owner_ref=None):
    r = client.post("/v1/admin/api-keys", json={"name": f"{role}-k", "role": role,
                                                "owner_ref": owner_ref}, headers=BOOT)  # fmt: skip
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['key']}"}


def test_admin_listing_summary_and_stats_expose_the_unknown_backlog(db, world, stub, client):
    m = to_unknown(db, stub)
    r = client.get("/v1/admin/queue/unknown", headers=BOOT)
    assert r.status_code == 200
    j = r.json()
    row = j["sms"][0]
    assert (row["id"], row["provider"], row["held_amount"], row["currency"]) == (
        m.public_id, "fake", "0.050000", "EUR")  # fmt: skip
    assert row["age_seconds"] >= 0 and row["attempts"] == 1 and row["unknown_since"]
    assert j["summary"]["sms"] == 1 and j["summary"]["held_amount_by_currency"] == {
        "EUR": "0.050000"
    }
    st = client.get("/v1/admin/stats", headers=BOOT).json()
    assert st["unknown_outcome"]["sms"] == 1 and st["messages_by_status"]["unknown"] == 1
    prov = client.get("/v1/admin/providers", headers=BOOT).json()["providers"][0]
    assert prov["unknown_outcome"] == 1


def test_resolve_endpoints_require_the_highest_role_and_are_not_public(db, world, stub, client):
    m = to_unknown(db, stub)
    body = {"outcome": "billable_delivered", "reason": "provider portal says delivered"}
    url = f"/v1/admin/queue/sms/{m.public_id}/resolve"
    for role in ("finance", "support", "pricing", "approver"):
        assert client.post(url, json=body, headers=_key(client, role)).status_code == 403, role
    assert client.post(url, json=body, headers=_key(client, "client", "c1")).status_code in (
        401,
        403,
    )
    assert client.post(url, json=body).status_code == 401
    assert client.get("/v1/admin/queue/unknown", headers=_key(client, "support")).status_code == 200
    assert client.post(url, json={**body, "reason": ""}, headers=BOOT).status_code == 422
    r = client.post(url, json=body, headers=BOOT)
    assert r.status_code == 200 and r.json()["status"] == "delivered"
    db.expire_all()
    assert hold_of(db, m).status == HoldStatus.CAPTURED
    assert client.post(url, json=body, headers=BOOT).status_code == 200  # replay
    assert client.post(url, json={**body, "outcome": "non_billable_failed"},
                       headers=BOOT).status_code == 409  # fmt: skip
    assert client.post(f"/v1/admin/queue/sms/{m.public_id}x/resolve", json=body,
                       headers=BOOT).status_code == 404  # fmt: skip


def test_email_resolve_endpoint_and_provider_id_endpoints(db, world, estub, client):
    e = email_unknown(db, estub)
    r = client.post(f"/v1/admin/queue/email/{e.public_id}/provider-id",
                    json={"provider_message_id": "M-7", "reason": "from the log"}, headers=BOOT)  # fmt: skip
    assert r.status_code == 200 and r.json()["provider_message_id"] == "M-7"
    r = client.post(f"/v1/admin/queue/email/{e.public_id}/resolve",
                    json={"outcome": "not_sent", "reason": "never left the relay"}, headers=BOOT)  # fmt: skip
    assert r.status_code == 200 and r.json()["status"] == "failed"
    assert client.post(f"/v1/admin/queue/email/{e.public_id}/resolve",
                       json={"outcome": "bogus", "reason": "x"}, headers=BOOT).status_code == 422  # fmt: skip


def test_customers_never_see_unknown(db, world, stub, client):
    m = to_unknown(db, stub)
    h = _key(client, "client", "c1")
    r = client.get(f"/v1/messages/{m.public_id}", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "sending"
    ev = client.get(f"/v1/messages/{m.public_id}/events", headers=h).json()
    assert "unknown" not in str(ev) and "stuck" not in str(ev) and "ReadTimeout" not in str(ev)
    assert svc.public_status(m) == "sending"


def test_queue_readiness_reports_stale_unknown_and_never_mutates(db, world, stub):
    from scripts import queue_readiness as qr

    assert qr.exit_code(qr.evaluate(db)) == 0 and {c.level for c in qr.evaluate(db)} == {"PASS"}
    m = to_unknown(db, stub)
    now = datetime.now(UTC)
    c = {x.name: x for x in qr.evaluate(db, now=now)}
    assert c["unknown_sms"].level == "PASS" and "1 UNKNOWN" in c["unknown_sms"].reason
    c = {x.name: x for x in qr.evaluate(db, now=now + timedelta(hours=2))}
    assert c["unknown_sms"].level == "WARN" and qr.exit_code(list(c.values())) == 0
    assert qr.exit_code(list(c.values()), strict=True) == 1
    c = {x.name: x for x in qr.evaluate(db, now=now + timedelta(days=2))}
    assert c["unknown_sms"].level == "FAIL" and qr.exit_code(list(c.values())) == 1
    assert "fake=True" in c["provider_capabilities"].reason
    db.expire_all()
    assert db.get(Message, m.id).status == MessageStatus.UNKNOWN  # vetëm-lexim
    assert hold_of(db, m).status == HoldStatus.ACTIVE


def test_queue_readiness_fails_when_the_sweeper_is_not_running(db, world, stub):
    from scripts import queue_readiness as qr

    stub.fn = lambda req: (_ for _ in ()).throw(SystemExit)
    send(db)
    with pytest.raises(SystemExit):
        svc.process_one(db)
    db.rollback()
    c = {x.name: x for x in qr.evaluate(db, now=datetime.now(UTC) + timedelta(hours=1))}
    assert c["stuck_sending_sms"].level == "FAIL"


# --- PostgreSQL: gara me një rezultat financiar ----------------------------------------------------------------------

pg = pytest.mark.skipif(not PG, reason="needs PostgreSQL")


def run_threads(fns):
    errs, ts = [], []

    def wrap(fn):
        def go():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        return go

    ts = [threading.Thread(target=wrap(f)) for f in fns]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert not [t for t in ts if t.is_alive()], "thread i varur"
    return errs


def movements(db, m):
    db.expire_all()
    return {
        "captures": db.scalar(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.wallet_id == m.wallet_id, LedgerEntry.entry_type == "capture")),
        "releases": db.scalar(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.wallet_id == m.wallet_id, LedgerEntry.entry_type == "release")),
    }  # fmt: skip


@pg
def test_pg_human_resolution_vs_late_dlr_exactly_one_terminal_outcome(db, world, stub):
    m = to_unknown(db, stub)
    barrier, out = threading.Barrier(2, timeout=20), []

    def human():
        with SessionLocal() as s:
            barrier.wait()
            try:
                svc.resolve_unknown(s, m.public_id, "non_billable_failed", actor="r",
                                    role="superadmin", reason="human says not sent")  # fmt: skip
                s.commit()
                out.append("human")
            except Conflict:
                s.rollback()
                out.append("human_conflict")

    def dlr():
        with SessionLocal() as s:
            barrier.wait()
            try:
                svc.apply_dlr(s, "fake", "P-R", delivered=True, reference=m.public_id)
                s.commit()
                out.append("dlr")
            except Conflict:
                s.rollback()
                out.append("dlr_conflict")
            except (
                svc.NotFound
            ):  # njeriu fitoi garën dhe mesazhi s'është më i pranueshëm për DLR: humbës i vlefshëm
                s.rollback()
                out.append("dlr_conflict")

    assert not run_threads([human, dlr])
    mv = movements(db, m)
    assert mv["captures"] + mv["releases"] == 1  # një rezultat financiar
    assert {hold_of(db, m).status} <= {HoldStatus.CAPTURED, HoldStatus.RELEASED}
    held_invariant(db, m)
    assert len([o for o in out if not o.endswith("conflict")]) == 1


@pg
@pytest.mark.parametrize("a,b", [("billable_delivered",) * 2, ("non_billable_failed",) * 2,
                                 ("billable_delivered", "non_billable_failed")])  # fmt: skip
def test_pg_two_human_resolutions_move_money_once(db, world, stub, a, b):
    m = to_unknown(db, stub)
    barrier = threading.Barrier(2, timeout=20)
    res = []

    def go(outcome):
        def run():
            with SessionLocal() as s:
                barrier.wait()
                try:
                    svc.resolve_unknown(s, m.public_id, outcome, actor="r", role="superadmin",
                                        reason="race")  # fmt: skip
                    s.commit()
                    res.append(("ok", outcome))
                except Conflict:
                    s.rollback()
                    res.append(("conflict", outcome))

        return run

    assert not run_threads([go(a), go(b)])
    mv = movements(db, m)
    assert mv["captures"] + mv["releases"] == 1
    assert len(audits(db, "message.unknown_resolve")) == 1
    held_invariant(db, m)
    if a == b:
        assert [r[0] for r in res] == ["ok", "ok"]  # replay idempotent, jo konflikt
    else:
        assert sorted(r[0] for r in res) == ["conflict", "ok"]


@pg
def test_pg_duplicate_sweepers_transition_each_stuck_row_once(db, world, stub):
    stub.fn = lambda req: (_ for _ in ()).throw(SystemExit)
    ms = [send(db, f"k{i}") for i in range(4)]
    for _ in ms:
        with pytest.raises(SystemExit):
            svc.process_one(db)
        db.rollback()
    barrier, reps = threading.Barrier(2, timeout=20), []

    def sweep():
        with SessionLocal() as s:
            barrier.wait()
            reps.append(svc.recover_stuck(s, LEASE, later()))
            s.commit()

    assert not run_threads([sweep, sweep])
    assert sum(r.unknown for r in reps) == 4
    n = db.scalar(
        select(func.count()).select_from(MessageEvent).where(MessageEvent.to_status == "unknown")
    )
    assert n == 4
    db.expire_all()
    assert {db.get(Message, m.id).status for m in ms} == {MessageStatus.UNKNOWN}


@pg
def test_pg_sweeper_vs_dlr_never_double_moves_money(db, world, stub):
    stub.fn = lambda req: (_ for _ in ()).throw(SystemExit)
    m = send(db)
    with pytest.raises(SystemExit):
        svc.process_one(db)
    db.rollback()
    barrier, notes = threading.Barrier(2, timeout=20), []

    def sweep():
        with SessionLocal() as s:
            barrier.wait()
            svc.recover_stuck(s, LEASE, later())
            s.commit()

    def dlr():
        with SessionLocal() as s:
            barrier.wait()
            try:
                svc.apply_dlr(s, "fake", "P-S", delivered=True, reference=m.public_id)
                s.commit()
                notes.append("applied")
            except svc.NotFound:
                s.rollback()
                notes.append("not_yet")  # provider-i riprovon (503)

    assert not run_threads([sweep, dlr])
    if notes == ["not_yet"]:  # riprovë e provider-it pasi sweeper-i e bëri UNKNOWN
        svc.apply_dlr(db, "fake", "P-S", delivered=True, reference=m.public_id)
        db.commit()
    mv = movements(db, m)
    assert mv == {"captures": 1, "releases": 0}
    held_invariant(db, m)


# --- migrimi 0022 -------------------------------------------------------------------------------------------------------------


def test_migration_0022_is_additive_reversible_and_historical_rows_stay_untouched(tmp_path):
    import os
    import subprocess
    import sys

    from sqlalchemy import create_engine, inspect, text

    url = f"sqlite:///{tmp_path / 'm.db'}"
    env = {**os.environ, "SMS_DATABASE_URL": url, "PYTHONPATH": "."}

    def alembic(*args):
        r = subprocess.run([sys.executable, "-m", "alembic", *args], env=env,
                           capture_output=True, text=True)  # fmt: skip
        assert r.returncode == 0, r.stderr

    alembic("upgrade", "0021")
    eng = create_engine(url)
    cols = lambda t: {c["name"] for c in inspect(eng).get_columns(t)}  # noqa: E731
    assert "dispatch_started_at" not in cols("sms_messages")
    alembic("upgrade", "head")
    assert "dispatch_started_at" in cols("sms_messages") and "dispatch_started_at" in cols(
        "sms_emails"
    )
    with eng.connect() as c:  # asnjë rresht s'migrohet në UNKNOWN (kolona e re është thjesht NULL)
        assert (
            c.execute(text("select count(*) from sms_messages where status = 'unknown'")).scalar()
            == 0
        )
    alembic("downgrade", "0021")
    assert "dispatch_started_at" not in cols("sms_messages") and "dispatch_started_at" not in cols(
        "sms_emails"
    )
    alembic("upgrade", "head")
    eng.dispose()
