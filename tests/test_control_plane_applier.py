"""M7-d: aplikuesi lokal i Control Plane (snapshot + feed + kursor). Vetëm gjendje lokale: pa HTTP."""

import ast
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.admin import AuditLog
from app.models.control_plane import (
    CpCursor,
    Entitlement,
    entitlement_enabled,
)
from app.models.enterprise import Enterprise
from app.models.sending import AccountPlan
from app.services import control_plane_sync as cps
from packages.contracts.control_plane import v1

IS_PG = os.environ.get("SMS_TEST_DATABASE_URL", "").startswith("postgresql")
ROOT = Path(__file__).resolve().parents[1]
EPOCH = uuid.UUID("11111111-1111-4111-8111-111111111111")
EPOCH2 = uuid.UUID("22222222-2222-4222-8222-222222222222")
T0 = datetime(2026, 1, 1, tzinfo=UTC)


# --- ndihmëse ----------------------------------------------------------------------------------


def mk_ent(db, name="local-legal", owner="owner-a") -> Enterprise:
    e = Enterprise(owner_ref=owner, legal_name=name, short_name="old")
    db.add(e)
    db.commit()
    return e


def ent_item(eid, rev, name="Acme", status="active"):
    return {
        "entity": {"type": "enterprise", "id": str(eid)}, "enterprise_id": str(eid),
        "revision": rev, "data": {"id": str(eid), "name": name, "status": status},
    }  # fmt: skip


def asg_item(eid, aid, rev, code="sms_std", channel="sms", status="active", pid=None):
    pid = pid or uuid.uuid5(uuid.NAMESPACE_DNS, code)
    return {
        "entity": {"type": "enterprise_product", "id": str(aid)}, "enterprise_id": str(eid),
        "revision": rev,
        "data": {
            "assignment_id": str(aid), "enterprise_id": str(eid),
            "product": {"id": str(pid), "code": code, "channel": channel}, "status": status,
        },
    }  # fmt: skip


def snap_dict(ents=(), asgs=(), seq=10, gen=1, epoch=EPOCH):
    return {
        "epoch": str(epoch), "authorization_generation": gen, "snapshot_seq": seq,
        "enterprises": list(ents), "assignments": list(asgs),
    }  # fmt: skip


def do_snapshot(db, **kw):
    r = cps.apply_snapshot(db, cps.parse_snapshot(snap_dict(**kw)), now=T0)
    db.commit()
    return r


def ev_ent(eid, seq, rev, name="Acme", status="active"):
    return v1.ControlPlaneEventV1(
        str(uuid.uuid4()), seq, "enterprise.upserted", str(eid), str(eid), rev, T0,
        v1.EnterpriseStateV1(str(eid), name, status),
    )  # fmt: skip


def ev_asg(eid, aid, seq, rev, code="sms_std", channel="sms", status="active"):
    pid = uuid.uuid5(uuid.NAMESPACE_DNS, code)
    return v1.ControlPlaneEventV1(
        str(uuid.uuid4()), seq, "enterprise_product.upserted", str(eid), str(aid), rev, T0,
        v1.EnterpriseProductStateV1(str(aid), str(eid), str(pid), code, channel, status),
    )  # fmt: skip


def feed(db, events, next_seq, *, epoch=EPOCH, gen=1):
    r = cps.apply_feed_batch(
        db, epoch=epoch, authorization_generation=gen, events=events, next_seq=next_seq, now=T0
    )
    db.commit()
    return r


def audit_actions(db):
    return [a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.id))]


# --- snapshot ----------------------------------------------------------------------------------


def test_initial_cursor_requires_a_snapshot(db):
    cur = cps.get_cursor(db)
    assert (cur.epoch, cur.authorization_generation, cur.last_seq) == (None, None, 0)
    with pytest.raises(cps.SnapshotRequired) as ex:
        feed(db, [], 5)
    assert ex.value.reason == "no_snapshot"
    assert cps.get_cursor(db).last_seq == 0


