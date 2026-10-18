# ruff: noqa: F811
"""M10-S2 — Enterprise: projeksioni i sinkronizuar `cp.sender.v1`, aplikim atomik/idempotent, kursor i ndarë, snapshot/rikuperim, poller E2E kundër Central, gatishmëri, migrim 0029, hot path i pandryshuar."""

import ast
import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.models.messaging import SenderId
from app.models.sender_sync import SyncedSenderAuthorization, SyncedSenderPolicy
from app.services import control_plane_client as cc
from app.services import messages as msgs
from app.services import sender_ids as sid
from app.services import sender_sync as ss
from app.services import sender_sync_poller as sp
from app.services import sender_sync_readiness as sr
from apps.central.services import senders as csvc
from packages.contracts.control_plane.sender import v1
from tests.test_central import (  # noqa: F401
    IS_PG,
    ROOT,
    central_alembic,
    enterprise_alembic,
    make_db,
)
from tests.test_central_auth import auth_secret  # noqa: F401
from tests.test_m10s2_central_feed import A, mutate, req, senv  # noqa: F401
from tests.test_pipeline import OK, fake, world  # noqa: F401

NOW = datetime(2031, 1, 1, tzinfo=UTC)


def client_for(senv, name="snd", kid="k1", http=None):
    key = load_pem_private_key(senv.private.encode(), password=None)
    return cc.ControlPlaneClient(
        cc.ControlPlaneConfig("http://testserver", name, kid, key, 5.0),
        http=http or senv,
        scope=cc.SENDER_SCOPE,
    )


def poll(senv, **kw):
    return sp.poll_once(
        SessionLocal,
        kw.pop("client", None) or client_for(senv),
        snapshot_interval_s=kw.pop("snapshot_interval_s", 3600),
        **kw,
    )


def cursor(db):
    db.expire_all()
    return ss.get_cursor(db)


def rows(db):
    db.expire_all()
    return {r.display_value: r for r in db.scalars(select(SyncedSenderAuthorization))}


def pol(db, country="AL", kind="alphanumeric"):
    db.expire_all()
    return ss.get_synced_policy(db, country, kind)


def mkev(seq, etype, data, rev, gid, gsize, entity=None, ent=None):
    ent = None if etype == v1.EVENT_POLICY else (ent or "00000000-0000-0000-0000-000000000111")
    entity = entity or (
        v1.policy_entity_id(data.country, data.sender_kind)
        if etype == v1.EVENT_POLICY
        else "00000000-0000-0000-0000-0000000000e1"
    )
    return v1.SenderEventV1(str(uuid.uuid4()), seq, etype, ent, entity, rev, gid, gsize, NOW, data)


def pdata(allowed=True, req_=True, pid="00000000-0000-0000-0000-0000000000a1"):
    return v1.PolicyStateV1("AL", "alphanumeric", allowed, req_, pid, NOW)


def rdata(
    status="pending",
    decision="requested",
    display="Acme",
    did="00000000-0000-0000-0000-0000000000d1",
    ent="00000000-0000-0000-0000-000000000111",
):
    key = f"AL:{display.lower()}" if status == "approved" else None
    explicit = status != "pending"
    return v1.RegistryStateV1(ent, "ext", "AL", "alphanumeric", display, display.lower(), status, key, did, decision, NOW,
                              "explicit" if explicit else "default", "00000000-0000-0000-0000-0000000000a1" if explicit else None, 1 if explicit else None)  # fmt: skip


def init_cursor(db, last=0):
    cur = ss._lock_cursor(db)
    cur.epoch, cur.authorization_generation, cur.last_seq = uuid.UUID(int=7), 1, last
    db.commit()
    return cur.epoch


# =============================================================================================================
# E2E kundër Central
# =============================================================================================================


