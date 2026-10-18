"""M11-J: siguria e provider-it dhe e dërgimit: G-01 (id e dyfishtë), G-03 (throttling), G-04 (Twilio 401/403),
G-05 (replay i webhook-ut), G-07 (retention + PII i DlrReceipt). Provider të rremë / transport i simuluar; pa rrjet."""

import hashlib
import hmac
import importlib.util
import json
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

import app.providers as providers
from app.core.config import Settings, settings
from app.core.db import SessionLocal, engine
from app.main import create_app
from app.models.admin import AuditLog
from app.models.sending import DlrReceipt, Message, MessageEvent, MessageStatus
from app.providers import SendResult
from app.providers.twilio import TwilioProvider, twilio_signature
from app.services import messages as svc
from app.services import wallet as wallets
from app.services.provider_throttle import throttle
from scripts import dlr_receipt_retention as ret
from scripts import provider_readiness as pr
from tests.test_f07_provider_dlr import Scripted, held
from tests.test_pipeline import OK, fake, send, world  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
PG = pytest.mark.skipif(
    not os.environ.get("SMS_TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="needs PostgreSQL",
)
SECRET = "m11j-dlr-" + hashlib.sha1(b"m11j").hexdigest()  # sintetik, 49 kar.
NOW0 = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_throttle():
    throttle.reset()
    yield
    throttle.reset()


@pytest.fixture
def prov(monkeypatch):
    def install(p, name="fake"):
        monkeypatch.setitem(providers._registry, name, p)
        return p

    return install


@pytest.fixture
def wh(monkeypatch):
    monkeypatch.setattr(settings, "dlr_secrets", {"fake": SECRET})
    return TestClient(create_app())


def dlr(wh, body: dict, secret: str = SECRET):
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return wh.post("/webhooks/dlr/fake", content=raw, headers={"X-Signature": sig})


def _sent(db, prov):
    prov(Scripted())
    m = send(db)
    svc.process_one(db)
    db.refresh(m)
    return m


# ============================================================ G-01 — id e dyfishtë e provider-it


def test_first_provider_id_is_accepted_and_repeats_for_the_same_message_are_idempotent(
    db, world, prov
):  # noqa: F811
    m = _sent(db, prov)
    assert m.status == MessageStatus.SENT and m.provider_message_id == "s-1"
    first = svc.apply_dlr(db, "fake", "s-1", True)
    again = svc.apply_dlr(
        db, "fake", "s-1", True
    )  # i njëjti mesazh, e njëjta id: pa Conflict, pa lëvizje të dytë
    db.commit()
    assert first.id == again.id == m.id and again.status == MessageStatus.DELIVERED


def test_database_refuses_two_rows_with_the_same_provider_id_but_nulls_and_other_providers_are_fine(
    db, world, prov
):  # noqa: F811
    a, b, c = (send(db, key=k) for k in "abc")
    a.provider_message_id = "x-1"
    db.commit()
    b.provider_message_id = "x-1"
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    c.provider = "other"
    c.provider_message_id = "x-1"  # i njëjti id, provider tjetër: lejohet
    db.commit()
    assert (
        db.query(Message).filter(Message.provider_message_id.is_(None)).count() == 1
    )  # b mbetet NULL
    d, e = send(db, key="d"), send(db, key="e")
    assert d.provider_message_id is None and e.provider_message_id is None  # shumë NULL pa konflikt


def test_second_message_with_a_used_id_becomes_unknown_keeps_its_hold_and_is_never_resent(
    db, world, prov
):  # noqa: F811
    w, _ = world
    p = prov(Scripted(SendResult("dup-9"), SendResult("dup-9")))
    a, b = send(db, key="a"), send(db, key="b")
    svc.process_one(db)
    svc.process_one(db)
    db.refresh(a), db.refresh(b)
    assert (a.status, a.provider_message_id) == (MessageStatus.SENT, "dup-9")
    assert (b.status, b.provider_message_id, b.error_code) == (
        MessageStatus.UNKNOWN,
        None,
        svc.DUPLICATE_PROVIDER_ID,
    )
    for _ in range(3):
        svc.process_one(db)
        svc.recover_stuck(db, lease=timedelta(0))
    db.commit()
    assert len(p.calls) == 2 and held(db, w) == (D("9.90"), D("0.10"))
    # zgjidhja e UNKNOWN funksionon pa e lidhur id-në e përdorur; një id e re mund të lidhet
    with pytest.raises(wallets.Conflict):
        svc.attach_provider_message_id(
            db, b.public_id, "dup-9", actor="o", role="superadmin", reason="clash"
        )
    db.rollback()
    svc.resolve_unknown(
        db,
        b.public_id,
        "non_billable_failed",
        actor="o",
        role="superadmin",
        reason="provider sent no usable id",
    )
    db.commit()
    assert held(db, w) == (D("9.95"), D("0.05")) and wallets.verify_wallet(db, w.id)


def test_a_race_that_slips_past_the_check_is_caught_by_the_unique_index(
    db, world, prov, monkeypatch
):  # noqa: F811
    prov(Scripted(SendResult("race-1"), SendResult("race-1")))
    a, b = send(db, key="a"), send(db, key="b")
    svc.process_one(db)
    monkeypatch.setattr(svc, "_id_owned_by_other", lambda *x: False)  # kontrolli «humbet» garën
    svc.process_one(db)
    db.refresh(a), db.refresh(b)
    assert a.status == MessageStatus.SENT and b.status == MessageStatus.UNKNOWN
    assert b.error_code == svc.DUPLICATE_PROVIDER_ID and b.provider_message_id is None
    assert [e.to_status for e in db.query(MessageEvent).filter_by(message_id=b.id).order_by(MessageEvent.id)] == [
        "queued", "sending", "unknown",
    ]  # fmt: skip


def test_late_ack_never_records_an_id_owned_by_another_message(db, world, prov):  # noqa: F811
    a, b = send(db, key="a"), send(db, key="b")
    a.provider_message_id = "late-1"
    b.status, b.error_code = MessageStatus.UNKNOWN, "stuck_sending"
    db.commit()
    svc._lost_claim(db, b, SendResult("late-1"))
    db.commit()
    db.refresh(b)
    assert b.provider_message_id is None
    assert "late_ack:duplicate_provider_id" in [
        e.detail for e in db.query(MessageEvent).filter_by(message_id=b.id)
    ]


@PG
def test_pg_concurrent_acknowledgements_of_the_same_id_give_exactly_one_owner(db, world, prov):  # noqa: F811
    from tests.test_queue_concurrency_pg import run_threads

    prov(Scripted())
    ids = [send(db, key=k).id for k in "ab"]
    svc.claim_next(db), svc.claim_next(db)
    db.commit()

    def work(i, barrier):
        with SessionLocal() as s:
            m = s.get(Message, ids[i], with_for_update=True)
            barrier.wait()
            svc._acknowledge(s, m, m.attempts, "same-id")
            s.commit()

    # kyçet FOR UPDATE janë të ndara (rreshta të ndryshëm): të dy arrijnë te INSERT i indeksit unik njëkohësisht
    run_threads(2, work)
    db.expire_all()
    rows = [db.get(Message, i) for i in ids]
    assert sorted(r.status.value for r in rows) == ["sent", "unknown"]
    assert [r.provider_message_id for r in rows].count("same-id") == 1


def test_historical_duplicates_make_the_migration_refuse_without_touching_data(db, world):  # noqa: F811
    spec = importlib.util.spec_from_file_location(
        "m0035", ROOT / "alembic/versions/0035_provider_safety_m11j.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    a, b = send(db, key="a"), send(db, key="b")
    db.execute(text("drop index uq_sms_messages_provider_msg"))
    db.execute(text("update sms_messages set provider_message_id = 'hist-dup'"))
    db.commit()
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with engine.connect() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match="duplicate-ids"):
                mod.upgrade()
    groups = pr.duplicate_groups(db)
    assert (
        len(groups) == 1
        and groups[0]["messages"] == 2
        and set(groups[0]["public_ids"]) == {a.public_id, b.public_id}
    )
    assert (
        db.query(Message).filter_by(provider_message_id="hist-dup").count() == 2
    )  # asnjë histori e ndryshuar


def test_migration_0035_is_additive_reversible_and_keeps_historical_receipts(tmp_path):
    from sqlalchemy import create_engine, inspect

    url = f"sqlite:///{tmp_path / 'm.db'}"
    env = {**os.environ, "SMS_DATABASE_URL": url, "PYTHONPATH": "."}

    def alembic(*args):
        r = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            env=env,
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        assert r.returncode == 0, r.stderr

    alembic("upgrade", "0034")
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(
            text(
                "insert into sms_dlr_receipts (provider, provider_message_id, status, outcome, raw_body, received_at) values ('p','m','delivered','applied','To=%2B355690000000','2020-01-01')"
            )
        )
    alembic("upgrade", "head")
    cols = {c["name"] for c in inspect(eng).get_columns("sms_dlr_receipts")}
    assert {"fingerprint", "seen_count", "last_seen_at"} <= cols
    with eng.connect() as c:
        row = c.execute(
            text("select raw_body, fingerprint, seen_count from sms_dlr_receipts")
        ).one()
    assert tuple(row) == ("To=%2B355690000000", None, 1)  # histori e paprekur (nuk rishkruhet)
    alembic("downgrade", "0034")
    assert "fingerprint" not in {c["name"] for c in inspect(eng).get_columns("sms_dlr_receipts")}
    alembic("upgrade", "head")


# ============================================================ G-03 — throttling


def _queue(db, n):
    return [send(db, key=f"k{i}") for i in range(n)]


def test_rate_limit_is_respected_and_the_worker_resumes_after_the_window(
    db, world, prov, monkeypatch
):  # noqa: F811
    monkeypatch.setattr(settings, "provider_rate_per_sec", {"fake": 2.0})
    p = prov(Scripted())
    ms = _queue(db, 5)
    t0 = datetime.now(UTC) + timedelta(days=1)
    assert [svc.process_one(db, t0) is not None for _ in range(3)] == [
        True,
        True,
        False,
    ]  # kapaciteti 2, i treti pret
    assert len(p.calls) == 2
    waiting = [m for m in ms if (db.refresh(m) or m.status) == MessageStatus.QUEUED]
    assert len(waiting) == 3 and all(
        m.attempts == 0 and m.error_code is None for m in waiting
    )  # të paprekura
    assert svc.process_one(db, t0 + timedelta(seconds=0.5)) is not None  # +1 token
    assert svc.process_one(db, t0 + timedelta(seconds=0.5)) is None
    assert [svc.process_one(db, t0 + timedelta(seconds=10)) is not None for _ in range(3)] == [
        True,
        True,
        False,
    ]
    assert (
        len(p.calls) == 5 and len({c.reference for c in p.calls}) == 5
    )  # secili dërguar saktësisht një herë
    assert {m.status for m in (db.refresh(x) or x for x in ms)} == {MessageStatus.SENT}


def test_providers_are_throttled_independently(db, world, prov, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "provider_rate_per_sec", {"fake": 1.0})
    a_calls = prov(Scripted())
    b_calls = prov(Scripted(), name="other")
    ms = _queue(db, 4)
    db.query(Message).filter(Message.id.in_([ms[2].id, ms[3].id])).update({"provider": "other"})
    db.commit()
    t0 = datetime.now(UTC) + timedelta(days=1)
    for _ in range(6):
        svc.process_one(db, t0)
    assert len(a_calls.calls) == 1 and len(b_calls.calls) == 2  # "fake" kufizohet, "other" jo


def test_max_concurrency_counts_in_flight_calls_across_workers(db, world, prov, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "provider_max_concurrency", {"fake": 1})
    p = prov(Scripted())
    a, b = _queue(db, 2)
    now = datetime.now(UTC)
    first = svc.claim_next(db, now)  # simulon një punonjës tjetër në mes të thirrjes
    first.dispatch_started_at = now
    db.commit()
    assert svc.process_one(db, now) is None and len(p.calls) == 0  # kufiri 1 i zënë
    first.status = MessageStatus.SENT  # thirrja mbaroi
    db.commit()
    assert svc.process_one(db, now) is not None and len(p.calls) == 1


def test_no_limits_configured_keeps_the_old_behaviour_with_zero_extra_queries(db, world, prov):  # noqa: F811
    assert settings.provider_rate_per_sec == {} and settings.provider_max_concurrency == {}
    p = prov(Scripted())
    _queue(db, 5)
    statements = []
    hook = lambda *a: statements.append(a[2])  # noqa: E731
    event.listen(engine, "before_cursor_execute", hook)
    try:
        assert throttle.blocked(db, datetime.now(UTC)) == {} and statements == []
    finally:
        event.remove(engine, "before_cursor_execute", hook)
    assert all(svc.process_one(db) is not None for _ in range(5)) and len(p.calls) == 5


def test_throttling_is_logged_once_and_visible_in_admin_stats(
    db, world, prov, monkeypatch, caplog, client
):  # noqa: F811
    monkeypatch.setattr(settings, "provider_rate_per_sec", {"fake": 1.0})
    prov(Scripted())
    _queue(db, 3)
    t0 = datetime.now(UTC) + timedelta(days=1)
    caplog.set_level(logging.WARNING, logger="sms.throttle")
    for _ in range(4):
        svc.process_one(db, t0)
    lines = [r.getMessage() for r in caplog.records if "provider throttled" in r.getMessage()]
    assert len(lines) == 1 and "provider=fake" in lines[0] and "reason=rate" in lines[0]
    safety = client.get("/v1/admin/stats").json()["provider_safety"]
    assert safety["throttle"]["rate_per_sec"] == {"fake": 1.0} and safety["throttle"][
        "queued_due"
    ] == {"fake": 2}


def test_limit_settings_are_validated():
    for bad in (
        {"provider_rate_per_sec": {"x": 0}},
        {"provider_rate_per_sec": {"x": 5000}},
        {"provider_max_concurrency": {"x": 0}},
    ):
        with pytest.raises(ValueError):
            Settings(**bad)
    s = Settings(provider_rate_per_sec={"twilio": 5}, provider_max_concurrency={"twilio": 2})
    assert s.provider_rate_per_sec == {"twilio": 5.0} and s.provider_max_concurrency == {
        "twilio": 2
    }


# ============================================================ G-04 — Twilio 401/403


def _twilio(status, body):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(status, json=body)

    return TwilioProvider(
        "ACm11j",
        "m11j-token-sintetik",
        "https://x.invalid/webhooks/twilio/status",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ), calls


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (401, {"code": 20003}, "twilio_auth"),
        (403, {}, "twilio_forbidden"),
        (403, {"code": 20008}, "twilio_20008"),
    ],
)
def test_twilio_auth_and_permission_errors_fail_fast_and_release_the_hold(
    db, world, prov, status, body, code, client
):  # noqa: F811
    w, _ = world
    tw, calls = _twilio(status, {**body, "message": "leaks m11j-token-sintetik"})
    prov(tw)
    ms = [send(db, key=f"k{i}") for i in range(3)]
    for i in range(3):
        svc.process_one(db, datetime.now(UTC) + timedelta(days=i + 1))
    for m in ms:
        db.refresh(m)
        assert (m.status, m.error_code, m.attempts) == (MessageStatus.FAILED, code, 1)
        assert "m11j-token" not in (m.error_code or "")
    assert len(calls) == 3 and held(db, w) == (
        D("10"),
        D("0"),
    )  # një thirrje për mesazh, hold-et të liruara
    assert client.get("/v1/admin/stats").json()["provider_safety"]["twilio_auth_failures_24h"] == 3
    post = {
        c.name: c
        for c in pr.metric_checks(
            db,
            "fake",
            hours=24,
            min_sample=1,
            min_rate=0.0,
            max_unknown=0,
            max_sent_age=3600,
            max_queued_age=900,
            now=datetime.now(UTC),
        )
    }
    assert post["provider_auth_failures"].level == "FAIL"