def test_snapshot_applies_enterprise_maps_name_to_short_name_and_keeps_local_identity(db):
    e = mk_ent(db)
    r = do_snapshot(db, ents=[ent_item(e.id, 3, name="Acme Central", status="suspended")])
    db.refresh(e)
    assert (e.short_name, e.status, e.cp_revision) == ("Acme Central", "suspended", 3)
    assert e.legal_name == "local-legal" and e.owner_ref == "owner-a"  # kurrë të prekura
    assert r.enterprises_applied == 1


def test_snapshot_creates_independent_sms_and_email_entitlements(db):
    e = mk_ent(db)
    a1, a2 = uuid.uuid4(), uuid.uuid4()
    do_snapshot(
        db,
        ents=[ent_item(e.id, 1)],
        asgs=[
            asg_item(e.id, a1, 2, "sms_std", "sms", "active"),
            asg_item(e.id, a2, 4, "email_std", "email", "suspended"),
        ],
    )
    rows = {r.channel: r for r in db.scalars(select(Entitlement))}
    assert rows["sms"].status == "active" and rows["sms"].revision == 2
    assert rows["email"].status == "suspended" and rows["email"].revision == 4
    assert rows["sms"].assignment_id == a1 and rows["sms"].product_code == "sms_std"
    assert entitlement_enabled("active", rows["sms"].status)
    assert not entitlement_enabled("active", rows["email"].status)
    assert not entitlement_enabled("suspended", rows["sms"].status)  # enterprise pezullon të gjitha


def test_snapshot_stores_cursor_fields_and_no_secret_columns(db):
    e = mk_ent(db)
    do_snapshot(db, ents=[ent_item(e.id, 1)], seq=42, gen=7)
    cur = cps.get_cursor(db)
    assert (cur.epoch, cur.authorization_generation, cur.last_seq) == (EPOCH, 7, 42)
    assert cur.last_snapshot_at is not None and cur.last_success_at is not None
    assert {c.name for c in CpCursor.__table__.columns} == {
        "id", "epoch", "authorization_generation", "last_seq", "last_snapshot_at", "last_success_at",
    }  # fmt: skip


def test_snapshot_is_atomic_on_failure(db):
    e = mk_ent(db)
    good = ent_item(e.id, 5, name="New")
    # i dyti assignment përplas (enterprise, product_code) me një rresht ekzistues ⇒ ApplyError
    other = uuid.uuid4()
    do_snapshot(db, ents=[ent_item(e.id, 1)], asgs=[asg_item(e.id, uuid.uuid4(), 1, "sms_std")])
    snap = cps.parse_snapshot(
        snap_dict(ents=[good], asgs=[asg_item(e.id, other, 2, "sms_std")], seq=20)
    )
    with pytest.raises(cps.ApplyError):
        cps.apply_snapshot(db, snap)
    db.commit()
    db.refresh(e)
    assert e.cp_revision == 1 and e.short_name != "New"  # enterprise u rikthye
    assert cps.get_cursor(db).last_seq == 10


def test_repeating_the_same_snapshot_is_idempotent(db):
    e = mk_ent(db)
    args = dict(ents=[ent_item(e.id, 2)], asgs=[asg_item(e.id, uuid.uuid4(), 3)])
    do_snapshot(db, **args)
    n_audit = len(audit_actions(db))
    r = do_snapshot(db, **args)
    assert (r.enterprises_applied, r.entitlements_applied, r.entitlements_withdrawn) == (0, 0, 0)
    assert r.enterprises_unchanged == 1 and r.entitlements_unchanged == 1
    assert db.scalar(select(func.count()).select_from(Entitlement)) == 1
    assert (
        len(audit_actions(db)) == n_audit + 1
    )  # vetëm rreshti i snapshot-it, pa ndryshime entitetesh


