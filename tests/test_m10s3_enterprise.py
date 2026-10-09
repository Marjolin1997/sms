# ruff: noqa: F811
"""M10-S3 — Enterprise: outbox atomik i kërkesave të sender-ave, dorëzim at-least-once, klasifikim gabimesh, rikuperim, E2E me Central + `cp.sender.v1`, gatishmëri, migrim 0030, hot path."""

import ast
import json
import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.models.enterprise_registry import resolve_id
from app.models.messaging import ApprovalStatus, SenderDecision, SenderId
from app.models.sender_request import (
    SenderRequestImmutableError,
    SenderRequestOutbox,
)
from app.models.sender_sync import SyncedSenderAuthorization
from app.services import control_plane_client as cc
from app.services import messages as msgs
from app.services import sender_ids as sid
from app.services import sender_request_outbox as ob
from app.services import sender_request_readiness as rr
from app.services import sender_sync_poller as sp
from apps.central.models.sender import SenderDecision as CDecision
from apps.central.models.sender import SenderRegistry, SenderRequestOperation
from apps.central.services import enterprises as cent
from apps.central.services import senders as csvc
from apps.central.services import service_auth
from packages.contracts.control_plane.sender import request_v1 as rv
from tests.test_central import (  # noqa: F401
    IS_PG,
    ROOT,
    central_alembic,
    enterprise_alembic,
    make_db,
)
from tests.test_central_auth import auth_secret  # noqa: F401
from tests.test_central_sync_api import keypair
from tests.test_m10s3_central import A, mutate, renv  # noqa: F401
from tests.test_pipeline import OK, fake, world  # noqa: F401

NOW = datetime(2031, 1, 1, tzinfo=UTC)


def outbox(db):
    db.expire_all()
    return list(db.scalars(select(SenderRequestOutbox).order_by(SenderRequestOutbox.id)))


def new_sender(db, value="ACME", owner="c1", country="AL"):
    s = sid.request(db, owner, country, value)
    db.commit()
    return s