@pytest.mark.parametrize("status", [408, 425, 429, 502, 503, 504])
def test_retryable_twilio_statuses_keep_their_retry_behaviour(db, world, prov, status):  # noqa: F811
    tw, calls = _twilio(status, {})
    prov(tw)
    m = send(db)
    svc.process_one(db)
    db.refresh(m)
    expected = MessageStatus.QUEUED if status in (429, 502, 503, 504) else MessageStatus.FAILED
    assert (
        m.status == expected and len(calls) == 1
    )  # 408/425 s'ishin kurrë të përkohshme te Twilio (4xx ⇒ i përhershëm): pa ndryshim


# ============================================================ G-05 — replay i webhook-ut


def test_exact_replay_changes_nothing_but_the_counter(db, world, prov, wh):  # noqa: F811
    w, _ = world
    m = _sent(db, prov)
    body = {"provider_message_id": m.provider_message_id, "status": "delivered"}
    assert dlr(wh, body).json() == {"outcome": "applied"}
    events, money = db.query(MessageEvent).count(), held(db, w)
    for _ in range(3):
        assert dlr(wh, body).status_code == 200
    db.expire_all()
    r = db.query(DlrReceipt).one()
    assert (r.outcome, r.seen_count) == ("applied", 4) and r.last_seen_at is not None
    assert db.query(MessageEvent).count() == events and held(db, w) == money