def test_newer_snapshot_updates_and_stale_snapshot_is_rejected(db):
    e = mk_ent(db)
    aid = uuid.uuid4()
    do_snapshot(db, ents=[ent_item(e.id, 1)], asgs=[asg_item(e.id, aid, 1)], seq=10)
    do_snapshot(
        db, ents=[ent_item(e.id, 2, "B", "suspended")],
        asgs=[asg_item(e.id, aid, 2, status="suspended")], seq=20,
    )  # fmt: skip
    db.refresh(e)
    assert (e.short_name, e.status, e.cp_revision) == ("B", "suspended", 2)
    assert db.scalar(select(Entitlement.status)) == "suspended"
    with pytest.raises(cps.StaleSnapshot):
        cps.apply_snapshot(db, cps.parse_snapshot(snap_dict(ents=[ent_item(e.id, 1)], seq=15)))
    db.rollback()
    assert cps.get_cursor(db).last_seq == 20


def test_missing_entitlement_is_withdrawn_not_deleted_and_can_return(db):
    e, other = mk_ent(db), mk_ent(db, owner="owner-b")
    a1, a2, a3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    do_snapshot(
        db, ents=[ent_item(e.id, 1), ent_item(other.id, 1)],
        asgs=[asg_item(e.id, a1, 1, "sms_std"), asg_item(e.id, a2, 1, "email_std", "email"),
              asg_item(other.id, a3, 1, "sms_std")],
    )  # fmt: skip
    r = do_snapshot(db, ents=[ent_item(e.id, 1)], asgs=[asg_item(e.id, a1, 1, "sms_std")], seq=11)
    st = {x.assignment_id: x.status for x in db.scalars(select(Entitlement))}
    assert st[a1] == "active" and st[a2] == "withdrawn"
    assert st[a3] == "active"  # enterprise jashtë snapshot-it: i paprekur (fail-static)
    assert r.entitlements_withdrawn == 1 and len(st) == 3
    assert not entitlement_enabled("active", st[a2])
    # rishfaqja me të njëjtin revision e rikthen
    do_snapshot(
        db, ents=[ent_item(e.id, 1)],
        asgs=[asg_item(e.id, a1, 1), asg_item(e.id, a2, 1, "email_std", "email")], seq=12,
    )  # fmt: skip
    assert {x.assignment_id: x.status for x in db.scalars(select(Entitlement))}[a2] == "active"


def test_snapshot_never_deletes_or_creates_local_enterprises(db):
    e = mk_ent(db)
    ghost = uuid.uuid4()
    r = do_snapshot(db, ents=[ent_item(e.id, 1), ent_item(ghost, 1)],
                    asgs=[asg_item(ghost, uuid.uuid4(), 1)])  # fmt: skip
    assert r.skipped_unknown_enterprise == 1
    assert db.scalar(select(func.count()).select_from(Enterprise)) == 1
    assert db.scalar(select(func.count()).select_from(Entitlement)) == 0
    do_snapshot(db, ents=[], seq=11)  # snapshot bosh: asgjë s'fshihet
    assert db.scalar(select(func.count()).select_from(Enterprise)) == 1


def test_new_epoch_replaces_state_even_with_lower_revisions(db):
    e = mk_ent(db)
    do_snapshot(db, ents=[ent_item(e.id, 9, "Old")], seq=500)
    r = do_snapshot(db, ents=[ent_item(e.id, 1, "Restored")], seq=3, epoch=EPOCH2, gen=1)
    db.refresh(e)
    assert r.reset and (e.short_name, e.cp_revision) == ("Restored", 1)
    cur = cps.get_cursor(db)
    assert (cur.epoch, cur.last_seq) == (EPOCH2, 3)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(epoch="nope"),
        lambda d: d.update(snapshot_seq=-1),
        lambda d: d.update(authorization_generation=True),
        lambda d: d["enterprises"].append(d["enterprises"][0]),
        lambda d: d["assignments"].append(asg_item(uuid.uuid4(), uuid.uuid4(), 1)),
        lambda d: d["enterprises"][0].update(revision=0),
        lambda d: d["enterprises"][0]["data"].update(status="deleted"),
        lambda d: d.pop("assignments"),
    ],
)
def test_malformed_snapshot_is_rejected_before_any_state_change(db, mutate):
    e = mk_ent(db)
    d = snap_dict(ents=[ent_item(e.id, 1)])
    mutate(d)
    with pytest.raises(v1.ContractError):
        cps.parse_snapshot(d)


