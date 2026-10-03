# ruff: noqa: F811
"""M7-e: orkestrimi (snapshot/feed/rakordim), shadow, singleton, integrim me Central real."""

import logging
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, engine
from app.models.admin import AuditLog
from app.models.control_plane import Entitlement
from app.models.enterprise import Enterprise
from app.models.sending import AccountPlan
from app.services import control_plane_client as cc
from app.services import control_plane_poller as poller
from app.services import control_plane_shadow as shadow
from app.services import control_plane_sync as cps
from app.services import messages as svc
from apps.central.services import enterprise_products as asg
from apps.central.services import enterprises as ent
from apps.central.services import service_auth, sync_feed
from tests.test_central import IS_PG, make_db  # noqa: F401
from tests.test_central_sync_api import env, mutate  # noqa: F401  (fixtures)
from tests.test_pipeline import OK, fake, send, world  # noqa: F401  (fixtures)

T = datetime(2030, 1, 1, tzinfo=UTC)


class Spy(cc.ControlPlaneClient):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.calls: list[str] = []

    def get_snapshot(self, enterprise_id=None):
        self.calls.append("snapshot")
        return super().get_snapshot(enterprise_id)

    def get_changes(self, *a, **k):
        self.calls.append("changes")
        return super().get_changes(*a, **k)


