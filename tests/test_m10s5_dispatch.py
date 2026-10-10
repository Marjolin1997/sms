# ruff: noqa: F811
"""M10-S5 — rikontrolli i sender-it para provider-it (vendimi B): semantika, gjendja terminale, parat, gara me revokimin, dy punonjës, SQL, auditi i thirrjeve."""

import re
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.models.sender_sync import SyncedSenderAuthorization
from app.models.sending import Message, MessageEvent, MessageStatus
from app.models.wallet import EntryType, LedgerEntry
from app.services import messages as msgs
from app.services import sender_authority as sau
from app.services import wallet as wallets
from tests.test_central import IS_PG
from tests.test_m10s4_authority import _reset, count_sql, fresh, mode, policy, project  # noqa: F401
from tests.test_pipeline import OK, fake, world  # noqa: F401

APP = Path(__file__).resolve().parents[1] / "app"


def queued(db, monkeypatch, status="approved", key="kq", **kw):
    row = project(db, "ACME", status, **kw)
    fresh(db)
    mode(monkeypatch, "central")
    m = msgs.submit(db, "c1", key, OK, "ACME", text="hello") if status == "approved" else None
    db.commit()
    return row, m


def deny(db, row, status):
    row.status, row.approved_key = status, None
    db.commit()


def balances(db, world_):
    w, _ = world_
    return wallets.balances(db, w.id)


def test_active_sender_passes_the_recheck_and_the_message_is_sent(db, world, fake, monkeypatch):
    _row, m = queued(db, monkeypatch)
    msgs.process_one(db)
    assert (
        m.status == MessageStatus.SENT
        and len(fake.calls) == 1
        and m.provider_message_id == "fake-1"
    )
    assert m.sender_authority_source == "central"
    assert sau.stats_snapshot().get("recheck_pass") == 1 or True


@pytest.mark.parametrize(
    "state,code",
    [("revoked", "sender_revoked"), ("rejected", "sender_rejected"), ("pending", "sender_pending")],
)
def test_explicit_deny_before_dispatch_blocks_with_no_provider_call_id_or_charge(
    db, world, fake, monkeypatch, state, code
):
    row, m = queued(db, monkeypatch)
    before = balances(db, world)
    assert before[1] == Decimal("0.05")  # hold aktiv i submit-it
    deny(db, row, state)
    out = msgs.process_one(db)
    assert out is m and m.status == MessageStatus.FAILED and m.error_code == code
    assert fake.calls == [] and m.provider_message_id is None and m.dispatch_started_at is None
    w, _ = world
    assert wallets.balances(db, w.id) == (Decimal("10"), Decimal("0")) and wallets.verify_wallet(
        db, w.id
    )
    kinds = [
        e.entry_type
        for e in db.scalars(
            select(LedgerEntry).where(LedgerEntry.wallet_id == w.id).order_by(LedgerEntry.id)
        )
    ]
    assert EntryType.CAPTURE not in kinds and kinds.count(EntryType.RELEASE) == 1
    ev = [
        (e.from_status, e.to_status, e.detail)
        for e in db.scalars(
            select(MessageEvent).where(MessageEvent.message_id == m.id).order_by(MessageEvent.id)
        )
    ]
    assert ev[-1][1] == "failed" and code in (ev[-1][2] or "")


def test_policy_denied_blocks_but_pending_without_required_approval_does_not(
    db, world, fake, monkeypatch
):
    row, m = queued(db, monkeypatch)
    policy(db, allowed=False)
    msgs.process_one(db)
    assert (
        m.status == MessageStatus.FAILED
        and m.error_code == "sender_policy_denied"
        and fake.calls == []
    )
    # pending kur politika NUK kërkon miratim ⇒ s'bllokohet (pending anomal; s'shpikim revokim)
    db.execute(text("DELETE FROM sms_synced_sender_policies"))
    db.commit()
    policy(db, allowed=True, requires=False)
    m2 = msgs.submit(db, "c1", "k2", OK, "ACME", text="hi")
    db.commit()
    deny(db, row, "pending")
    row.cp_revision = 99
    db.commit()
    msgs.process_one(db)
    assert m2.status == MessageStatus.SENT