# --- ngjarje -----------------------------------------------------------------------------------


@pytest.fixture
def base(db):
    e = mk_ent(db)
    aid = uuid.uuid4()
    do_snapshot(db, ents=[ent_item(e.id, 5)], asgs=[asg_item(e.id, aid, 5)], seq=10)
    return e, aid


def test_enterprise_event_newer_equal_lower(db, base):
    e, _ = base
    assert feed(db, [ev_ent(e.id, 11, 6, "N6", "suspended")], 11).applied == 1
    db.refresh(e)
    assert (e.short_name, e.status, e.cp_revision) == ("N6", "suspended", 6)
    r = feed(db, [ev_ent(e.id, 12, 6, "SAME", "active")], 12)  # revision i barabartë: no-op
    assert r.noop == 1
    r = feed(db, [ev_ent(e.id, 13, 4, "OLD", "active")], 13)  # më i ulët: e injoruar, e dukshme
    assert r.stale == 1 and r.outcomes == [(13, "stale")]
    db.refresh(e)
    assert (e.short_name, e.status, e.cp_revision) == ("N6", "suspended", 6)
    assert cps.get_cursor(db).last_seq == 13  # seq përparon pavarësisht freskisë së entitetit


def test_assignment_event_newer_equal_lower(db, base):
    e, aid = base
    feed(db, [ev_asg(e.id, aid, 11, 6, status="suspended")], 11)
    row = db.scalar(select(Entitlement))
    assert (row.status, row.revision) == ("suspended", 6)
    assert feed(db, [ev_asg(e.id, aid, 12, 6, status="active")], 12).noop == 1
    assert feed(db, [ev_asg(e.id, aid, 13, 3, status="active")], 13).stale == 1
    db.refresh(row)
    assert (row.status, row.revision) == ("suspended", 6)


def test_assignment_event_creates_new_entitlement(db, base):
    e, _ = base
    new = uuid.uuid4()
    feed(db, [ev_asg(e.id, new, 11, 1, "email_std", "email")], 11)
    assert db.scalar(select(func.count()).select_from(Entitlement)) == 2


def test_event_for_unknown_enterprise_is_reported_not_applied(db, base):
    r = feed(db, [ev_ent(uuid.uuid4(), 11, 1)], 11)
    assert r.skipped_unknown_enterprise == 1 and cps.get_cursor(db).last_seq == 11


@pytest.mark.parametrize(
    "raw",
    [
        {"schema": "cp.v2"},
        {"schema": "cp.v1", "type": "enterprise.deleted"},
        {"schema": "cp.v1", "event_id": "x"},
        "not-an-object",
    ],
)
def test_malformed_unknown_events_apply_nothing_and_do_not_advance(db, base, raw):
    e, _ = base
    good = ev_ent(e.id, 11, 9, "Good").to_dict()
    with pytest.raises(v1.ContractError):
        cps.parse_events([good, raw])  # parsimi i plotë para aplikimit: as "good" nuk aplikohet
    db.refresh(e)
    assert e.cp_revision == 5 and cps.get_cursor(db).last_seq == 10


def test_epoch_and_generation_mismatch_require_snapshot_and_change_nothing(db, base):
    e, _ = base
    with pytest.raises(cps.SnapshotRequired) as ex:
        feed(db, [ev_ent(e.id, 11, 9)], 11, epoch=EPOCH2)
    assert ex.value.reason == "epoch_mismatch"
    with pytest.raises(cps.SnapshotRequired) as ex:
        feed(db, [ev_ent(e.id, 11, 9)], 11, gen=2)
    assert ex.value.reason == "generation_mismatch"
    db.rollback()
    db.refresh(e)
    assert e.cp_revision == 5 and cps.get_cursor(db).last_seq == 10
    # pas snapshot-it me generation të re, feed-i me të punon
    do_snapshot(db, ents=[ent_item(e.id, 5)], seq=10, gen=2)
    assert feed(db, [ev_ent(e.id, 11, 9)], 11, gen=2).applied == 1