def test_a_different_signed_callback_is_not_a_replay_and_hits_the_state_machine(
    db, world, prov, wh
):  # noqa: F811
    m = _sent(db, prov)
    pid = m.provider_message_id
    assert dlr(wh, {"provider_message_id": pid, "status": "delivered"}).status_code == 200
    contradict = {"provider_message_id": pid, "status": "failed", "code": "x"}
    assert [dlr(wh, contradict).status_code for _ in range(3)] == [
        409,
        409,
        409,
    ]  # i dyti+ = replay i konfliktit
    db.expire_all()
    rows = {r.outcome: r.seen_count for r in db.query(DlrReceipt)}
    assert (
        rows == {"applied": 1, "conflict": 3}
        and db.get(Message, m.id).status == MessageStatus.DELIVERED
    )
    assert (
        dlr(wh, {**contradict, "code": "y"}).status_code == 409
    )  # gjurmë tjetër ⇒ shqyrtohet nga e para
    assert db.query(DlrReceipt).filter_by(outcome="conflict").count() == 2


def test_unknown_message_is_never_deduplicated_so_the_provider_retry_can_succeed(
    db, world, prov, wh
):  # noqa: F811
    p = prov(Scripted())
    m = send(db)
    pid = "s-1"
    body = {"provider_message_id": pid, "status": "delivered"}
    assert [dlr(wh, body).status_code for _ in range(2)] == [503, 503]
    unknown = db.query(DlrReceipt).one()
    assert (unknown.outcome, unknown.seen_count) == (
        "unknown_message",
        2,
    )  # një rresht, jo rritje e pakufizuar
    svc.process_one(db)
    assert (
        dlr(wh, body).status_code == 200
    )  # tani mesazhi ekziston: aplikohet, pa u bllokuar nga dedupe
    db.expire_all()
    assert db.get(Message, m.id).status == MessageStatus.DELIVERED and len(p.calls) == 1
    assert {r.outcome for r in db.query(DlrReceipt)} == {"unknown_message", "applied"}