@pytest.fixture
def cp(env, db):  # noqa: F811
    """Central real (TestClient) + DB Enterprise (conftest). Rreshtat lokalë ndajnë UUID me Central."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    key = load_pem_private_key(env.private.encode(), password=None)
    cfg = cc.ControlPlaneConfig("http://testserver", "ent-main", "k1", key, 5.0)
    client = Spy(cfg, http=env)
    for name in ("e1", "e2"):
        db.add(Enterprise(id=env.ids[name], owner_ref=f"o-{name}", legal_name=f"Legal {name}"))
    db.commit()
    env.client = client
    return env


def poll(cp, now=T, interval=10**9):
    return poller.poll_once(SessionLocal, cp.client, snapshot_interval_s=interval, now=now)


def cursor(db):
    c = cps.get_cursor(db)  # populate_existing
    db.commit()
    return c


def assign(cp, who="e1", prod="sms"):
    return mutate(cp, lambda s: asg.assign_product(s, cp.ids[who], cp.ids[prod])[0].id)


def ents(db):
    return {(e.enterprise_id, e.channel): e for e in db.scalars(select(Entitlement))}


# --- bootstrap / feed ------------------------------------------------------------------------


def test_initial_cursor_null_does_snapshot_first_then_feed(cp, db):
    assert cursor(db).epoch is None
    out = poll(cp)
    assert out.ok and out.snapshots == 1
    assert cp.client.calls[:2] == ["snapshot", "changes"]  # kurrë feed-first
    c = cursor(db)
    assert c.epoch is not None and c.authorization_generation >= 1 and c.last_success_at is not None


def test_normal_polling_applies_central_changes_and_advances_cursor(cp, db):
    poll(cp)
    seq0 = cursor(db).last_seq
    mutate(cp, lambda s: ent.rename(s, cp.ids["e1"], "Renamed"))
    cp.client.calls.clear()
    out = poll(cp)
    assert out.ok and out.snapshots == 0 and out.applied == 1 and cp.client.calls == ["changes"]
    e1 = db.get(Enterprise, cp.ids["e1"])
    db.refresh(e1)
    assert e1.short_name == "Renamed" and e1.legal_name == "Legal e1" and e1.cp_revision == 2
    assert cursor(db).last_seq > seq0


def test_empty_feed_and_filtered_gaps_advance_with_response_next_seq(cp, db):
    poll(cp)
    s0 = cursor(db).last_seq
    mutate(cp, lambda s: ent.rename(s, cp.ids["e3"], "Unseen"))  # e3: jo i autorizuar ⇒ filtruar
    out = poll(cp)
    assert out.ok and out.applied == 0
    assert cursor(db).last_seq > s0  # next_seq i mbështjellësit, jo "seq i fundit i ngjarjeve"
    mutate(cp, lambda s: ent.rename(s, cp.ids["e1"], "A"))
    mutate(cp, lambda s: ent.rename(s, cp.ids["e3"], "Unseen2"))
    mutate(cp, lambda s: ent.rename(s, cp.ids["e1"], "B"))
    out = poll(cp)
    assert out.applied == 2  # boshllëk seq-esh i pranuar
    e1 = db.get(Enterprise, cp.ids["e1"])
    db.refresh(e1)
    assert e1.short_name == "B"
    assert cursor(db).last_seq == state_latest(cp)


def state_latest(cp):
    with Session(cp.eng) as s:
        return sync_feed.read_state(s)[2]


def test_authorization_generation_409_triggers_a_full_snapshot(cp, db):
    poll(cp)
    g0 = cursor(db).authorization_generation
    db.add(Enterprise(id=cp.ids["e3"], owner_ref="o-e3"))
    db.commit()
    mutate(cp, lambda s: service_auth.grant_enterprise(s, "ent-main", cp.ids["e3"]))
    cp.client.calls.clear()
    out = poll(cp)
    assert out.ok and out.snapshots == 1 and cp.client.calls == ["changes", "snapshot", "changes"]
    assert cursor(db).authorization_generation == g0 + 1
    e3 = db.get(Enterprise, cp.ids["e3"])
    db.refresh(e3)
    assert e3.cp_revision >= 1  # historia e enterprise-it të ri u sjell nga snapshot-i


def test_epoch_mismatch_409_triggers_snapshot_without_blind_reset(cp, db):
    poll(cp)
    old = cursor(db).epoch
    mutate(cp, sync_feed.rotate_epoch)
    cp.client.calls.clear()
    out = poll(cp)
    assert out.ok and out.snapshots == 1 and cp.client.calls[0] == "changes"
    assert cursor(db).epoch != old
    assert "snapshot" in cp.client.calls


def test_cursor_expired_410_triggers_snapshot_not_partial_feed(cp, db):
    poll(cp)
    mutate(cp, lambda s: ent.rename(s, cp.ids["e1"], "X"))
    with cp.eng.begin() as c:
        c.execute(text("update sync_sequence set floor_seq = :f"), {"f": state_latest(cp)})
    cp.client.calls.clear()
    out = poll(cp)
    assert out.ok and out.snapshots == 1 and cp.client.calls[:2] == ["changes", "snapshot"]
    e1 = db.get(Enterprise, cp.ids["e1"])
    db.refresh(e1)
    assert e1.short_name == "X"


# --- rakordim periodik / enterprise i panjohur / fusha e autorizimit ----------------------------


def test_periodic_full_snapshot_happens_when_due_and_not_before(cp, db):
    poll(cp)
    cp.client.calls.clear()
    assert poll(cp, now=T + timedelta(seconds=100), interval=3600).snapshots == 0
    assert "snapshot" not in cp.client.calls
    cp.client.calls.clear()
    out = poll(cp, now=T + timedelta(seconds=3601), interval=3600)
    assert out.snapshots == 1 and cp.client.calls[0] == "snapshot"
    assert cursor(db).last_snapshot_at is not None


def test_skipped_unknown_enterprise_event_is_reconciled_by_periodic_snapshot(cp, db):
    db.delete(db.get(Enterprise, cp.ids["e2"]))  # e2 ekziston në Central, jo lokalisht
    db.commit()
    poll(cp)
    sid = assign(cp, "e2", "sms")
    mutate(cp, lambda s: ent.rename(s, cp.ids["e2"], "Beta2"))
    out = poll(cp)
    assert out.skipped_unknown_enterprise == 2 and out.ok  # kursori përparon
    assert cursor(db).last_seq == state_latest(cp)
    # krijohet lokalisht më vonë; feed-i s'e rikthen kurrë ngjarjen
    db.add(Enterprise(id=cp.ids["e2"], owner_ref="o-late", legal_name="Late"))
    db.commit()
    out = poll(cp)
    assert out.applied == 0 and not [k for k in ents(db) if k[0] == cp.ids["e2"]]
    out = poll(cp, now=T + timedelta(hours=2), interval=3600)  # rakordimi periodik e popullon
    assert out.snapshots == 1
    e2 = db.get(Enterprise, cp.ids["e2"])
    db.refresh(e2)
    assert e2.short_name == "Beta2" and e2.legal_name == "Late" and e2.owner_ref == "o-late"
    assert ents(db)[(cp.ids["e2"], "sms")].assignment_id == sid


def test_partial_snapshot_does_not_infer_removed_tenant_nor_move_the_cursor(cp, db):
    assign(cp, "e1", "sms")
    assign(cp, "e2", "email")
    poll(cp)
    before = cursor(db).last_seq
    partial = cc.ControlPlaneClient.get_snapshot(cp.client, cp.ids["e1"])
    assert {e["enterprise_id"] for e in partial["enterprises"]} == {str(cp.ids["e1"])}
    r = cps.apply_snapshot(db, cps.parse_snapshot(partial), full_scope=False)
    db.commit()
    assert r.entitlements_out_of_scope == 0 and not r.full_scope
    assert ents(db)[(cp.ids["e2"], "email")].status == "active"  # i paprekur
    assert cursor(db).last_seq == before


def test_full_snapshot_scope_shrink_withdraws_but_never_deletes(cp, db):
    assign(cp, "e1", "sms")
    assign(cp, "e2", "email")
    poll(cp)
    n_ent, n_entitlements = db.scalar(select(func.count()).select_from(Enterprise)), len(ents(db))
    e2 = db.get(Enterprise, cp.ids["e2"])
    rev, g0 = e2.cp_revision, cursor(db).authorization_generation
    mutate(cp, lambda s: service_auth.revoke_enterprise(s, "ent-main", cp.ids["e2"]))  # gen 1 → 2
    out = poll(cp)
    assert out.ok and out.snapshots == 1
    db.expire_all()
    e = ents(db)
    assert e[(cp.ids["e2"], "email")].status == "withdrawn"  # s'është më autoritet i Central
    assert e[(cp.ids["e1"], "sms")].status == "active"
    assert db.scalar(select(func.count()).select_from(Enterprise)) == n_ent  # tenant s'fshihet
    assert len(ents(db)) == n_entitlements  # historiku s'fshihet
    assert db.get(Enterprise, cp.ids["e2"]).cp_revision == rev  # gjendja e fundit e njohur
    assert cursor(db).authorization_generation == g0 + 1
    reasons = [
        a.detail
        for a in db.scalars(select(AuditLog))
        if "out_of_authorization_scope" in (a.detail or "")
    ]
    assert len(reasons) == 1
    # rikthimi në fushë: entitlement-i rikthehet
    mutate(cp, lambda s: service_auth.grant_enterprise(s, "ent-main", cp.ids["e2"]))
    poll(cp)
    db.expire_all()
    assert ents(db)[(cp.ids["e2"], "email")].status == "active"


def test_never_managed_enterprise_is_untouched_by_full_snapshot(cp, db):
    poll(cp)
    db.add(Enterprise(id=uuid.uuid4(), owner_ref="legacy-only"))
    db.commit()
    out = poll(cp, now=T + timedelta(hours=2), interval=3600)
    assert out.ok and db.scalar(select(func.count()).select_from(Enterprise)) == 3


def test_repeated_poll_is_idempotent_no_extra_audit_rows(cp, db):
    assign(cp, "e1", "sms")
    poll(cp)
    n = db.scalar(select(func.count()).select_from(AuditLog))
    out = poll(cp)
    assert out.ok and out.applied == 0
    assert db.scalar(select(func.count()).select_from(AuditLog)) == n


# --- integrim: Central real + Enterprise DB + outage ---------------------------------------------


def test_end_to_end_snapshot_mutation_feed_entitlement_cursor_and_outage(cp, db):
    out = poll(cp)
    assert out.ok and ents(db) == {}  # asnjë assignment ende
    sid = assign(cp, "e1", "sms")
    s1 = cursor(db).last_success_at
    out = poll(cp, now=T + timedelta(seconds=30))
    assert out.ok and out.applied == 1
    row = ents(db)[(cp.ids["e1"], "sms")]
    assert (row.assignment_id, row.status, row.product_code, row.channel) == (
        sid,
        "active",
        "sms",
        "sms",
    )
    assert cursor(db).last_seq == state_latest(cp) and cursor(db).last_success_at > s1
    mutate(cp, lambda s: asg.suspend_assignment(s, cp.ids["e1"], sid))
    poll(cp, now=T + timedelta(seconds=60))
    db.expire_all()
    assert ents(db)[(cp.ids["e1"], "sms")].status == "suspended"
    # Central ploqet (rrjeti): gjendja lokale mbetet, last_success_at s'lëviz, asgjë s'çaktivizohet
    before = (
        cursor(db).last_seq,
        cursor(db).last_success_at,
        ents(db)[(cp.ids["e1"], "sms")].status,
    )
    cp.client._http = __import__("httpx").Client(
        transport=__import__("httpx").MockTransport(
            lambda r: (_ for _ in ()).throw(__import__("httpx").ConnectError("down", request=r))
        )  # fmt: skip
    )
    out = poll(cp, now=T + timedelta(seconds=90))
    db.expire_all()
    assert out.kind == "network_error"
    assert before == (
        cursor(db).last_seq,
        cursor(db).last_success_at,
        ents(db)[(cp.ids["e1"], "sms")].status,
    )
    assert db.get(Enterprise, cp.ids["e1"]).status == "active"  # fail-static


def test_accountplan_is_never_mutated_by_polling(cp, db, world):  # noqa: F811
    plan = db.scalar(select(AccountPlan))
    cols = {c.name: getattr(plan, c.name) for c in AccountPlan.__table__.columns}
    e = db.get(Enterprise, plan.enterprise_id)
    cp.ids["e1"] = e.id  # lidh enterprise-in e AccountPlan me Central
    with Session(cp.eng, expire_on_commit=False) as s:
        from apps.central.models import Enterprise as CEnt

        s.add(
            CEnt(id=e.id, name="Plan Owner", status="suspended")
        )  # pa outbox: snapshot lexon tabelat
        s.commit()
    mutate(cp, lambda s: service_auth.grant_enterprise(s, "ent-main", e.id))
    poll(cp)
    poll(cp, now=T + timedelta(hours=2), interval=3600)
    db.expire_all()
    plan2 = db.scalar(select(AccountPlan))
    assert {c.name: getattr(plan2, c.name) for c in AccountPlan.__table__.columns} == cols
    assert db.get(Enterprise, e.id).status == "suspended" and plan2.enabled is True  # CP ≠ legacy


# --- shadow ------------------------------------------------------------------------------------


@pytest.fixture
def shadow_on(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "cp_sync_mode", "shadow")
    monkeypatch.setattr(shadow, "CACHE_TTL_S", 0.0)
    shadow.stats.reset()
    shadow.clear_cache()
    yield shadow.stats
    shadow.clear_cache()


def put_cp(db, eid, *, ent_status="active", sms=None, email=None, last_ok=None, rev=3):
    e = db.get(Enterprise, eid)
    e.status, e.cp_revision = ent_status, rev
    for ch, st in (("sms", sms), ("email", email)):
        if st:
            db.add(Entitlement(enterprise_id=eid, assignment_id=uuid.uuid4(), product_id=uuid.uuid4(),
                               product_code=f"{ch}_std", channel=ch, status=st, revision=1))  # fmt: skip
    cur = cps.get_cursor(db)
    cur.epoch, cur.authorization_generation = uuid.uuid4(), 1
    cur.last_success_at = last_ok or datetime.now(UTC)
    db.commit()


def eid_of(db):
    return db.scalar(select(AccountPlan.enterprise_id))


def test_shadow_classification_matrix_and_channel_independence(db, world, shadow_on):  # noqa: F811
    eid = eid_of(db)
    put_cp(db, eid, sms="suspended", email="active")
    st = shadow.stats
    send(db, "k-allow-deny")  # legacy allow (plan.enabled) vs CP sms suspended
    assert st.snapshot()["legacy_allow_cp_deny"] == 1
    # e-mail është i pavarur nga SMS
    assert shadow.classify(db, eid, "email", True) == "legacy_allow_cp_allow"
    assert shadow.classify(db, eid, "sms", True) == "legacy_allow_cp_deny"
    plan = db.scalar(select(AccountPlan))
    plan.enabled = False  # legacy deny
    db.commit()
    assert shadow.classify(db, eid, "email", False) == "legacy_deny_cp_allow"
    assert shadow.classify(db, eid, "sms", False) == "legacy_deny_cp_deny"


def test_shadow_missing_withdrawn_and_stale(db, world, shadow_on):  # noqa: F811
    eid = eid_of(db)
    assert shadow.classify(db, eid, "sms", True) == "cp_missing"  # cp_revision = 0
    put_cp(db, eid, sms="withdrawn")
    assert shadow.classify(db, eid, "sms", True) == "cp_withdrawn"
    assert shadow.classify(db, eid, "email", True) == "cp_missing"  # asnjë entitlement email
    cur = cps.get_cursor(db)
    cur.last_success_at = datetime.now(UTC) - timedelta(seconds=cps.SLO_AGE_S + 60)
    db.add(Entitlement(enterprise_id=eid, assignment_id=uuid.uuid4(), product_id=uuid.uuid4(),
                       product_code="email_std", channel="email", status="active", revision=1))  # fmt: skip
    db.commit()
    assert shadow.classify(db, eid, "email", True) == "cp_stale"
    assert shadow.classify(db, None, "sms", True) == "cp_missing"


def test_shadow_never_changes_submit_outcome_even_on_total_cp_deny_or_errors(
    db, world, shadow_on, monkeypatch
):  # noqa: F811
    eid = eid_of(db)
    put_cp(db, eid, ent_status="suspended", sms="suspended")
    m = send(db, "k1")  # legacy allow, CP deny: submit kalon
    assert m.status.name == "QUEUED" and shadow.stats.snapshot()["legacy_allow_cp_deny"] == 1
    monkeypatch.setattr(
        shadow, "classify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    assert send(db, "k2").status.name == "QUEUED"  # gabim në shadow: i përlahur
    db.scalar(select(AccountPlan)).enabled = False
    db.commit()
    monkeypatch.undo()
    with pytest.raises(svc.AccountDisabled):  # legacy deny mbetet deny, pavarësisht CP
        svc.submit(db, "c1", "k3", OK, "ACME", text="hi")


def test_shadow_off_is_a_noop_and_does_not_query(db, world):  # noqa: F811
    shadow.stats.reset()
    shadow.clear_cache()
    n = []

    def count(*a):
        n.append(1)

    event.listen(engine, "before_cursor_execute", count)
    try:
        shadow.observe(db, None, type("O", (), {"enterprise_id": uuid.uuid4()})(), "sms", True)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert n == [] and sum(shadow.stats.snapshot().values()) == 0


def test_shadow_logs_are_rate_limited_not_per_request(db, world, shadow_on, caplog):  # noqa: F811
    eid = eid_of(db)
    put_cp(db, eid, sms="suspended")
    caplog.set_level(logging.WARNING, logger="sms.cp.shadow")
    for _ in range(25):
        shadow.observe(db, None, type("O", (), {"enterprise_id": eid})(), "sms", True)
    assert shadow.stats.snapshot()["legacy_allow_cp_deny"] == 25
    assert caplog.text.count("shadow mismatch") == 1


def test_shadow_hot_path_overhead_sql_and_latency(db, world, monkeypatch):
    """Raunde të ndërthurura me rrotullim (numëruesi i minutës rritet me çdo submit ⇒ mode i fundit
    do dukej më i ngadaltë); minimumi për mode."""
    from app.core.config import settings
    from app.models.wallet import Wallet
    from app.services import wallet as wallets

    w = db.scalar(select(Wallet))
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "10000", wallets.TopupMethod.CASH).id)
    eid = eid_of(db)
    put_cp(db, eid, sms="active")
    counts = {"n": 0}

    def count(*a):
        counts["n"] += 1

    event.listen(engine, "before_cursor_execute", count)
    best, sql = {}, {}
    try:
        for r in range(6):
            order = ("off", "shadow")
            for mode in order[r % 2 :] + order[: r % 2]:
                monkeypatch.setattr(settings, "cp_sync_mode", mode)
                if r < 2:
                    shadow.clear_cache()
                counts["n"] = 0
                t = time.perf_counter()
                for i in range(25):
                    send(db, f"{mode}-{r}-{i}")
                ms = (time.perf_counter() - t) / 25 * 1000
                best[mode] = min(best.get(mode, 1e9), ms)
                sql[mode] = counts["n"] / 25
    finally:
        event.remove(engine, "before_cursor_execute", count)
    print(
        f"\n[perf] submit off: {sql['off']:.1f} sql/{best['off']:.2f} ms · shadow: {sql['shadow']:.1f} sql/{best['shadow']:.2f} ms"
    )
    assert sql["shadow"] - sql["off"] <= 0.5  # cache: ≈0 SQL shtesë në gjendje të qëndrueshme
    assert best["shadow"] <= best["off"] * 1.05 + 1.0  # ≤5% (+1 ms zhurmë të makinës/PG)


# --- singleton (PostgreSQL) ----------------------------------------------------------------------


pg_only = pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")


@pg_only
def test_only_one_poller_lock_holder_release_and_recovery(db):
    a, b = poller.PollerLock(engine), poller.PollerLock(engine)
    try:
        assert a.acquire() and a.held()
        assert not b.acquire()  # e dyta nuk merr kyçin
        assert a.acquire()  # i njëjti mban (idempotent)
        a.release()
        assert b.acquire()  # lirimi ⇒ tjetri e merr
        # humbje: serveri e mbyll lidhjen e b ⇒ kyçi lirohet vetë; b e zbulon dhe rimerr
        with engine.connect() as admin:
            pid = b._conn.exec_driver_sql("select pg_backend_pid()").scalar()
            admin.execute(text("select pg_terminate_backend(:p)"), {"p": pid})
        assert not b.held()
        assert a.acquire()  # tjetri e merr pas vdekjes së mbajtësit
        a.release()
        assert b.acquire()
    finally:
        a.release()
        b.release()


@pg_only
def test_run_loop_standby_when_lock_is_held_and_repeated_runs_are_safe(db):
    holder = poller.PollerLock(engine)
    assert holder.acquire()
    stop, polls = threading.Event(), []
    lock = poller.PollerLock(engine)
    t = threading.Thread(
        target=poller.run_loop, args=(SessionLocal, None),
        kwargs=dict(poll_interval_s=5, snapshot_interval_s=3600, stop=stop, lock=lock,
                    poll=lambda *a, **k: polls.append(1) or poller.PollOutcome()),
    )  # fmt: skip
    orig = poller.check_staleness
    poller.check_staleness = lambda f, now=None: 0.0
    try:
        t.start()
        time.sleep(0.5)
        assert polls == []  # pa kyç: asnjë poll
        holder.release()
        time.sleep(6)
        assert polls  # pas lirimit e merr dhe poll-on
        stop.set()
        t.join(10)
        assert not t.is_alive()
        assert not lock.held() and holder.acquire()  # mbyllja e hijshme e liroi kyçin
    finally:
        poller.check_staleness = orig
        stop.set()
        holder.release()
        lock.release()


# --- worker (role control_plane) ------------------------------------------------------------------


@pytest.fixture
def restore_signals():
    import signal

    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield
    for s, h in old.items():
        signal.signal(s, h)


def test_worker_role_misconfiguration_exits_2_and_never_generates_a_key(
    monkeypatch, restore_signals
):
    from app import worker
    from app.core.config import settings

    monkeypatch.setattr(settings, "cp_sync_mode", "shadow")
    monkeypatch.setattr(settings, "cp_base_url", "")
    assert worker.run_control_plane(once=True) == 2


def test_worker_once_is_repeatable_and_idempotent(cp, db, monkeypatch, restore_signals):
    from app import worker
    from app.core.config import settings

    assign(cp, "e1", "sms")
    monkeypatch.setattr(settings, "cp_sync_mode", "shadow")
    monkeypatch.setattr(cc, "config_from_settings", lambda s: cp.client._cfg)
    monkeypatch.setattr(cc, "ControlPlaneClient", lambda cfg: cp.client)
    cp.client.close = lambda: None  # TestClient i fixture-s mbetet i hapur
    assert worker.run_control_plane(once=True) == 0
    n = db.scalar(select(func.count()).select_from(AuditLog))
    assert worker.run_control_plane(once=True) == 0  # thirrje e përsëritur: pa efekt shtesë
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(AuditLog)) == n
    assert ents(db)[(cp.ids["e1"], "sms")].status == "active"


@pg_only
def test_worker_once_does_nothing_when_another_poller_is_active(
    cp, db, monkeypatch, restore_signals
):
    from app import worker
    from app.core.config import settings

    monkeypatch.setattr(settings, "cp_sync_mode", "shadow")
    monkeypatch.setattr(cc, "config_from_settings", lambda s: cp.client._cfg)
    monkeypatch.setattr(cc, "ControlPlaneClient", lambda cfg: cp.client)
    cp.client.close = lambda: None
    holder = poller.PollerLock(engine)
    assert holder.acquire()
    try:
        assert worker.run_control_plane(once=True) == 0
        assert cp.client.calls == [] and cursor(db).epoch is None  # s'u thirr Central
    finally:
        holder.release()