# --- kursori -----------------------------------------------------------------------------------


def test_filtered_seq_gaps_and_next_seq_from_wrapper(db, base):
    e, aid = base
    r = feed(db, [ev_ent(e.id, 100, 6), ev_asg(e.id, aid, 107, 6, status="suspended"),
                  ev_ent(e.id, 129, 7, "X")], 140)  # fmt: skip
    assert r.applied == 3 and cps.get_cursor(db).last_seq == 140  # next_seq > seq i fundit
    feed(db, [], 200)  # faqe bosh: kursori përparon te next_seq
    assert cps.get_cursor(db).last_seq == 200


def test_cursor_never_moves_backwards_or_out_of_order(db, base):
    e, _ = base
    for events, nxt in (
        ([], 9),  # next_seq < kursor
        ([ev_ent(e.id, 10, 6)], 10),  # seq ≤ kursor
        ([ev_ent(e.id, 12, 6), ev_ent(e.id, 11, 7)], 12),  # jo në rend
        ([ev_ent(e.id, 11, 6)], 10),  # seq > next_seq
    ):
        with pytest.raises(cps.ApplyError):
            cps.apply_feed_batch(db, epoch=EPOCH, authorization_generation=1, events=events,
                                 next_seq=nxt)  # fmt: skip
        db.rollback()
    assert cps.get_cursor(db).last_seq == 10


def test_batch_and_cursor_are_atomic_failure_mid_batch(db, base, monkeypatch):
    e, aid = base
    real = cps._apply_entitlement

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(cps, "_apply_entitlement", boom)
    with pytest.raises(RuntimeError):
        cps.apply_feed_batch(
            db, epoch=EPOCH, authorization_generation=1,
            events=[ev_ent(e.id, 11, 6, "A"), ev_asg(e.id, aid, 12, 6, status="suspended")],
            next_seq=12,
        )  # fmt: skip
    db.commit()  # edhe nëse thirrësi commit-on pas përjashtimit, savepoint-i e ka rikthyer
    monkeypatch.setattr(cps, "_apply_entitlement", real)
    db.refresh(e)
    assert e.cp_revision == 5 and cps.get_cursor(db).last_seq == 10
    assert db.scalar(select(Entitlement.revision)) == 5


# --- audit & kufij -----------------------------------------------------------------------------


def test_audit_is_system_actor_and_contains_no_secrets(db, base):
    e, _ = base
    feed(db, [ev_ent(e.id, 11, 6, "N")], 11)
    rows = list(db.scalars(select(AuditLog)))
    assert {r.role for r in rows} == {"system"} and {r.actor for r in rows} == {cps.SYSTEM_ACTOR}
    assert {r.action for r in rows} == {
        "control_plane.enterprise.apply", "control_plane.entitlement.apply",
        "control_plane.snapshot.apply",
    }  # fmt: skip
    blob = " ".join((r.detail or "") for r in rows).lower()
    assert not any(w in blob for w in ("jwt", "private", "secret", "token", "bearer"))


def test_account_plan_and_billing_are_untouched_by_sync(db, base):
    cols = {c.name for c in AccountPlan.__table__.columns}
    path = ROOT / "app/services/control_plane_sync.py"
    assert "app.models.sending" not in _imports(path) and "app.models.billing" not in _imports(path)
    names = {n.id for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Name)}
    assert not names & {"AccountPlan", "BillingProfile", "Wallet"}
    assert "enabled" in cols  # break-glass lokal mbetet i pavarur nga sinkronizimi