def test_signature_stays_mandatory_for_replays_and_unsigned_ones_touch_nothing(db, world, prov, wh):  # noqa: F811
    m = _sent(db, prov)
    body = {"provider_message_id": m.provider_message_id, "status": "delivered"}
    dlr(wh, body)
    raw = json.dumps(body).encode()
    for headers in (
        {},
        {"X-Signature": "sha256=" + "0" * 64},
        {"X-Replay-Ok": "1"},
        {"X-Signature": "sha256=" + hmac.new(b"wrong", raw, hashlib.sha256).hexdigest()},
    ):
        assert wh.post("/webhooks/dlr/fake", content=raw, headers=headers).status_code == 401
    db.expire_all()
    assert db.query(DlrReceipt).one().seen_count == 1  # asnjë rritje nga kërkesat e pa-nënshkruara


def test_fingerprint_is_deterministic_secret_free_and_field_sensitive():
    from app.api.webhooks import DlrIn, receipt_fingerprint

    base = DlrIn(provider_message_id="SM1", status="delivered")
    fp = receipt_fingerprint("twilio", base)
    assert fp == receipt_fingerprint("twilio", base) and len(fp) == 64 and SECRET not in fp
    assert fp != receipt_fingerprint("fake", base)
    assert fp != receipt_fingerprint("twilio", DlrIn(provider_message_id="SM1", status="failed"))
    assert fp != receipt_fingerprint(
        "twilio", DlrIn(provider_message_id="SM1", status="delivered", code="twilio_x")
    )