def test_initial_snapshot_then_incremental_poll_builds_the_projection_without_touching_local_senders(
    db, senv
):
    a = req(senv, "e1", "Acme")
    mutate(senv, lambda s, ad: csvc.approve(s, ad, a))
    b = req(senv, "e1", "Beta")
    mutate(senv, lambda s, ad: csvc.set_policy(s, ad, "XK", "numeric", True, False, "open"))
    local = sid.request(db, "c1", "AL", "ACME")  # objekt lokal: s'duhet të preket
    sid.approve(db, local.id, "staff")
    db.commit()
    before_local = (local.status, local.approved_key, local.current_decision_id)
    out = poll(senv)
    assert out.ok and out.snapshots == 1, out.detail
    r = rows(db)
    assert {k: v.status for k, v in r.items()} == {"Acme": "approved", "Beta": "pending"} and r[
        "Acme"
    ].approved_key == "AL:acme"
    assert pol(db, "XK", "numeric").requires_approval is False
    cur = cursor(db)
    assert (
        cur.epoch is not None
        and cur.last_seq == cur.latest_central_seq
        and cur.last_snapshot_at
        and cur.last_success_at
        and cur.snapshot_seq == cur.last_seq
    )
    # ndryshim në Central → poll inkremental (pa snapshot të ri)
    mutate(senv, lambda s, ad: csvc.approve(s, ad, b))
    out2 = poll(senv)
    assert out2.ok and out2.snapshots == 0 and out2.pages >= 1 and out2.applied == 1
    assert rows(db)["Beta"].status == "approved" and rows(db)["Beta"].cp_revision == 2
    db.expire_all()
    assert (
        local.status,
        local.approved_key,
        local.current_decision_id,
    ) == before_local  # SenderId lokal i paprekur
    assert db.scalar(select(func.count()).select_from(SenderId)) == 1


def test_policy_revocation_arrives_as_one_group_and_the_projection_never_mixes_states(db, senv):
    ids = [req(senv, "e1", f"Brand{i}") for i in range(3)]
    for i in ids:
        mutate(senv, lambda s, ad, i=i: csvc.approve(s, ad, i))
    assert poll(senv).ok
    assert all(r.status == "approved" for r in rows(db).values())
    mutate(senv, lambda s, ad: csvc.set_policy(s, ad, "AL", "alphanumeric", False, True, "ban"))
    seen = []
    orig = ss.apply_feed_batch

    def spy(db_, **kw):
        res = orig(db_, **kw)
        seen.append(len(kw["events"]))
        return res

    import app.services.sender_sync_poller as mod

    mod.ss.apply_feed_batch = spy
    try:
        assert poll(senv).ok
    finally:
        mod.ss.apply_feed_batch = orig
    assert seen == [4]  # grupi i plotë në një faqe/transaksion
    assert pol(db).allowed is False and all(
        r.status == "revoked" and r.approved_key is None for r in rows(db).values()
    )


def test_central_outage_leaves_the_projection_and_cursor_untouched_and_recovery_resumes(db, senv):
    a = req(senv, "e1", "Acme")
    mutate(senv, lambda s, ad: csvc.approve(s, ad, a))
    assert poll(senv).ok
    snap_before = {k: (v.status, v.cp_revision) for k, v in rows(db).items()}
    seq_before = cursor(db).last_seq

    def down(request):
        raise httpx.ConnectError("central down")

    bad = client_for(senv, http=httpx.Client(transport=httpx.MockTransport(down)))
    mutate(senv, lambda s, ad: csvc.revoke(s, ad, a, "abuse"))
    out = poll(senv, client=bad)
    assert out.kind == "network_error"
    assert {
        k: (v.status, v.cp_revision) for k, v in rows(db).items()
    } == snap_before  # asnjë miratim lokal i shpikur, asnjë mohim në masë
    cur = cursor(db)
    assert cur.last_seq == seq_before and cur.failure_count == 1 and cur.last_error
    assert poll(senv).ok and rows(db)["Acme"].status == "revoked" and cursor(db).last_error is None