def test_apply_event_primitive_does_not_touch_cursor(db, base):
    e, _ = base
    assert cps.apply_event(db, ev_ent(e.id, 99, 6)) == "applied"
    db.commit()
    assert cps.get_cursor(db).last_seq == 10


# --- izolim / paketim ---------------------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    out = set()
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


def test_enterprise_does_not_import_central_and_applier_has_no_network():
    for f in (ROOT / "app").rglob("*.py"):
        assert not any(m.split(".")[0] == "apps" for m in _imports(f)), f
    mods = {m.split(".")[0] for m in _imports(ROOT / "app/services/control_plane_sync.py")}
    assert not mods & {"httpx", "requests", "urllib", "socket", "aiohttp", "http", "jwt"}
    assert "packages" in mods  # kontrata konsumohet nga paketa e përbashkët (jo e kopjuar)
    assert not (ROOT / "app/contracts/control_plane").exists()


def test_m7d_tables_have_no_owner_ref_and_only_expected_schema_added():
    from app.core.db import Base

    for t in ("sms_entitlements", "sms_cp_cursor"):
        assert "owner_ref" not in Base.metadata.tables[t].c
    assert {"sms_entitlements", "sms_cp_cursor"} <= set(Base.metadata.tables)


def test_dockerfile_copies_packages_and_container_layout_imports_cleanly(tmp_path):
    """Simulon shtresën e imazhit: VETËM ajo që COPY-t e Dockerfile sillnin, pa PYTHONPATH."""
    copies = [
        ln.split()[1].lstrip("./")
        for ln in (ROOT / "Dockerfile").read_text().splitlines()
        if ln.startswith("COPY ") and not ln.startswith("COPY requirements")
    ]
    assert "packages" in copies and "app" in copies
    srv = tmp_path / "srv"
    srv.mkdir()
    for name in copies:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, srv / name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy(src, srv / name)
    shutil.rmtree(srv / "apps", ignore_errors=True)  # Central s'është në imazhin Enterprise
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env.update(SMS_DATABASE_URL=f"sqlite:///{tmp_path}/x.db", SMS_ADMIN_API_KEY="k",
               SMS_PII_HMAC_KEY="k", SMS_SECRETS_KEY="wV0dVQ1nH7xk2m3bYw0m8y7QbKpZ0o1o9mGQ0mF0dJQ=")  # fmt: skip
    code = (
        "import app.services.control_plane_sync as m, packages.contracts.control_plane.v1 as v, "
        "app.main, app.worker, sys, pathlib;"
        "print(pathlib.Path(m.__file__).parts[:3], pathlib.Path(v.__file__).is_relative_to(pathlib.Path.cwd()))"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], cwd=srv, env=env, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("True"), r.stdout  # packages vjen nga shtresa, jo nga repo


def test_applier_throughput_is_light(db, base):
    """Matje e lehtë (jo prag i ngushtë): 300 ngjarje enterprise të aplikuara në një batch."""
    e, _ = base
    evs = [ev_ent(e.id, 11 + i, 6 + i, f"N{i}") for i in range(300)]
    t = time.perf_counter()
    r = feed(db, evs, 310)
    dt = time.perf_counter() - t
    print(f"\n[perf] 300 events applied in {dt * 1000:.0f} ms ({dt / 300 * 1000:.2f} ms/event)")
    assert r.applied == 300 and dt < 30


# --- konkurrencë (PostgreSQL) -------------------------------------------------------------------