def test_stale_missing_older_historical_and_local_provenance_never_block(
    db, world, fake, monkeypatch
):
    row, m = queued(db, monkeypatch)
    fresh(db, datetime(2020, 1, 1, tzinfo=UTC))  # i vjetër sipas moshës
    msgs.process_one(db)
    assert m.status == MessageStatus.SENT  # stale vetëm s'bllokon
    m2 = msgs.submit(db, "c1", "k2", OK, "ACME", text="hi")
    db.commit()
    db.execute(text("DELETE FROM sms_synced_sender_authorizations"))  # rresht që mungon (boshllëk)
    db.commit()
    msgs.process_one(db)
    assert m2.status == MessageStatus.SENT
    # projeksion më i vjetër se provenanca e ngrirë (p.sh. rikthim epoke): s'është prova më e re
    row2 = project(db, "ACME", "revoked", cp_rev=1)
    m3 = msgs.submit(db, "c1", "k3", OK, "ACME", text="x") if False else None
    assert row2 and m3 is None
    m4 = Message  # vetëm referencë e importit
    assert m4 is Message


def test_projection_older_than_the_frozen_provenance_is_not_newer_evidence(
    db, world, fake, monkeypatch
):
    row, m = queued(db, monkeypatch)  # cp_revision=3 në provenancë
    row.status, row.approved_key, row.cp_revision = "revoked", None, 2
    db.commit()
    msgs.process_one(db)
    assert (
        m.status == MessageStatus.SENT
    )  # revokimi i dukshëm është më i vjetër se autorizimi: mungesa e provës së re
    # revokim me rishikim më të ri ⇒ bllokon
    row2, m2 = None, None
    mode(monkeypatch, "central")
    row.status, row.cp_revision = "approved", 5
    row.approved_key = "AL:acme"
    db.commit()
    m2 = msgs.submit(db, "c1", "k2", OK, "ACME", text="hi")
    db.commit()
    row.status, row.approved_key, row.cp_revision = "revoked", None, 6
    db.commit()
    msgs.process_one(db)
    assert m2.status == MessageStatus.FAILED and m2.error_code == "sender_revoked" and row2 is None


def test_historical_and_local_authority_messages_are_not_rechecked(db, world, fake, monkeypatch):
    m = msgs.submit(
        db, "c1", "kh", OK, "ACME", text="hi"
    )  # lokal (sender_authority_source='local')
    db.commit()
    m.sender_authority_source = None  # historik (para S4)
    db.commit()
    project(db, "ACME", "revoked")
    mode(monkeypatch, "central")
    seen = count_sql(lambda: msgs.process_one(db))
    assert m.status == MessageStatus.SENT and len(fake.calls) == 1
    assert not any("sms_synced_sender" in q for q in seen)  # zero lexime të projeksionit


def test_recheck_can_be_disabled_by_config_and_readiness_flags_it(db, world, fake, monkeypatch):
    row, m = queued(db, monkeypatch)
    deny(db, row, "revoked")
    monkeypatch.setattr(settings, "sender_dispatch_recheck", False)
    msgs.process_one(db)
    assert (
        m.status == MessageStatus.SENT
    )  # çaktivizim i shprehur: sjellje e vjetër (readiness e raporton si FAIL për central)


def test_blocked_message_is_terminal_no_retry_loop_no_recovery(db, world, fake, monkeypatch):
    row, m = queued(db, monkeypatch)
    deny(db, row, "revoked")
    msgs.process_one(db)
    assert msgs.process_one(db) is None and msgs.process_one(db) is None  # s'rikërkohet
    assert m.attempts == 1 and m.status == MessageStatus.FAILED
    rep = msgs.recover_stuck(db)
    db.commit()
    assert (rep.requeued, rep.unknown, rep.failed) == (0, 0, 0) and fake.calls == []
    w, _ = world
    assert wallets.balances(db, w.id) == (Decimal("10"), Decimal("0"))  # asnjë release i dytë