def test_epoch_change_and_authorization_shrink_trigger_snapshots_and_withdraw_instead_of_delete(
    db, senv
):
    a, b = req(senv, "e1", "Alpha"), req(senv, "e2", "Bravo")
    for i in (a, b):
        mutate(senv, lambda s, ad, i=i: csvc.approve(s, ad, i))
    assert poll(senv).ok
    assert {r.projection_state for r in rows(db).values()} == {"active"}
    from apps.central.services import service_auth

    with Session(senv.eng) as s:
        service_auth.revoke_enterprise(s, "snd", senv.ids["e2"])
        s.commit()
    out = poll(senv)
    assert out.ok and out.snapshots == 1  # generation ndryshoi ⇒ snapshot
    r = rows(db)
    assert (
        r["Alpha"].projection_state == "active"
        and r["Bravo"].projection_state == "withdrawn"
        and r["Bravo"].approved_key is None
        and r["Bravo"].withdrawn_at
    )
    assert cursor(db).gap_recoveries == 1
    with Session(senv.eng) as s:  # epokë e re (restore/reseed)
        s.execute(text("UPDATE sender_sync_sequence SET epoch = :e"), {"e": str(uuid.uuid4())})
        s.commit()
    out = poll(senv)
    assert out.ok and out.snapshots == 1 and cursor(db).gap_recoveries == 2
    assert (
        rows(db)["Alpha"].status == "approved"
        and db.scalar(select(func.count()).select_from(SyncedSenderAuthorization)) == 2
    )


def test_incomplete_group_in_a_page_forces_snapshot_recovery(db, senv):
    ids = [req(senv, "e1", f"Grp{i}") for i in range(3)]
    for i in ids:
        mutate(senv, lambda s, ad, i=i: csvc.approve(s, ad, i))
    assert poll(senv).ok
    mutate(senv, lambda s, ad: csvc.set_policy(s, ad, "AL", "alphanumeric", False, True, "ban"))
    real = cc.ControlPlaneClient.get_sender_changes

    def lossy(self, after, epoch, gen, limit=200):
        page = real(self, after, epoch, gen, limit)
        if page.events:  # kap një ngjarje nga grupi ⇒ gjendje e përzier e mundshme
            object.__setattr__(page, "events", list(page.events)[:-1])
        return page

    cc.ControlPlaneClient.get_sender_changes = lossy
    try:
        out = poll(senv)
    finally:
        cc.ControlPlaneClient.get_sender_changes = real
    assert out.ok and out.snapshots == 1  # u zbulua, u rikuperua me snapshot
    assert cursor(db).gap_recoveries == 1
    assert pol(db).allowed is False and all(r.status == "revoked" for r in rows(db).values())


def test_periodic_snapshot_detects_and_repairs_drift_when_an_event_was_lost(db, senv):
    a = req(senv, "e1", "Acme")
    assert poll(senv).ok
    mutate(senv, lambda s, ad: csvc.approve(s, ad, a))
    real = cc.ControlPlaneClient.get_sender_changes

    def swallow(self, after, epoch, gen, limit=200):
        page = real(self, after, epoch, gen, limit)
        object.__setattr__(page, "events", [])  # ngjarja humbi, por kursori ecën deri te latest
        return page

    cc.ControlPlaneClient.get_sender_changes = swallow
    try:
        assert poll(senv).ok
    finally:
        cc.ControlPlaneClient.get_sender_changes = real
    assert (
        rows(db)["Acme"].status == "pending"
        and cursor(db).last_seq == cursor(db).latest_central_seq
    )
    out = poll(senv, snapshot_interval_s=0)  # snapshot rakordimi
    assert out.ok and rows(db)["Acme"].status == "approved"
    assert cursor(db).drift_repairs == 1


# =============================================================================================================
# aplikuesi: revision, atomicitet, kursor
# =============================================================================================================