def run_threads(fns, timeout=40):
    errs, res, threads = [], [], []
    for fn in fns:

        def wrap(fn=fn):
            try:
                res.append(fn())
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        threads.append(threading.Thread(target=wrap))
    [t.start() for t in threads]
    [t.join(timeout) for t in threads]
    assert not [t for t in threads if t.is_alive()], "thread i varur"
    return res, errs


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
@pytest.mark.parametrize("round_", range(4))
def test_pg_concurrent_rev5_and_rev6_end_at_rev6(db, round_):
    e = mk_ent(db)
    do_snapshot(db, ents=[ent_item(e.id, 4)], seq=10)
    barrier = threading.Barrier(2, timeout=20)

    def worker(seq, rev):
        def run():
            with Session(db.get_bind(), expire_on_commit=False) as s:
                barrier.wait()
                out = cps.apply_event(s, ev_ent(e.id, seq, rev, f"N{rev}"))
                s.commit()
                return out

        return run

    res, errs = run_threads([worker(11, 5), worker(12, 6)])
    assert not errs, errs
    db.refresh(e)
    assert e.cp_revision == 6 and e.short_name == "N6"  # rev5 i vonuar s'e ul kurrë
    assert sorted(res) in (["applied", "applied"], ["applied", "stale"])


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_concurrent_duplicate_same_revision_is_safe(db):
    e = mk_ent(db)
    do_snapshot(db, ents=[ent_item(e.id, 4)], seq=10)
    barrier = threading.Barrier(3, timeout=20)

    def run():
        with Session(db.get_bind(), expire_on_commit=False) as s:
            barrier.wait()
            out = cps.apply_event(s, ev_ent(e.id, 11, 5, "Dup"))
            s.commit()
            return out

    res, errs = run_threads([run, run, run])
    assert not errs, errs
    assert sorted(res) == ["applied", "noop", "noop"]
    n = db.scalar(select(func.count()).select_from(AuditLog).where(
        AuditLog.action == "control_plane.enterprise.apply"))  # fmt: skip
    assert n == 2  # snapshot fillestar + saktësisht një aplikim


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_snapshot_and_event_race_never_regresses(db):
    e = mk_ent(db)
    do_snapshot(db, ents=[ent_item(e.id, 4)], seq=10)
    barrier = threading.Barrier(2, timeout=20)

    def snap():
        with Session(db.get_bind(), expire_on_commit=False) as s:
            barrier.wait()
            r = cps.apply_snapshot(
                s, cps.parse_snapshot(snap_dict(ents=[ent_item(e.id, 5)], seq=10))
            )
            s.commit()
            return r

    def event():
        with Session(db.get_bind(), expire_on_commit=False) as s:
            barrier.wait()
            r = cps.apply_feed_batch(s, epoch=EPOCH, authorization_generation=1,
                                     events=[ev_ent(e.id, 11, 6, "N6")], next_seq=11)  # fmt: skip
            s.commit()
            return r

    _, errs = run_threads([snap, event])
    assert not [x for x in errs if not isinstance(x, cps.StaleSnapshot)], errs
    db.refresh(e)
    assert e.cp_revision == 6 and cps.get_cursor(db).last_seq >= 11


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_cursor_singleton_update_race_is_monotonic(db):
    e = mk_ent(db)
    do_snapshot(db, ents=[ent_item(e.id, 1)], seq=10)
    barrier = threading.Barrier(2, timeout=20)

    def batch(events, nxt):
        def run():
            with Session(db.get_bind(), expire_on_commit=False) as s:
                barrier.wait()
                try:
                    cps.apply_feed_batch(s, epoch=EPOCH, authorization_generation=1,
                                         events=events, next_seq=nxt)  # fmt: skip
                    s.commit()
                    return "ok"
                except cps.ApplyError:
                    s.rollback()
                    return "rejected"

        return run

    res, errs = run_threads([batch([ev_ent(e.id, 11, 2, "A")], 11),
                             batch([ev_ent(e.id, 11, 2, "A"), ev_ent(e.id, 12, 3, "B")], 12)])  # fmt: skip
    assert not errs, errs
    cur = cps.get_cursor(db)
    db.refresh(e)
    assert "ok" in res and cur.last_seq in (11, 12)
    assert e.cp_revision == (2 if cur.last_seq == 11 else 3)  # kursori dhe gjendja në sinkron
    assert db.scalar(select(func.count()).select_from(CpCursor)) == 1