@PG
def test_pg_concurrent_identical_callbacks_apply_once_and_share_one_receipt(db, world, prov, wh):  # noqa: F811
    from tests.test_queue_concurrency_pg import run_threads

    w, _ = world
    m = _sent(db, prov)
    body = {"provider_message_id": m.provider_message_id, "status": "delivered"}
    from app.api.webhooks import DlrIn, _handle

    def work(i, barrier):
        barrier.wait()
        return _handle("fake", DlrIn(**body))

    out = run_threads(4, work)
    assert out == [(200, "applied")] * 4
    db.expire_all()
    r = db.query(DlrReceipt).one()
    assert r.seen_count == 4 and held(db, w) == (D("9.95"), D("0"))
    assert db.get(Message, m.id).status == MessageStatus.DELIVERED


# ============================================================ G-07 — PII + retention


def test_hmac_receipts_store_only_the_minimized_callback(db, world, prov, wh):  # noqa: F811
    m = _sent(db, prov)
    dlr(
        wh,
        {
            "provider_message_id": m.provider_message_id,
            "status": "failed",
            "code": "absent",
            "reference": m.public_id,
        },
    )
    r = db.query(DlrReceipt).one()
    assert json.loads(r.raw_body) == {
        "code": "absent",
        "provider_message_id": m.provider_message_id,
        "reference": m.public_id,
        "status": "failed",
    }
    assert SECRET not in r.raw_body and "sha256" not in r.raw_body