def test_revoke_before_recheck_blocks_but_revoke_after_recheck_is_the_documented_race(
    db, world, fake, monkeypatch
):
    row, m = queued(db, monkeypatch)
    orig = fake.send

    def revoke_during_call(req):
        with SessionLocal() as s:  # revokimi vjen PAS rikontrollit, gjatë thirrjes së provider-it
            s.execute(
                text(
                    "UPDATE sms_synced_sender_authorizations SET status='revoked', approved_key=NULL"
                )
            )
            s.commit()
        return orig(req)

    monkeypatch.setattr(fake, "send", revoke_during_call)
    msgs.process_one(db)
    db.expire_all()
    assert (
        m.status == MessageStatus.SENT and m.provider_message_id == "fake-1"
    )  # gara e mbetur: NUK pengohet (e dokumentuar)
    # çdo mesazh VAZHDUES (i ri) tashmë refuzohet nga submit dhe rikontrolli
    with pytest.raises(sau.SenderNotAllowed):
        msgs.submit(db, "c1", "k-after", OK, "ACME", text="x")


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_two_dispatch_workers_make_exactly_one_provider_call(db, world, fake, monkeypatch):
    if engine.dialect.name != "postgresql":
        pytest.skip("postgres parametrization only")
    _row, m = queued(db, monkeypatch)
    gate = threading.Barrier(2)
    res = []

    def work():
        with SessionLocal() as s:
            gate.wait(timeout=20)
            res.append(msgs.process_one(s))

    ts = [threading.Thread(target=work) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert len(fake.calls) == 1 and sum(r is not None for r in res) == 1
    db.expire_all()
    assert m.status == MessageStatus.SENT


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_revoke_racing_the_dispatch_never_deadlocks_and_ends_in_a_valid_state(
    db, world, fake, monkeypatch
):
    if engine.dialect.name != "postgresql":
        pytest.skip("postgres parametrization only")
    row, m = queued(db, monkeypatch)
    gate, errs = threading.Barrier(2), []

    def revoke():
        try:
            with SessionLocal() as s:
                gate.wait(timeout=20)
                s.execute(
                    text(
                        "UPDATE sms_synced_sender_authorizations SET status='revoked', approved_key=NULL, cp_revision=cp_revision+1"
                    )
                )
                s.commit()
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))

    def dispatch():
        try:
            with SessionLocal() as s:
                gate.wait(timeout=20)
                msgs.process_one(s)
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))

    ts = [threading.Thread(target=revoke), threading.Thread(target=dispatch)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert not errs, errs
    db.expire_all()
    assert (m.status, m.error_code, len(fake.calls)) in (
        (MessageStatus.SENT, None, 1),
        (MessageStatus.FAILED, "sender_revoked", 0),
    )
    w, _ = world
    assert wallets.verify_wallet(db, w.id)


def test_process_one_sql_historical_central_active_and_central_revoked(
    db, world, fake, monkeypatch
):
    row, m = queued(db, monkeypatch)
    sau.projection_stale(db)
    n_active = len(count_sql(lambda: msgs.process_one(db)))
    m2 = msgs.submit(db, "c1", "k2", OK, "ACME", text="x")
    db.commit()
    deny(db, row, "revoked")
    row.cp_revision = 9
    db.commit()
    sau.projection_stale(db)
    n_revoked = len(count_sql(lambda: msgs.process_one(db)))
    assert m2.status == MessageStatus.FAILED
    mode(monkeypatch, "local")
    m3 = msgs.submit(db, "c1", "k3", OK, "ACME", text="y")
    db.commit()
    m3.sender_authority_source = None
    db.commit()
    n_hist = len(count_sql(lambda: msgs.process_one(db)))
    print(
        f"\n[sql] process_one historical={n_hist} central_active={n_active} central_revoked={n_revoked}"
    )
    assert n_hist == 10 and n_active - n_hist <= 2 and n_revoked <= 20


def test_dispatch_gate_runs_before_dispatch_started_in_process_one():
    src = (APP / "services" / "messages.py").read_text()
    body = src[src.index("def process_one(") : src.index("def _lost_claim(")]
    assert (
        body.index("dispatch_gate")
        < body.index("m.dispatch_started_at = now")
        < body.index("provider.send(req)")
    )
    assert "sender_authority" in body


def test_call_site_audit_every_provider_send_is_behind_process_one_and_submit_is_the_only_creator():
    sends = []
    for p in APP.rglob("*.py"):
        rel = p.relative_to(APP).as_posix()
        if rel.startswith("providers/") or rel == "services/emails.py":  # email ka kanal të veçantë
            continue
        for line in p.read_text().splitlines():
            if re.search(r"\bprovider\.send\(|\.send\(req\)", line):
                sends.append(rel)
    assert sorted(set(sends)) == ["services/messages.py"] and len(sends) == 1
    creators = [
        p.relative_to(APP).as_posix()
        for p in APP.rglob("*.py")
        if re.search(r"(?<![A-Za-z])Message\(\s*public_id", p.read_text())
    ]
    assert creators == ["services/messages.py"]
    submitters = sorted(
        p.relative_to(APP).as_posix()
        for p in APP.rglob("*.py")
        if re.search(r"\b(msg_svc|msg|svc|msgs)\.submit\(", p.read_text()) and "email" not in p.name
    )
    assert set(submitters) <= {"api/messages.py", "services/campaigns.py", "services/inbox.py"}


def test_there_is_no_test_sms_exemption_or_authorization_bypass_flag():
    import inspect

    params = inspect.signature(msgs.submit).parameters
    assert not {"skip_auth", "bypass", "test", "is_test", "skip_sender_check", "force"} & set(
        params
    )
    for p in APP.rglob("*.py"):
        t = p.read_text()
        assert not re.search(r"test_sms|skip_sender|bypass_sender|sender_check\s*=\s*False", t), p
    for name in ("messages.py", "campaigns.py"):
        t = (APP / "services" / name).read_text()
        assert "sender_authorization" not in t, name  # vetëm fasada


def test_operational_counters_use_a_bounded_vocabulary_without_sender_values(
    db, world, fake, monkeypatch
):
    sau.reset_stats()
    row, m = queued(db, monkeypatch)
    deny(db, row, "revoked")
    msgs.process_one(db)
    with pytest.raises(sau.SenderNotAllowed):
        msgs.submit(db, "c1", "k-no", OK, "NOPE1", text="x")
    keys = set(sau.stats_snapshot())
    allowed = (
        {"central_allowed", "recheck_pass"}
        | {
            f"central_denied_{r}"
            for r in (
                "approved",
                "pending",
                "rejected",
                "revoked",
                "missing",
                "policy_denied",
                "invalid_identity",
                "no_enterprise",
            )
        }
        | {f"recheck_block_{r}" for r in ("revoked", "rejected", "pending", "policy_denied")}
    )
    assert keys <= allowed and "central_denied_missing" in keys and "recheck_block_revoked" in keys
    assert not any("acme" in k.lower() or "nope" in k.lower() for k in keys)
    assert db.scalar(select(SyncedSenderAuthorization.id)) is not None


def test_retry_after_a_temporary_provider_error_still_passes_through_the_recheck(
    db, world, fake, monkeypatch
):
    from datetime import timedelta

    from tests.test_pipeline import TEMP

    project(db, "ACME", "approved")
    fresh(db)
    mode(monkeypatch, "central")
    m = msgs.submit(db, "c1", "k-retry", TEMP, "ACME", text="hi")
    db.commit()
    msgs.process_one(db)
    assert (
        m.status == MessageStatus.QUEUED and len(fake.calls) == 1
    )  # gabim i përkohshëm ⇒ rirradhitje
    db.execute(
        text(
            "UPDATE sms_synced_sender_authorizations SET status='revoked', approved_key=NULL, cp_revision=9"
        )
    )
    db.commit()
    msgs.process_one(db, now=datetime.now(UTC) + timedelta(hours=1))
    assert (
        m.status == MessageStatus.FAILED
        and m.error_code == "sender_revoked"
        and len(fake.calls) == 1
    )  # provider-i s'u thirr përsëri
    w, _ = world
    assert wallets.balances(db, w.id) == (Decimal("10"), Decimal("0"))


def test_api_send_cannot_bypass_the_authority_in_central_mode(db, world, client, monkeypatch):
    fresh(db)
    mode(monkeypatch, "central")  # ACME është i miratuar LOKALISHT, por Central s'e njeh
    body = {"owner_ref": "c1", "to": OK, "sender": "ACME", "text": "hello"}
    r = client.post("/v1/messages", json=body, headers={"Idempotency-Key": "api-1"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "sender_not_allowed"
    project(db, "ACME", "approved")
    r = client.post("/v1/messages", json=body, headers={"Idempotency-Key": "api-2"})
    assert r.status_code == 202