def test_duplicate_stale_and_conflicting_revisions(db):
    init_cursor(db, 0)
    ep = uuid.UUID(int=7)
    g = str(uuid.uuid4())
    e1 = mkev(1, v1.EVENT_REGISTRY, rdata("approved", "approved"), 2, g, 1)
    r = ss.apply_feed_batch(
        db, epoch=ep, authorization_generation=1, events=[e1], next_seq=1, now=NOW
    )
    db.commit()
    assert (r.applied, r.noop, r.stale) == (1, 0, 0) and cursor(db).last_seq == 1
    same = mkev(2, v1.EVENT_REGISTRY, rdata("approved", "approved"), 2, str(uuid.uuid4()), 1)
    older = mkev(3, v1.EVENT_REGISTRY, rdata("pending", "requested"), 1, str(uuid.uuid4()), 1)
    r = ss.apply_feed_batch(
        db, epoch=ep, authorization_generation=1, events=[same, older], next_seq=3, now=NOW
    )
    db.commit()
    assert (
        (r.applied, r.noop, r.stale) == (0, 1, 1)
        and rows(db)["Acme"].status == "approved"
        and cursor(db).last_seq == 3
    )
    conflict = mkev(
        4,
        v1.EVENT_REGISTRY,
        rdata("approved", "approved", did="00000000-0000-0000-0000-0000000000d9"),
        2,
        str(uuid.uuid4()),
        1,
    )
    with pytest.raises(ss.EventConflict):
        ss.apply_feed_batch(
            db, epoch=ep, authorization_generation=1, events=[conflict], next_seq=4, now=NOW
        )
    db.rollback()
    assert cursor(db).last_seq == 3 and rows(db)["Acme"].decision_id == uuid.UUID(
        "00000000-0000-0000-0000-0000000000d1"
    )


def test_invalid_event_rolls_back_the_whole_page_and_the_cursor_does_not_advance(db):
    ep = init_cursor(db, 0)
    g1, g2 = str(uuid.uuid4()), str(uuid.uuid4())
    good = mkev(1, v1.EVENT_POLICY, pdata(False, True), 1, g1, 1)
    ok2 = mkev(2, v1.EVENT_REGISTRY, rdata("approved", "approved"), 2, g2, 1)
    bad = mkev(
        3,
        v1.EVENT_REGISTRY,
        rdata("approved", "approved", did="00000000-0000-0000-0000-0000000000d7"),
        2,
        str(uuid.uuid4()),
        1,
    )
    with pytest.raises(ss.EventConflict):
        ss.apply_feed_batch(
            db, epoch=ep, authorization_generation=1, events=[good, ok2, bad], next_seq=3, now=NOW
        )
    db.rollback()
    assert pol(db) is None and rows(db) == {} and cursor(db).last_seq == 0  # asgjë e pjesshme


def test_page_validation_rejects_seq_regression_gaps_beyond_next_seq_and_wrong_epoch_or_generation(
    db,
):
    ep = init_cursor(db, 5)
    mk = lambda seq: mkev(seq, v1.EVENT_POLICY, pdata(), seq, str(uuid.uuid4()), 1)  # noqa: E731
    for evs, nxt in (([mk(5)], 6), ([mk(6), mk(6)], 6), ([mk(7), mk(6)], 7), ([mk(9)], 8), ([], 4)):
        with pytest.raises(ss.ApplyError):
            ss.apply_feed_batch(
                db, epoch=ep, authorization_generation=1, events=evs, next_seq=nxt, now=NOW
            )
        db.rollback()
    with pytest.raises(ss.SnapshotRequired) as e:
        ss.apply_feed_batch(
            db, epoch=uuid.uuid4(), authorization_generation=1, events=[], next_seq=5, now=NOW
        )
    assert e.value.reason == "epoch_mismatch"
    db.rollback()
    with pytest.raises(ss.SnapshotRequired) as e:
        ss.apply_feed_batch(
            db, epoch=ep, authorization_generation=2, events=[], next_seq=5, now=NOW
        )
    assert e.value.reason == "authorization_changed"
    db.rollback()
    assert cursor(db).last_seq == 5
    with pytest.raises(ss.IncompleteGroup):
        g = str(uuid.uuid4())
        ss.apply_feed_batch(
            db,
            epoch=ep,
            authorization_generation=1,
            events=[mkev(6, v1.EVENT_POLICY, pdata(), 1, g, 2)],
            next_seq=6,
            now=NOW,
        )
    db.rollback()
    assert pol(db) is None