def test_twilio_receipts_never_store_phone_numbers_or_the_signature(db, world, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "twilio_auth_token", "m11j-twilio-token-sintetik")
    monkeypatch.setattr(settings, "public_base_url", "https://platform.invalid")
    c = TestClient(create_app())
    m = send(db)
    m.provider, m.provider_message_id = "twilio", "SM11J"
    m.status = MessageStatus.SENT
    db.commit()
    params = [
        ("MessageSid", "SM11J"),
        ("MessageStatus", "delivered"),
        ("To", "+355691230003"),
        ("From", "+14155550100"),
        ("AccountSid", "ACm11j"),
    ]
    sig = twilio_signature(
        "m11j-twilio-token-sintetik", "https://platform.invalid/webhooks/twilio/status", params
    )
    for _ in range(2):
        r = c.post(
            "/webhooks/twilio/status",
            content=urlencode(params).encode(),
            headers={
                "X-Twilio-Signature": sig,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        assert r.status_code == 200
    row = db.query(DlrReceipt).one()
    assert row.seen_count == 2 and row.provider == "twilio"
    for needle in ("355691230003", "14155550100", "ACm11j", sig, "m11j-twilio-token"):
        assert needle not in row.raw_body
    assert json.loads(row.raw_body) == {
        "code": "twilio_delivered",
        "provider_message_id": "SM11J",
        "status": "delivered",
    }


def _receipt(db, age_s: int, outcome="applied", body="To=%2B355690000000", base=NOW0, i=[0]):  # noqa: B006
    i[0] += 1
    db.add(
        DlrReceipt(
            provider="p",
            provider_message_id=f"m{i[0]}",
            status="delivered",
            outcome=outcome,
            raw_body=body,
            received_at=base - timedelta(seconds=age_s),
        )
    )


def test_retention_defaults_to_dry_run_and_never_deletes_without_apply(db):
    assert settings.dlr_receipt_retention_days == 90
    day = 86400
    for age in (91 * day, 120 * day, 1 * day):
        _receipt(db, age)
    db.commit()
    out = ret.run(db, days=90, batch=100, max_batches=10, apply=False, now=NOW0)
    assert (
        out["mode"] == "dry-run"
        and out["would_delete_total"] == 2
        and out["kept"] == 1
        and "deleted" not in out
    )
    assert db.query(DlrReceipt).count() == 3


def test_retention_boundary_is_strict_and_newer_rows_survive(db):
    day = 86400
    _receipt(db, 90 * day)  # saktësisht në prag ⇒ mbahet
    _receipt(db, 90 * day + 1)  # 1 s më i vjetër ⇒ fshihet
    _receipt(db, 89 * day)
    db.commit()
    out = ret.run(db, days=90, batch=100, max_batches=10, apply=True, now=NOW0)
    assert out["deleted"] == 1 and db.query(DlrReceipt).count() == 2
    assert min(r.received_at for r in db.query(DlrReceipt)).replace(tzinfo=UTC) >= NOW0 - timedelta(
        days=90
    )
    audit = db.query(AuditLog).filter_by(action="dlr_receipt.retention").one()
    assert "deleted" in str(audit.detail) and audit.role == "system"


def test_retention_deletes_in_batches_and_honours_max_batches(db):
    for _ in range(25):
        _receipt(db, 200 * 86400)
    db.commit()
    first = ret.run(db, days=90, batch=10, max_batches=1, apply=True, now=NOW0)
    assert first["deleted"] == 10 and db.query(DlrReceipt).count() == 15
    rest = ret.run(db, days=90, batch=10, max_batches=10, apply=True, now=NOW0)
    assert rest["deleted"] == 15 and db.query(DlrReceipt).count() == 0


def test_retention_cli_output_has_counters_only_and_validates_input(db, capsys):
    secret_phone = "355691230003"
    _receipt(
        db, 400 * 86400, body=f"To=%2B{secret_phone}&From=%2B14155550100", base=datetime.now(UTC)
    )
    db.commit()
    assert ret.main(["--days", "90", "--json"]) == 0
    dry = capsys.readouterr().out
    assert (
        secret_phone not in dry
        and "14155550100" not in dry
        and json.loads(dry)["would_delete_total"] == 1
    )
    assert db.query(DlrReceipt).count() == 1  # dry-run: asgjë e fshirë
    assert ret.main(["--days", "3"]) == 2 and ret.main(["--batch", "0"]) == 2
    assert ret.main(["--days", "90", "--apply"]) == 0
    applied = capsys.readouterr().out
    assert secret_phone not in applied and "deleted=1" in applied
    db.expire_all()
    assert db.query(DlrReceipt).count() == 0


@PG
def test_pg_retention_apply_refuses_to_run_while_another_run_holds_the_lock(db):
    _receipt(db, 400 * 86400, base=datetime.now(UTC))
    db.commit()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as other:
        assert other.execute(text("select pg_try_advisory_lock(:k)"), {"k": ret.LOCK_KEY}).scalar()
        assert ret.main(["--days", "90", "--apply"]) == 1
        assert db.query(DlrReceipt).count() == 1
        other.execute(text("select pg_advisory_unlock(:k)"), {"k": ret.LOCK_KEY})
    assert ret.main(["--days", "90", "--apply"]) == 0


# ============================================================ observabilitet + siguri


def test_admin_stats_expose_provider_safety_signals(db, world, prov, wh, client):  # noqa: F811
    m = _sent(db, prov)
    body = {"provider_message_id": m.provider_message_id, "status": "delivered"}
    for _ in range(3):
        dlr(wh, body)
    dlr(wh, {**body, "status": "failed"})
    stats = client.get("/v1/admin/stats").json()
    s = stats["provider_safety"]
    assert s["dlr_replays_24h"] == 2 and s["duplicate_provider_id"] == 0
    assert stats["dlr_problems_24h"] == {"conflict": 1}
    assert set(s) == {
        "duplicate_provider_id",
        "twilio_auth_failures_24h",
        "dlr_replays_24h",
        "throttle",
    }


def test_readiness_reports_duplicate_conflicts_and_throttle_configuration(
    db, world, prov, monkeypatch
):  # noqa: F811
    monkeypatch.setattr(settings, "provider_rate_per_sec", {"fake": 3.0})
    prov(Scripted(SendResult("d"), SendResult("d")))
    send(db, key="a"), send(db, key="b")
    svc.process_one(db), svc.process_one(db)
    checks = {
        c.name: c
        for c in pr.evaluate(db, "fake", phase="post", min_sample=1, now=datetime.now(UTC))
    }
    assert checks["duplicate_provider_id_conflicts"].level == "WARN"
    assert (
        "rate_per_sec=3.0" in checks["throttle_config"].reason
        and "unlimited" in checks["throttle_config"].reason
    )
    assert checks["dlr_replays"].level == "PASS"


def test_new_code_and_migration_contain_no_credentials_or_secret_logging():
    for rel in (
        "alembic/versions/0035_provider_safety_m11j.py",
        "scripts/dlr_receipt_retention.py",
        "app/services/provider_throttle.py",
    ):
        src = (ROOT / rel).read_text().lower()
        for bad in ("password=", "authorization", "bearer ", "auth_token", "api_key", "secret ="):
            assert bad not in src, (rel, bad)
    log_src = (ROOT / "app/services/provider_throttle.py").read_text()
    assert (
        "log.warning" in log_src and "provider=%s reason=%s" in log_src
    )  # nume provider + arsye, asgjë tjetër