class Stub:
    """Klient i falsifikuar: kthen ACK ose ngre gabimin e radhës."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []

    def post_sender_request(self, payload):
        self.calls.append(payload)
        step = self.script.pop(0) if self.script else None
        if isinstance(step, Exception):
            raise step
        return {
            "status": "accepted",
            "operation_id": payload["operation_id"],
            "outcome": "created",
            "registry_ref": str(uuid.uuid4()),
        }


def http_client(handler):
    key = load_pem_private_key(keypair()[0].encode(), password=None)
    return cc.ControlPlaneClient(
        cc.ControlPlaneConfig("http://central", "ent", "k1", key, 5.0),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        scope=cc.SENDER_REPORT_SCOPE,
    )


def ack(request, **over):
    p = json.loads(request.content)
    d = {
        "status": "accepted",
        "operation_id": p["operation_id"],
        "outcome": "created",
        "registry_ref": str(uuid.uuid4()),
    }
    d.update(over)
    return d


# =============================================================================================================
# shkrimi atomik dhe identiteti
# =============================================================================================================


def test_initial_request_creates_exactly_one_outbox_operation_in_the_same_transaction(db):
    s = sid.request(db, "c1", "AL", "ACME")
    db.flush()
    rows = outbox(db)  # ende pa commit: e njëjta transaksion
    assert len(rows) == 1
    r = rows[0]
    assert (r.request_type, r.state, r.attempts, r.sender_id) == ("requested", "pending", 0, s.id)
    assert r.external_ref == rv.external_ref_for(s.id) == f"sms-sender-{s.id}"
    assert r.enterprise_id == s.enterprise_id == resolve_id(db, "c1")
    assert (
        r.schema_version == "sender.request.v1"
        and r.request_hash == rv.SenderRequestV1.parse(r.payload).request_hash()
    )
    p = r.payload
    assert (
        p["country"],
        p["sender_kind"],
        p["display_value"],
        p["operation"],
        p["evidence_ref"],
    ) == ("AL", "alphanumeric", "ACME", "request", None)
    assert (
        "owner_ref" not in p and "norm_value" not in p and p["operation_id"] == str(r.operation_id)
    )
    assert s.status == ApprovalStatus.PENDING  # gjendja lokale e pandryshuar nga transporti
    db.commit()


def begin_tx(db):
    """pysqlite: SAVEPOINT si deklaratë e parë e transaksionit e commit-on në release; një DML bosh e hap transaksionin (rollback i vërtetë)."""
    db.execute(text("UPDATE sms_sender_ids SET reason = reason WHERE 1 = 0"))


def test_rollback_removes_both_the_sender_and_the_outbox_row(db):
    begin_tx(db)
    sid.request(db, "c1", "AL", "ROLLBK")
    db.flush()
    assert len(outbox(db)) == 1
    db.rollback()
    assert db.scalar(select(func.count()).select_from(SenderId)) == 0 and outbox(db) == []


def test_idempotent_rerequest_and_pending_resubmit_attempts_create_no_new_operation(db):
    s = new_sender(db)
    for _ in range(3):
        sid.request(db, "c1", "AL", "acme")  # kërkesë e përsëritur (case-insensitive)
        db.commit()
    assert len(outbox(db)) == 1
    assert s.status == ApprovalStatus.PENDING


def test_resubmit_creates_a_new_logical_operation_with_the_same_external_ref(db):
    s = new_sender(db)
    sid.reject(db, s.id, "staff", "no proof")
    db.commit()
    sid.request(db, "c1", "AL", "ACME")
    db.commit()
    a, b = outbox(db)
    assert (a.request_type, b.request_type) == ("requested", "resubmitted")
    assert a.operation_id != b.operation_id and a.external_ref == b.external_ref
    assert b.payload["operation"] == "resubmit" and b.id > a.id
    # ridërgimi i dytë nga pending s'krijon operacion të tretë
    sid.request(db, "c1", "AL", "ACME")
    db.commit()
    assert len(outbox(db)) == 2
    sid.reject(db, s.id, "staff", "again")
    db.commit()
    sid.request(db, "c1", "AL", "ACME")
    db.commit()
    assert [r.request_type for r in outbox(db)] == ["requested", "resubmitted", "resubmitted"]
    assert len({r.operation_id for r in outbox(db)}) == 3


def test_local_review_actions_do_not_enqueue(db):
    s = new_sender(db)
    sid.approve(db, s.id, "staff")
    sid.revoke(db, s.id, "staff", "abuse")
    db.commit()
    assert len(outbox(db)) == 1


def test_external_ref_does_not_depend_on_the_sender_value_or_owner(db):
    a, b = new_sender(db, "AAAAA"), new_sender(db, "BBBBB", owner="c2")
    refs = [r.external_ref for r in outbox(db)]
    assert refs == [f"sms-sender-{a.id}", f"sms-sender-{b.id}"] and len(set(refs)) == 2


def test_database_enforces_one_request_per_sender_and_the_stable_external_ref(db):
    s = new_sender(db)
    base = outbox(db)[0]

    def clone(**over):
        d = dict(operation_id=uuid.uuid4(), enterprise_id=base.enterprise_id, sender_id=s.id, external_ref=base.external_ref,
                 request_type="requested", schema_version=base.schema_version, payload=base.payload, request_hash=base.request_hash,
                 created_at=NOW, updated_at=NOW, next_attempt_at=NOW)  # fmt: skip
        d.update(over)
        return SenderRequestOutbox(**d)

    for bad in (
        clone(),
        clone(request_type="resubmitted", external_ref="sms-sender-9999"),
        clone(request_type="resubmitted", state="weird"),
        clone(request_type="resubmitted", operation_id=base.operation_id),
    ):
        db.add(bad)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
    db.add(clone(request_type="resubmitted"))
    db.commit()  # shumë ridërgime lejohen
    assert len(outbox(db)) == 2


def test_outbox_content_is_immutable_but_delivery_metadata_is_not(db):
    new_sender(db)
    r = outbox(db)[0]
    r.state, r.attempts, r.last_error_code, r.next_attempt_at = "retry", 1, "transport", NOW
    db.commit()
    for field, val in (
        ("payload", {"x": 1}),
        ("request_hash", "0" * 64),
        ("operation_id", uuid.uuid4()),
        ("external_ref", "sms-sender-999"),
        ("request_type", "resubmitted"),
    ):
        r = outbox(db)[0]
        setattr(r, field, val)
        with pytest.raises(SenderRequestImmutableError):
            db.flush()
        db.rollback()
    db.delete(outbox(db)[0])
    with pytest.raises(SenderRequestImmutableError):
        db.flush()
    db.rollback()


def test_legacy_row_without_enterprise_is_not_queued_and_is_logged(db, monkeypatch, caplog):
    monkeypatch.setattr(settings, "enterprise_dual_write", False)
    with caplog.at_level(logging.WARNING, logger="sms.sender.request"):
        s = sid.request(db, "legacy-owner", "AL", "NOENT")
        db.commit()
    assert s.enterprise_id is None and outbox(db) == []
    assert "not queued" in caplog.text


def test_customer_request_path_has_no_network_and_exactly_one_extra_insert(db, monkeypatch):
    src = ast.parse(Path(sid.__file__).read_text())
    imported = {n.module for n in ast.walk(src) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(src) if isinstance(n, ast.Import) for a in n.names
    }
    assert not {"httpx", "requests", "app.services.control_plane_client"} & imported

    def count(fn):
        seen = []
        cb = lambda *a: seen.append(a[2])  # noqa: E731
        event.listen(engine, "before_cursor_execute", cb)
        try:
            fn()
            db.flush()
        finally:
            event.remove(engine, "before_cursor_execute", cb)
        return seen

    new_sender(db, "WARMUP")  # tenant-i ekziston (pa krijim regjistri në matje)
    begin_tx(db)
    with_ob = count(lambda: sid.request(db, "c1", "AL", "SQLONE"))
    db.rollback()
    monkeypatch.setattr(ob, "enqueue", lambda *a, **k: None)
    begin_tx(db)
    without = count(lambda: sid.request(db, "c1", "AL", "SQLONE"))
    db.rollback()
    inserts = [q for q in with_ob if "sms_sender_request_outbox" in q]
    assert len(inserts) == 1 and inserts[0].lstrip().upper().startswith("INSERT")
    assert len(with_ob) == len(without) + 1

    monkeypatch.undo()
    s = new_sender(db, "SQLTWO")
    sid.reject(db, s.id, "staff", "x")
    db.commit()
    begin_tx(db)
    r_with = count(lambda: sid.request(db, "c1", "AL", "SQLTWO"))
    db.rollback()
    monkeypatch.setattr(ob, "enqueue", lambda *a, **k: None)
    begin_tx(db)
    r_without = count(lambda: sid.request(db, "c1", "AL", "SQLTWO"))
    db.rollback()
    assert len(r_with) == len(r_without) + 1  # ridërgimi: gjithashtu një INSERT


def test_submit_and_process_one_sql_is_unchanged(db, world, fake):
    def count(fn):
        seen = []
        cb = lambda *a: seen.append(a[2])  # noqa: E731
        event.listen(engine, "before_cursor_execute", cb)
        try:
            fn()
        finally:
            event.remove(engine, "before_cursor_execute", cb)
        return len(seen)

    assert count(lambda: msgs.submit(db, "c1", "k-s3", OK, "ACME", text="hello")) == 20
    db.commit()
    assert count(lambda: msgs.process_one(db)) == 10
    for mod in (msgs,):
        assert "sender_request" not in Path(mod.__file__).read_text()


# =============================================================================================================
# dorëzimi: sukses, klasifikim, rikuperim
# =============================================================================================================


def test_successful_delivery_marks_sent_and_never_touches_local_sender_state(db):
    s = new_sender(db)
    before = (
        s.status,
        s.approved_key,
        s.current_decision_id,
        db.scalar(select(func.count()).select_from(SenderDecision)),
    )
    stub = Stub()
    out = ob.deliver(SessionLocal, stub, now=NOW)
    assert out.ok and out.sent == 1 and len(stub.calls) == 1
    r = outbox(db)[0]
    assert (r.state, r.attempts, r.sent_at is not None, r.last_error_code, r.ack_outcome) == (
        "sent",
        1,
        True,
        None,
        "created",
    )
    assert r.leased_until is None and r.ack_registry_ref is not None
    db.refresh(s)
    assert (
        s.status,
        s.approved_key,
        s.current_decision_id,
        db.scalar(select(func.count()).select_from(SenderDecision)),
    ) == before
    assert ob.deliver(SessionLocal, stub, now=NOW).sent == 0  # asgjë për të ridërguar


def test_payload_sent_is_exactly_the_frozen_contract_document(db):
    new_sender(db)
    stub = Stub()
    ob.deliver(SessionLocal, stub, now=NOW)
    assert stub.calls[0] == outbox(db)[0].payload
    assert rv.SenderRequestV1.parse(stub.calls[0]).request_hash() == outbox(db)[0].request_hash


@pytest.mark.parametrize(
    "exc,code",
    [
        (cc.CpTransportError("timeout"), "transport"),
        (cc.CpAuthError("401"), "auth"),
        (cc.CpForbidden("403"), "forbidden"),
        (cc.CpProtocolError("bad"), "protocol"),
    ],
)
def test_retryable_failures_back_off_and_are_retried_with_the_same_operation(db, exc, code):
    new_sender(db)
    stub = Stub(exc)
    out = ob.deliver(SessionLocal, stub, now=NOW)
    assert not out.ok and out.retry == 1 and out.failed == 0
    r = outbox(db)[0]
    assert (r.state, r.attempts, r.last_error_code, r.leased_until) == ("retry", 1, code, None)
    assert r.next_attempt_at.replace(tzinfo=UTC) == NOW + timedelta(seconds=30)
    assert ob.deliver(SessionLocal, stub, now=NOW + timedelta(seconds=10)).sent == 0  # backoff
    stub.script.append(None)
    assert ob.deliver(SessionLocal, stub, now=NOW + timedelta(seconds=31)).sent == 1
    assert len(stub.calls) == 2 and stub.calls[0] == stub.calls[1]  # i njëjti operation_id
    assert outbox(db)[0].state == "sent" and outbox(db)[0].last_error_code is None


def test_backoff_grows_and_is_capped(db):
    new_sender(db)
    stub = Stub(*[cc.CpTransportError("x")] * 12)
    t, gaps = NOW, []
    for _ in range(8):
        ob.deliver(SessionLocal, stub, now=t)
        gap = (outbox(db)[0].next_attempt_at.replace(tzinfo=UTC) - t).total_seconds()
        gaps.append(gap)
        t = t + timedelta(seconds=gap)
    assert gaps[:3] == [30, 60, 120] and max(gaps) == ob.BACKOFF_CAP_S


def test_http_status_classification_through_the_real_client(db):
    def run(status, headers=None, json_body=None, ok=False):
        def handler(request):
            if ok:
                return httpx.Response(201, json=ack(request))
            return httpx.Response(status, json=json_body or {"detail": {"code": "x"}})

        return handler

    cases = [
        (500, "retry", "transport"), (502, "retry", "transport"), (429, "retry", "transport"),
        (401, "retry", "auth"), (403, "retry", "forbidden"),
        (409, "failed", "conflict"), (422, "failed", "invalid"), (413, "failed", "too_large"), (404, "failed", "not_found"),
    ]  # fmt: skip
    for status, state, code in cases:
        d = {"detail": {"code": code if state == "failed" else "x"}}
        n = SenderRequestOutbox
        s = new_sender(db, f"S{status}XY")
        h = run(status, json_body=d)
        ob.deliver(SessionLocal, http_client(h), now=NOW)
        r = db.scalars(select(n).where(n.sender_id == s.id)).one()
        db.refresh(r)
        assert (r.state, r.last_error_code) == (state, code), (status, r.state, r.last_error_code)
    # 403 me kod enterprise_not_authorized ⇒ permanent
    s = new_sender(db, "NOAUTH")
    h = lambda request: httpx.Response(403, json={"detail": {"code": "enterprise_not_authorized"}})  # noqa: E731
    ob.deliver(SessionLocal, http_client(h), now=NOW)
    r = db.scalars(select(SenderRequestOutbox).where(SenderRequestOutbox.sender_id == s.id)).one()
    db.refresh(r)
    assert (r.state, r.last_error_code) == ("failed", "enterprise_not_authorized")


def test_network_errors_and_malformed_or_oversized_responses_are_retryable_protocol_problems(db):
    def boom(request):
        raise httpx.ConnectTimeout("t")

    new_sender(db)
    ob.deliver(SessionLocal, http_client(boom), now=NOW)
    assert (outbox(db)[0].state, outbox(db)[0].last_error_code) == ("retry", "transport")
    bad = [
        lambda r: httpx.Response(200, json={"status": "accepted"}),
        lambda r: httpx.Response(
            200, json={**ack(r), "operation_id": str(uuid.uuid4())}
        ),  # operation_id i gabuar
        lambda r: httpx.Response(200, json={**ack(r), "outcome": "weird"}),
        lambda r: httpx.Response(200, json={**ack(r), "registry_ref": "nope"}),
        lambda r: httpx.Response(200, content=b"not json"),
        lambda r: httpx.Response(200, json={**ack(r), "pad": "x" * 9000}),
    ]
    t = NOW
    for h in bad:
        t += timedelta(hours=1)
        ob.deliver(SessionLocal, http_client(h), now=t)
        assert (outbox(db)[0].state, outbox(db)[0].last_error_code) == ("retry", "protocol")
    ok = lambda r: httpx.Response(201, json=ack(r))  # noqa: E731
    ob.deliver(SessionLocal, http_client(ok), now=t + timedelta(hours=1))
    assert outbox(db)[0].state == "sent"


def test_permanent_failure_is_never_retried_and_blocks_only_its_own_sender(db):
    a, b = new_sender(db, "AAAAA"), new_sender(db, "BBBBB")
    stub = Stub(cc.CpReportRejected(409, "conflict"))
    out = ob.deliver(SessionLocal, stub, now=NOW)
    assert out.failed == 1 and out.sent == 1 and not out.ok
    rows = {r.sender_id: r for r in outbox(db)}
    assert (rows[a.id].state, rows[a.id].last_error_code) == ("failed", "conflict") and rows[
        b.id
    ].state == "sent"
    stub2 = Stub()
    assert (
        ob.deliver(SessionLocal, stub2, now=NOW + timedelta(days=1)).sent == 0 and stub2.calls == []
    )
    db.refresh(a)
    assert a.status == ApprovalStatus.PENDING  # identiteti lokal nuk rishkruhet


def test_stuck_sending_rows_are_recovered_after_the_lease_expires(db):
    new_sender(db)
    r = outbox(db)[0]
    r.state, r.attempts, r.leased_until = "sending", 1, NOW + timedelta(seconds=ob.LEASE_S)
    db.commit()
    stub = Stub()
    assert (
        ob.deliver(SessionLocal, stub, now=NOW + timedelta(seconds=5)).sent == 0
    )  # lease ende i vlefshëm
    assert ob.deliver(SessionLocal, stub, now=NOW + timedelta(seconds=ob.LEASE_S + 1)).sent == 1
    assert outbox(db)[0].attempts == 2 and outbox(db)[0].state == "sent"


def test_a_resubmit_waits_for_its_predecessors_per_sender(db):
    s = new_sender(db)
    sid.reject(db, s.id, "staff", "x")
    db.commit()
    sid.request(db, "c1", "AL", "ACME")
    db.commit()
    stub = Stub(cc.CpTransportError("down"))
    ob.deliver(SessionLocal, stub, now=NOW)
    assert (
        len(stub.calls) == 1 and stub.calls[0]["operation"] == "request"
    )  # ridërgimi s'e kalon kërkesën
    assert [r.state for r in outbox(db)] == ["retry", "pending"]
    later = NOW + timedelta(minutes=5)
    stub2 = Stub()
    ob.deliver(SessionLocal, stub2, now=later)  # vetëm koka e radhës për sender në ciklin e parë
    assert [c["operation"] for c in stub2.calls] == ["request"]
    ob.deliver(SessionLocal, stub2, now=later)
    assert [c["operation"] for c in stub2.calls] == ["request", "resubmit"]
    assert [r.state for r in outbox(db)] == ["sent", "sent"]


def test_failed_head_blocks_later_operations_of_the_same_sender_only(db):
    s = new_sender(db)
    other = new_sender(db, "OTHER1")
    sid.reject(db, s.id, "staff", "x")
    db.commit()
    sid.request(db, "c1", "AL", "ACME")
    db.commit()
    ob.deliver(SessionLocal, Stub(cc.CpReportRejected(409, "conflict")), now=NOW)
    stub = Stub()
    ob.deliver(SessionLocal, stub, now=NOW + timedelta(minutes=5))
    assert stub.calls == []  # resubmit-i i bllokuar nga kërkesa e dështuar
    assert {r.sender_id: r.state for r in outbox(db) if r.request_type == "requested"}[
        other.id
    ] == "sent"


def test_two_workers_cannot_claim_the_same_row(db):
    new_sender(db)
    with SessionLocal() as a, SessionLocal() as b:
        a.begin()
        first = ob._claim(a, NOW, 10)
        first[0].state = "sending"
        a.flush()
        if engine.dialect.name == "postgresql":
            second = ob._claim(b, NOW, 10)  # SKIP LOCKED: rreshti i kyçur kapërcehet
            assert second == []
        a.commit()
    assert (
        ob.deliver(SessionLocal, Stub(), now=NOW).sent == 0
    )  # tashmë sending me lease të vlefshëm? jo: lease s'u vendos ⇒ rikuperohet
    # (claim() i drejtpërdrejtë s'vendos lease; deliver() e vendos — rreshti nuk humbet kurrë)


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_concurrent_reporters_post_each_operation_once(db):
    for i in range(4):
        new_sender(db, f"CONC{i}XX")
    calls, lock, gate = [], threading.Lock(), threading.Barrier(3)

    class Slow(Stub):
        def post_sender_request(self, payload):
            with lock:
                calls.append(payload["operation_id"])
            threading.Event().wait(0.2)
            return super().post_sender_request(payload)

    def run():
        gate.wait(timeout=20)
        ob.deliver(SessionLocal, Slow(), now=NOW)

    ts = [threading.Thread(target=run) for _ in range(3)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert len(calls) == 4 == len(set(calls))
    assert {r.state for r in outbox(db)} == {"sent"}


# =============================================================================================================
# Central i vërtetë: crash pas pranimit, dublikat, rezultate biznesi, E2E me cp.sender.v1
# =============================================================================================================


@pytest.fixture
def loop(db, renv):
    eid = resolve_id(db, "c1")
    db.commit()
    with Session(renv.eng) as s:
        cent.create(s, "Local", enterprise_id=eid)
        service_auth.create_client(s, "loop", ["sender:report", "sender:read"], [eid])
        service_auth.add_key(s, "loop", "k9", renv.public)
        s.commit()
    renv.eid = eid
    return renv


def reporter(loop, scope=cc.SENDER_REPORT_SCOPE):
    key = load_pem_private_key(loop.private.encode(), password=None)
    return cc.ControlPlaneClient(
        cc.ControlPlaneConfig("http://testserver", "loop", "k9", key, 5.0), http=loop, scope=scope
    )


def central_counts(loop):
    with Session(loop.eng) as s:
        return tuple(
            s.scalar(select(func.count()).select_from(m))
            for m in (SenderRegistry, CDecision, SenderRequestOperation)
        )


def registry_of(loop, ref):
    with Session(loop.eng) as s:
        return s.scalar(select(SenderRegistry).where(SenderRegistry.external_ref == ref))


def test_full_loop_local_request_outbox_central_event_projection_without_touching_local_status(
    db, loop
):
    s = new_sender(db, "LoopCo")
    local_before = (s.status, s.approved_key, s.current_decision_id)
    decisions_before = db.scalar(select(func.count()).select_from(SenderDecision))
    assert db.scalar(select(func.count()).select_from(SyncedSenderAuthorization)) == 0
    out = ob.deliver(SessionLocal, reporter(loop), now=NOW)
    assert out.ok and out.sent == 1
    r = outbox(db)[0]
    assert r.state == "sent" and r.ack_outcome == "created"
    reg = registry_of(loop, f"sms-sender-{s.id}")
    assert (
        reg is not None
        and reg.current_status == "pending"
        and str(reg.id) == str(r.ack_registry_ref)
        and reg.enterprise_id == loop.eid
    )
    # transporti ≠ gjendje: projeksioni është ende bosh derisa të vijë cp.sender.v1
    assert db.scalar(select(func.count()).select_from(SyncedSenderAuthorization)) == 0
    poll = sp.poll_once(SessionLocal, reporter(loop, cc.SENDER_SCOPE), snapshot_interval_s=3600)
    assert poll.ok, poll.detail
    db.expire_all()
    proj = db.scalars(select(SyncedSenderAuthorization)).all()
    assert len(proj) == 1 and (proj[0].display_value, proj[0].status, proj[0].external_ref) == (
        "LoopCo",
        "pending",
        f"sms-sender-{s.id}",
    )
    assert str(proj[0].registry_id) == str(reg.id)
    db.refresh(s)
    assert (s.status, s.approved_key, s.current_decision_id) == local_before
    assert db.scalar(select(func.count()).select_from(SenderDecision)) == decisions_before


def test_central_approval_reaches_only_the_projection_never_the_local_sender(db, loop):
    s = new_sender(db, "Approved1")
    ob.deliver(SessionLocal, reporter(loop), now=NOW)
    rid = registry_of(loop, f"sms-sender-{s.id}").id
    mutate(loop, lambda cs, a: csvc.approve(cs, a, rid))
    assert sp.poll_once(SessionLocal, reporter(loop, cc.SENDER_SCOPE), snapshot_interval_s=3600).ok
    db.expire_all()
    proj = db.scalars(select(SyncedSenderAuthorization)).one()
    assert (proj.status, proj.approved_key) == ("approved", "AL:approved1")
    db.refresh(s)
    assert s.status == ApprovalStatus.PENDING and s.approved_key is None


def test_auto_approval_is_transport_success_and_does_not_change_the_local_status(db, loop):
    mutate(loop, lambda cs, a: csvc.set_policy(cs, a, "AL", "alphanumeric", True, False, "open"))
    s = new_sender(db, "AutoOk1")
    out = ob.deliver(SessionLocal, reporter(loop), now=NOW)
    assert out.ok and outbox(db)[0].state == "sent"
    assert registry_of(loop, f"sms-sender-{s.id}").current_status == "approved"
    db.refresh(s)
    assert s.status == ApprovalStatus.PENDING and s.approved_key is None  # POST-i s'e pasqyron
    assert sp.poll_once(SessionLocal, reporter(loop, cc.SENDER_SCOPE), snapshot_interval_s=3600).ok
    db.expire_all()
    assert db.scalars(select(SyncedSenderAuthorization)).one().status == "approved"
    db.refresh(s)
    assert s.status == ApprovalStatus.PENDING


def test_policy_denial_is_transport_success_not_a_retry_loop(db, loop):
    mutate(loop, lambda cs, a: csvc.set_policy(cs, a, "AL", "alphanumeric", False, True, "ban"))
    s = new_sender(db, "Denied1")
    out = ob.deliver(SessionLocal, reporter(loop), now=NOW)
    assert out.ok and outbox(db)[0].state == "sent" and outbox(db)[0].attempts == 1
    assert registry_of(loop, f"sms-sender-{s.id}").current_status == "rejected"
    assert ob.deliver(SessionLocal, reporter(loop), now=NOW + timedelta(days=1)).sent == 0
    assert sp.poll_once(SessionLocal, reporter(loop, cc.SENDER_SCOPE), snapshot_interval_s=3600).ok
    db.expire_all()
    assert db.scalars(select(SyncedSenderAuthorization)).one().status == "rejected"
    db.refresh(s)
    assert s.status == ApprovalStatus.PENDING


def test_crash_after_central_accepted_but_before_local_sent_is_safe(db, loop):
    s = new_sender(db, "CrashCo")
    real = reporter(loop)

    class Crashy:
        def post_sender_request(self, payload):
            real.post_sender_request(payload)  # Central e pranoi
            raise cc.CpTransportError("connection lost after accept")

    assert not ob.deliver(SessionLocal, Crashy(), now=NOW).ok
    assert outbox(db)[0].state == "retry"
    snap = central_counts(loop)
    with Session(loop.eng) as cs:
        events_before = cs.scalar(text("select count(*) from sender_sync_outbox"))
    out = ob.deliver(SessionLocal, real, now=NOW + timedelta(minutes=5))
    assert out.ok and outbox(db)[0].state == "sent" and outbox(db)[0].attempts == 2
    assert central_counts(loop) == snap == (1, 1, 1)  # efekt biznesi saktësisht një herë
    with Session(loop.eng) as cs:
        assert cs.scalar(text("select count(*) from sender_sync_outbox")) == events_before
    db.refresh(s)
    assert s.status == ApprovalStatus.PENDING


def test_resubmit_end_to_end_is_one_central_transition_and_survives_transport_retries(db, loop):
    s = new_sender(db, "ReSub1")
    ob.deliver(SessionLocal, reporter(loop), now=NOW)
    rid = registry_of(loop, f"sms-sender-{s.id}").id
    mutate(loop, lambda cs, a: csvc.reject(cs, a, rid, "no proof"))
    sid.reject(db, s.id, "staff", "local no")
    db.commit()
    sid.request(db, "c1", "AL", "ReSub1")  # ridërgim lokal
    db.commit()
    real = reporter(loop)

    class FlakyAfterAccept:
        n = 0

        def post_sender_request(self, payload):
            real.post_sender_request(payload)
            FlakyAfterAccept.n += 1
            if FlakyAfterAccept.n == 1:
                raise cc.CpTransportError("lost")
            return real.post_sender_request(payload)

    ob.deliver(SessionLocal, FlakyAfterAccept(), now=NOW + timedelta(minutes=1))
    ob.deliver(SessionLocal, FlakyAfterAccept(), now=NOW + timedelta(minutes=10))
    assert [r.state for r in outbox(db)] == ["sent", "sent"]
    with Session(loop.eng) as cs:
        kinds = [d.decision for d in cs.scalars(select(CDecision).order_by(CDecision.seq))]
        ops = cs.scalars(
            select(SenderRequestOperation).order_by(SenderRequestOperation.received_at)
        ).all()
    assert kinds == ["requested", "rejected", "resubmitted"] and [o.outcome for o in ops] == [
        "created",
        "resubmitted",
    ]
    assert registry_of(loop, f"sms-sender-{s.id}").current_status == "pending"


def test_central_outage_never_fails_the_customer_request_and_recovers(db, loop):
    down = Stub(cc.CpTransportError("central unavailable"))
    s = sid.request(db, "c1", "AL", "Offline1")  # kërkesa e klientit: asnjë thirrje Central
    db.commit()
    assert s.status == ApprovalStatus.PENDING and outbox(db)[0].state == "pending"
    assert not ob.deliver(SessionLocal, down, now=NOW).ok
    assert (outbox(db)[0].state, s.status) == (
        "retry",
        ApprovalStatus.PENDING,
    )  # asnjë miratim lokal i shpikur
    assert ob.deliver(SessionLocal, reporter(loop), now=NOW + timedelta(minutes=5)).ok
    assert outbox(db)[0].state == "sent"


def test_reporter_credentials_cannot_read_the_feed_and_read_credentials_cannot_report(db, loop):
    new_sender(db, "Scopes1")
    reader_as_reporter = reporter(loop, cc.SENDER_SCOPE)  # skop sender:read për POST
    out = ob.deliver(SessionLocal, reader_as_reporter, now=NOW)
    assert (
        not out.ok
        and outbox(db)[0].state == "retry"
        and outbox(db)[0].last_error_code == "forbidden"
    )
    with pytest.raises(cc.CpForbidden):
        reporter(loop, cc.SENDER_REPORT_SCOPE).get_sender_snapshot()
    assert central_counts(loop) == (0, 0, 0)


# =============================================================================================================
# gatishmëria, metrikat, worker
# =============================================================================================================


def checks(db, now=NOW):
    return {c.name: c for c in rr.checks(db, now)}


def test_readiness_flags_backlog_permanent_failures_and_stuck_rows(db, monkeypatch):
    monkeypatch.setattr(settings, "sender_request_reporting", True)
    monkeypatch.setattr(settings, "cp_base_url", "https://central.example")
    monkeypatch.setattr(settings, "cp_client_id", "c")
    monkeypatch.setattr(settings, "cp_key_id", "k")
    monkeypatch.setattr(settings, "cp_private_key_path", "/nonexistent.pem")
    assert checks(db)["endpoint_configured"].level == "FAIL"  # çelës i palexueshëm
    monkeypatch.setattr(settings, "cp_private_key_path", str(ROOT / "pyproject.toml"))
    c = checks(db)
    assert (
        c["endpoint_configured"].level == "PASS"
        and c["no_permanent_failures"].level == "PASS"
        and c["oldest_pending_age"].level == "PASS"
    )
    s = new_sender(db, "Backlog1")
    created = outbox(db)[0].created_at.replace(tzinfo=UTC)
    alert = settings.sender_request_alert_age_seconds
    assert checks(db, created + timedelta(seconds=alert - 1))["oldest_pending_age"].level == "PASS"
    assert checks(db, created + timedelta(seconds=alert + 1))["oldest_pending_age"].level == "WARN"
    assert (
        checks(db, created + timedelta(seconds=alert * 6 + 1))["oldest_pending_age"].level == "FAIL"
    )
    assert checks(db, created + timedelta(seconds=alert * 6 + 1))["recent_delivery"].level == "WARN"
    r = outbox(db)[0]
    r.state, r.leased_until = "sending", created - timedelta(seconds=1)
    db.commit()
    assert checks(db, created + timedelta(seconds=5))["no_stuck_sending"].level == "WARN"
    r = outbox(db)[0]
    r.state, r.last_error_code, r.leased_until = "failed", "conflict", None
    db.commit()
    f = checks(db)["no_permanent_failures"]
    assert f.level == "FAIL" and "conflict" in f.reason and "Backlog1" not in f.reason
    assert rr.overall(list(checks(db).values())) == "FAIL"
    del s


def test_readiness_detects_credential_or_scope_rejection_and_https_in_production(db, monkeypatch):
    new_sender(db, "AuthBad1")
    ob.deliver(SessionLocal, Stub(cc.CpForbidden("403")), now=NOW)
    assert checks(db)["scope_granted"].level == "FAIL"
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "cp_base_url", "http://central")
    assert checks(db)["https_required"].level == "FAIL"
    monkeypatch.setattr(settings, "sender_request_reporting", False)
    assert checks(db)["reporter_enabled"].level == "WARN"


def test_metrics_and_cli_expose_no_sender_values(db, capsys):
    from scripts import sender_request_readiness as cli

    new_sender(db, "TopSecret")
    ob.deliver(SessionLocal, Stub(cc.CpReportRejected(409, "conflict")), now=NOW)
    new_sender(db, "Pending2")
    st = rr.status(db, NOW + timedelta(seconds=60))
    assert st["failed"] == 1 and st["pending"] == 1 and st["failed_by_category"] == {"conflict": 1}
    assert st["attempts_total"] == 1 and st["oldest_open_age_seconds"] is not None
    assert cli.main(["--json"]) in (0, 1)
    out = capsys.readouterr().out
    assert {"status", "checks", "metrics"} <= set(json.loads(out))
    for needle in ("TopSecret", "topsecret", "Pending2", "sms-sender-"):
        assert needle not in out


def test_worker_role_is_separate_idle_without_flag_and_misconfiguration_is_reported(monkeypatch):
    import app.worker as worker
    from app.services import sender_sync_poller

    src = Path(worker.__file__).read_text()
    assert "sender_request_reporter" in src and "run_sender_request_reporter" in src
    assert ob.LOCK_KEY != sender_sync_poller.LOCK_KEY  # push ≠ pull
    monkeypatch.setattr(settings, "sender_request_reporting", True)
    monkeypatch.setattr(settings, "cp_base_url", "")
    assert worker.run_sender_request_reporter(once=True) == 2
    body = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(body)
        if isinstance(n, ast.FunctionDef) and n.name == "run_sender_request_reporter"
    )
    assert not any(isinstance(n, ast.Attribute) and n.attr == "poll_once" for n in ast.walk(fn))


def test_production_requires_https_for_the_reporter():
    from app.core.config import Settings

    s = Settings(env="production", sender_request_reporting=True, cp_base_url="http://central")
    assert any("SMS_SENDER_REQUEST_REPORTING" in p for p in s.production_problems())


# =============================================================================================================
# kufijtë e transportit: asnjë shkurtore statusi
# =============================================================================================================


def test_transport_code_never_writes_local_authorization_or_projection_state():
    for name in ("sender_request_outbox.py", "sender_request_readiness.py"):
        tree = ast.parse((ROOT / "app" / "services" / name).read_text())
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert (
            "app.models.sender_sync" not in imported and "app.services.sender_sync" not in imported
        )
        assert (
            "app.services.sender_ids" not in imported
            and "app.services.sender_authorization" not in imported
        )
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not names & {
            "SenderDecision",
            "SyncedSenderAuthorization",
            "SyncedSenderPolicy",
            "approved_key",
            "approve",
            "reject",
            "revoke",
            "transition",
        }, name
    # SenderId lexohet vetëm për enqueue (identiteti), asnjëherë nuk ndryshohet
    src = (ROOT / "app" / "services" / "sender_request_outbox.py").read_text()
    assert "sender.status" not in src and ".status =" not in src.replace("r.state", "")


# =============================================================================================================
# migrimi 0030 dhe trigger-at PG
# =============================================================================================================


def _drift(conn):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    import app.models  # noqa: F401
    from app.core.db import Base

    ctx = MigrationContext.configure(conn, opts={"compare_type": True})
    return [
        repr(i)
        for d in compare_metadata(ctx, Base.metadata)
        for i in (d if isinstance(d, list) else [d])
        if "sms_sender_request_outbox" in repr(i)
    ]


def test_enterprise_0030_is_additive_reversible_and_matches_metadata(make_db):
    url = make_db("ent")
    enterprise_alembic(url, "upgrade", "0029")
    eng = create_engine(url)
    before = {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    }
    assert "sms_sender_request_outbox" not in before
    enterprise_alembic(url, "upgrade", "0030")
    assert "sms_sender_request_outbox" in inspect(eng).get_table_names()
    for t, cols in before.items():
        assert {
            c["name"] for c in inspect(eng).get_columns(t)
        } == cols  # asnjë tabelë ekzistuese s'ndryshon
    with eng.connect() as c:
        assert _drift(c) == []
    enterprise_alembic(url, "downgrade", "0029")
    assert "sms_sender_request_outbox" not in inspect(eng).get_table_names()
    enterprise_alembic(url, "upgrade", "head")
    with eng.connect() as c:
        assert _drift(c) == []
    eng.dispose()


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_triggers_freeze_content_and_forbid_delete_and_truncate(make_db):
    url = make_db("ent")
    enterprise_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    with Session(eng) as s:
        sid.request(s, "c1", "AL", "TrigCo")
        s.commit()
    ok = "UPDATE sms_sender_request_outbox SET state = 'retry', attempts = 1, last_error_code = 'transport'"
    with eng.begin() as c:
        c.execute(text(ok))  # fushat e dërgimit ndryshojnë lirshëm
    for stmt in (
        "UPDATE sms_sender_request_outbox SET payload = '{}'::json",
        "UPDATE sms_sender_request_outbox SET request_hash = repeat('0', 64)",
        "UPDATE sms_sender_request_outbox SET operation_id = gen_random_uuid()",
        "UPDATE sms_sender_request_outbox SET request_type = 'resubmitted'",
        "UPDATE sms_sender_request_outbox SET created_at = now()",
        "DELETE FROM sms_sender_request_outbox",
        "TRUNCATE sms_sender_request_outbox",
    ):
        with eng.connect() as c:
            with pytest.raises(DBAPIError):
                c.execute(text(stmt))
                c.commit()
    with eng.connect() as c:
        assert c.execute(text("select count(*) from sms_sender_request_outbox")).scalar() == 1
    eng.dispose()