def test_empty_pages_advance_the_cursor_only_forward(db):
    ep = init_cursor(db, 3)
    ss.apply_feed_batch(
        db, epoch=ep, authorization_generation=1, events=[], next_seq=10, latest_seq=10, now=NOW
    )
    db.commit()
    c = cursor(db)
    assert c.last_seq == 10 and c.latest_central_seq == 10 and c.last_success_at is not None


def test_snapshot_withdraws_missing_rows_rejects_stale_snapshots_and_keeps_approved_keys_unique(db):
    ep = uuid.UUID(int=7)
    snap = lambda seq, senders, policies=(), gen=1, epoch=ep: ss.SnapshotV1(
        epoch, gen, seq, list(policies), list(senders)
    )  # noqa: E731
    item = lambda st, rev, ent=1: v1.SnapshotItemV1(
        v1.EVENT_REGISTRY, st.enterprise_id, f"00000000-0000-0000-0000-00000000e{ent:03d}", rev, st
    )  # noqa: E731
    a = item(rdata("approved", "approved", "Alpha"), 2, 1)
    b = item(rdata("pending", "requested", "Bravo"), 1, 2)
    r = ss.apply_snapshot(
        db,
        snap(
            10,
            [a, b],
            [
                v1.SnapshotItemV1(
                    v1.EVENT_POLICY, None, v1.policy_entity_id("AL", "alphanumeric"), 1, pdata()
                )
            ],
        ),
        now=NOW,
    )
    db.commit()
    assert r.applied == 3 and not r.reset and cursor(db).last_seq == 10
    with pytest.raises(ss.StaleSnapshot):
        ss.apply_snapshot(db, snap(9, [a]), now=NOW)
    db.rollback()
    # Bravo mungon ⇒ withdrawn; Alpha pezullohet dhe Bravo miratohet me çelës tjetër në të njëjtin snapshot (rend: jo-të-miratuarit para)
    a2 = item(rdata("revoked", "revoked", "Alpha"), 3, 1)
    b2 = item(rdata("approved", "approved", "Bravo"), 2, 2)
    ss.apply_snapshot(
        db,
        snap(
            11,
            [a2, b2],
            [
                v1.SnapshotItemV1(
                    v1.EVENT_POLICY, None, v1.policy_entity_id("AL", "alphanumeric"), 1, pdata()
                )
            ],
        ),
        now=NOW,
    )
    db.commit()
    assert {k: v.status for k, v in rows(db).items()} == {"Alpha": "revoked", "Bravo": "approved"}
    ss.apply_snapshot(db, snap(12, [a2]), now=NOW)
    db.commit()
    r = rows(db)
    assert (
        r["Bravo"].projection_state == "withdrawn"
        and r["Bravo"].approved_key is None
        and r["Alpha"].projection_state == "active"
    )
    # epokë e re: zëvendësim pa kontroll revision; rreshtat e vjetër largohen si withdrawn
    new = uuid.uuid4()
    ss.apply_snapshot(
        db, snap(2, [item(rdata("pending", "requested", "Alpha"), 1, 1)], epoch=new), now=NOW
    )
    db.commit()
    c = cursor(db)
    assert (
        c.epoch == new
        and c.last_seq == 2
        and rows(db)["Alpha"].status == "pending"
        and rows(db)["Alpha"].cp_revision == 1
    )


def test_snapshot_parse_is_strict(db):
    good = {
        "schema": v1.SCHEMA,
        "epoch": str(uuid.uuid4()),
        "authorization_generation": 1,
        "snapshot_seq": 0,
        "policies": [],
        "senders": [],
    }
    assert ss.parse_snapshot(good).snapshot_seq == 0
    for bad in (
        {**good, "extra": 1},
        {**good, "schema": "cp.sender.v2"},
        {**good, "epoch": "x"},
        {**good, "snapshot_seq": -1},
        {**good, "authorization_generation": 0},
        {**good, "policies": {}},
    ):
        with pytest.raises((v1.ContractError, v1.UnsupportedSchemaError)):
            ss.parse_snapshot(bad)


# =============================================================================================================
# lexime lokale
# =============================================================================================================


def test_local_read_helpers_resolve_policy_and_authorization_without_touching_submit(db, senv):
    a = req(senv, "e1", "Acme")
    mutate(senv, lambda s, ad: csvc.approve(s, ad, a))
    mutate(senv, lambda s, ad: csvc.set_policy(s, ad, "XK", "numeric", True, False, "open"))
    assert poll(senv).ok
    eid = senv.ids["e1"]
    for v in ("Acme", "ACME", "acme"):
        got = ss.get_synced_sender_authorization(db, eid, "al", v)
        assert (
            got.found
            and got.allowed
            and got.status == "approved"
            and got.canonical_key == "AL:acme"
        )
    assert not ss.get_synced_sender_authorization(db, eid, "XK", "Acme").found
    assert not ss.get_synced_sender_authorization(
        db, uuid.uuid4(), "AL", "Acme"
    ).found  # tenant tjetër
    d = ss.effective_synced_policy(db, "AL", "alphanumeric")
    assert (d.source, d.allowed, d.requires_approval, d.policy_revision) == (
        "default",
        True,
        True,
        None,
    )  # parazgjedhja virtuale
    x = ss.effective_synced_policy(db, "xk", "numeric")
    assert (x.source, x.allowed, x.requires_approval, x.policy_revision) == (
        "explicit",
        True,
        False,
        1,
    )
    row = db.scalar(select(SyncedSenderAuthorization))
    row.projection_state, row.approved_key = "withdrawn", None
    db.commit()
    assert not ss.get_synced_sender_authorization(
        db, eid, "AL", "Acme"
    ).found  # i tërhequr ⇒ i padukshëm


def test_submit_and_dispatch_do_not_depend_on_the_projection_and_sql_is_unchanged(db, world, fake):
    for mod in (msgs, sid):
        src = Path(mod.__file__).read_text()
        assert "sender_sync" not in src, mod.__name__  # asnjë lidhje me projeksionin
    import app.services.sender_authorization as sa_mod

    assert "sender_sync" not in Path(sa_mod.__file__).read_text()
    # projeksioni bosh/i prishur s'ndikon: submit kalon vetëm nga autorizimi lokal
    db.add(
        SyncedSenderPolicy(
            country="AL",
            sender_kind="alphanumeric",
            allowed=False,
            requires_approval=True,
            policy_id=uuid.uuid4(),
            policy_revision=1,
            effective_from=NOW,
            updated_at=NOW,
        )
    )
    db.commit()

    def count(fn):
        seen = []
        cb = lambda *a: seen.append(a[2])  # noqa: E731
        event.listen(engine, "before_cursor_execute", cb)
        try:
            fn()
        finally:
            event.remove(engine, "before_cursor_execute", cb)
        return len(seen)

    assert count(lambda: msgs.submit(db, "c1", "k-s2", OK, "ACME", text="hello")) == 20
    db.commit()
    assert count(lambda: msgs.process_one(db)) == 10


def test_worker_role_is_idle_without_flag_and_misconfiguration_is_reported(monkeypatch):
    import app.worker as worker

    monkeypatch.setattr(settings, "sender_sync_enabled", True)
    monkeypatch.setattr(settings, "cp_base_url", "")
    assert worker.run_sender_control_plane(once=True) == 2
    assert "sender_control_plane" in Path(worker.__file__).read_text()
    src = ast.parse(Path(msgs.__file__).read_text())
    assert not any(
        isinstance(n, ast.ImportFrom) and (n.module or "").endswith("sender_sync")
        for n in ast.walk(src)
    )


def test_production_requires_https_for_sender_sync(monkeypatch):
    from app.core.config import Settings

    s = Settings(env="production", sender_sync_enabled=True, cp_base_url="http://central")
    assert any("SMS_SENDER_SYNC_ENABLED" in p for p in s.production_problems())


# =============================================================================================================
# gatishmëria
# =============================================================================================================


def test_readiness_is_healthy_after_a_sync_and_detects_stale_invalid_and_inconsistent_state(
    db, senv, monkeypatch
):
    a = req(senv, "e1", "Acme")
    mutate(senv, lambda s, ad: csvc.approve(s, ad, a))
    monkeypatch.setattr(settings, "sender_sync_enabled", True)
    assert {c.name for c in sr.checks(db, NOW) if c.level == "FAIL"} == {
        "generation_known",
        "snapshot_known",
        "sync_fresh",
    }  # pa snapshot: FAIL i pritur
    db.commit()  # get_cursor krijon singleton-in pa migrim (create_all): mos mbaj kyç gjatë poll-it
    assert poll(senv).ok
    now = datetime.now(UTC)
    items = {c.name: c for c in sr.checks(db, now)}
    assert sr.overall(list(items.values())) == "PASS", {
        k: v.reason for k, v in items.items() if v.level != "PASS"
    }
    st = sr.status(db, now)
    assert (
        st["authorization_count"] == 1
        and st["lag"] == 0
        and st["initialized"]
        and "Acme" not in json.dumps(st)
    )
    stale = datetime(2099, 1, 1, tzinfo=UTC)
    assert {c.name: c for c in sr.checks(db, stale)}[
        "sync_fresh"
    ].level == "FAIL"  # vetëm raportim: asnjë skadim miratimesh
    assert rows(db)["Acme"].status == "approved"
    cur = ss._lock_cursor(db)
    cur.last_error, cur.last_error_at = "boom", datetime(2099, 1, 1, tzinfo=UTC)
    cur.epoch = None
    db.commit()
    bad = {c.name: c for c in sr.checks(db, now)}
    assert bad["cursor_valid"].level == "FAIL" and bad["no_unresolved_error"].level == "WARN"
    cur = ss._lock_cursor(db)
    cur.epoch = uuid.UUID(int=1)
    cur.last_error = cur.last_error_at = None
    db.commit()
    row = db.scalar(select(SyncedSenderAuthorization))
    row.approved_key = None  # i miratuar pa çelës ⇒ inkonsistent
    db.commit()
    assert {c.name: c for c in sr.checks(db, now)}["projection_consistent"].level == "FAIL"
    row.approved_key = "AL:acme"
    db.add(
        SyncedSenderPolicy(
            country="AL",
            sender_kind="alphanumeric",
            allowed=False,
            requires_approval=True,
            policy_id=uuid.uuid4(),
            policy_revision=2,
            effective_from=NOW,
            updated_at=NOW,
        )
    )
    db.commit()
    assert {c.name: c for c in sr.checks(db, now)}[
        "policy_registry_coherent"
    ].level == "FAIL"  # gjendje e përzier


def test_readiness_cli_json_has_metrics_and_no_sender_values(db, senv, capsys):
    from scripts import sender_sync_readiness as cli

    a = req(senv, "e1", "SecretBrand")
    mutate(senv, lambda s, ad: csvc.approve(s, ad, a))
    assert poll(senv).ok
    assert cli.main(["--json"]) in (0, 1)
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert (
        {"status", "checks", "metrics"} <= set(doc)
        and "SecretBrand" not in out
        and "secretbrand" not in out.lower()
    )
    assert doc["metrics"]["authorization_count"] == 1 and doc["metrics"]["policy_count"] == 0


# =============================================================================================================
# PostgreSQL: konkurrencë
# =============================================================================================================


@pytest.mark.skipif(not IS_PG, reason="postgres concurrency")
def test_two_pollers_snapshot_vs_incremental_and_same_page_races_are_serialized_by_the_cursor(
    db, senv
):
    if engine.dialect.name != "postgresql":
        pytest.skip("postgres parametrization only")
    ids = [req(senv, "e1", f"Race{i}") for i in range(6)]
    for i in ids:
        mutate(senv, lambda s, ad, i=i: csvc.approve(s, ad, i))
    outs, errs = [], []
    gate = threading.Barrier(3)

    def run(kind):
        try:
            gate.wait(timeout=20)
            if kind == "snap":
                outs.append(poll(senv, snapshot_interval_s=0))
            else:
                outs.append(poll(senv))
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=run, args=(k,)) for k in ("snap", "inc", "inc")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, errs
    assert all(o.ok for o in outs), [o.detail for o in outs]
    r = rows(db)
    assert len(r) == 6 and all(v.status == "approved" for v in r.values())
    assert len({v.approved_key for v in r.values()}) == 6
    c = cursor(db)
    assert c.last_seq == c.latest_central_seq and not [
        x
        for x in sr.checks(db, datetime.now(UTC))
        if x.name == "projection_consistent" and x.level != "PASS"
    ]
    # aplikimi i të njëjtës faqe njëkohësisht: një fiton, tjetri refuzohet pastër (seq jashtë intervalit), asnjë gabim DB
    ep = c.epoch
    last = c.last_seq
    ev = mkev(
        last + 1, v1.EVENT_POLICY, pdata(False, True, str(uuid.uuid4())), 50, str(uuid.uuid4()), 1
    )
    res = []

    def same_page():
        with SessionLocal() as s:
            try:
                ss.apply_feed_batch(
                    s,
                    epoch=ep,
                    authorization_generation=c.authorization_generation,
                    events=[ev],
                    next_seq=last + 1,
                    now=NOW,
                )
                s.commit()
                res.append("ok")
            except ss.ApplyError:
                s.rollback()
                res.append("rejected")
            except Exception as e:  # noqa: BLE001
                s.rollback()
                res.append(repr(e))

    ts = [threading.Thread(target=same_page) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(res) == ["ok", "rejected"], res


# =============================================================================================================
# migrimi 0029
# =============================================================================================================

NEW = {"sms_synced_sender_policies", "sms_synced_sender_authorizations", "sms_sender_sync_cursor"}


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
        if any(t in repr(i) for t in NEW)
    ]


def test_enterprise_0029_is_additive_reversible_and_matches_metadata(make_db):
    url = make_db("ent")
    enterprise_alembic(url, "upgrade", "0028")
    eng = create_engine(url)
    before = {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    }
    assert not NEW & set(before)
    enterprise_alembic(url, "upgrade", "0029")
    assert NEW <= set(inspect(eng).get_table_names())
    for t, cols in before.items():
        assert {
            c["name"] for c in inspect(eng).get_columns(t)
        } == cols  # asnjë tabelë ekzistuese s'ndryshon
    with eng.connect() as c:
        assert _drift(c) == []
        assert c.execute(text("SELECT count(*) FROM sms_sender_sync_cursor")).scalar() == 1
    enterprise_alembic(url, "downgrade", "0028")
    assert not NEW & set(inspect(eng).get_table_names())
    enterprise_alembic(url, "upgrade", "head")
    with eng.connect() as c:
        assert _drift(c) == []
    eng.dispose()


def test_central_0027_is_additive_reversible_and_matches_metadata(make_db):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from apps.central.core.db import Base

    new = {"sender_sync_sequence", "sender_sync_outbox"}
    url = make_db("central")
    central_alembic(url, "upgrade", "0026")
    eng = create_engine(url)
    before = {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    }
    assert not new & set(before)
    central_alembic(url, "upgrade", "0027")
    for t, cols in before.items():
        assert {c["name"] for c in inspect(eng).get_columns(t)} == cols
    with eng.connect() as c:
        ctx = MigrationContext.configure(
            c, opts={"compare_type": True, "version_table": "central_alembic_version"}
        )
        assert [
            repr(i)
            for d in compare_metadata(ctx, Base.metadata)
            for i in (d if isinstance(d, list) else [d])
            if any(t in repr(i) for t in new)
        ] == []
        assert c.execute(text("SELECT last_seq, floor_seq FROM sender_sync_sequence")).one() == (
            0,
            0,
        )
    central_alembic(url, "downgrade", "0026")
    assert not new & set(inspect(eng).get_table_names())
    central_alembic(url, "upgrade", "head")
    assert new <= set(inspect(eng).get_table_names())
    eng.dispose()


def test_sync_never_references_or_mutates_the_local_sender_tables():
    tree = ast.parse((ROOT / "app" / "services" / "sender_sync.py").read_text())
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert "app.models.messaging" not in imported and "app.services.sender_ids" not in imported
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"SenderId", "SenderDecision", "sender_ids"}
