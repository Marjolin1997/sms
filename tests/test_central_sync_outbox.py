"""M7-b1 — revision per entitet + numërues global transaksional + outbox (vetëm Central)."""

import ast
import threading
import time
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.central.core import errors
from apps.central.core.db import Base
from apps.central.models import (
    AuditLog,
    Enterprise,
    EnterpriseProduct,
    SyncOutbox,
    SyncSequence,
)
from apps.central.models.sync import RevisionError, SyncOutboxImmutableError
from apps.central.services import enterprise_products as asg
from apps.central.services import enterprises as ent
from apps.central.services import products as prod
from apps.central.services import sync
from tests.test_central import (  # noqa: F401
    IS_PG,
    ROOT,
    central_alembic,
    enterprise_alembic,
    make_db,
)
from tests.test_central_auth import auth_secret, bearer, mk, token_for  # noqa: F401
from tests.test_central_products import api, cdb, db  # noqa: F401

T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)


def outbox(db, entity_id=None):
    q = select(SyncOutbox).order_by(SyncOutbox.seq)
    if entity_id is not None:
        q = q.where(SyncOutbox.entity_id == entity_id)
    return list(db.scalars(q))


def counter(db):
    return db.scalar(select(SyncSequence.last_seq))


def world(db):
    e = ent.create(db, "Acme", now=T0)
    p = prod.create(db, "sms", "SMS", "sms")
    db.commit()
    return e, p


# --- migrimi: backfill, up/down/up, readiness ----------------------------------------------------


def _uid(eng, v):
    return v.hex if eng.dialect.name == "sqlite" else v


def _ts(eng, v):
    return v.replace(tzinfo=None).isoformat(sep=" ") if eng.dialect.name == "sqlite" else v


def test_existing_rows_get_revision_1_and_migration_creates_no_events(make_db):  # noqa: F811
    url = make_db()
    central_alembic(url, "upgrade", "0006")
    eng = create_engine(url)
    eid, pid, aid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with eng.begin() as c:
        c.execute(text("insert into enterprises (id, name, status, created_at, updated_at) "
                       "values (:i, 'Old', 'active', :t, :t)"), {"i": _uid(eng, eid), "t": _ts(eng, T0)})  # fmt: skip
        c.execute(text("insert into products (id, code, name, channel, status, created_at, updated_at) "
                       "values (:i, 'sms', 'SMS', 'sms', 'active', :t, :t)"), {"i": _uid(eng, pid), "t": _ts(eng, T0)})  # fmt: skip
        c.execute(text("insert into enterprise_products (id, enterprise_id, product_id, status, created_at, updated_at) "
                       "values (:i, :e, :p, 'active', :t, :t)"),
                  {"i": _uid(eng, aid), "e": _uid(eng, eid), "p": _uid(eng, pid), "t": _ts(eng, T0)})  # fmt: skip
    central_alembic(url, "upgrade", "head")
    with Session(eng) as s:
        assert s.get(Enterprise, eid).revision == 1 and s.get(EnterpriseProduct, aid).revision == 1
        assert outbox(s) == [] and counter(s) == 0  # migrim ≠ replay
        ent.rename(s, eid, "New")  # ndryshimi i parë real → revision 2, seq 1
        s.commit()
        row = outbox(s)[0]
        assert (row.seq, row.revision, row.entity_id) == (1, 2, eid)
        assert s.get(Enterprise, eid).revision == 2


def test_migration_0007_up_down_up_and_readiness(make_db):  # noqa: F811
    from fastapi.testclient import TestClient

    from apps.central.main import create_app

    url = make_db()
    eng = create_engine(url)
    c = TestClient(create_app(eng))
    central_alembic(url, "upgrade", "0006")
    r = c.get("/readyz")
    assert r.status_code == 503 and "not at the expected version" in r.json()["reason"]
    central_alembic(url, "upgrade", "head")
    assert c.get("/readyz").json() == {"status": "ready"}
    tables = set(inspect(eng).get_table_names())
    assert {"sync_sequence", "sync_outbox"} <= tables
    assert "revision" in {col["name"] for col in inspect(eng).get_columns("enterprises")}
    central_alembic(url, "downgrade", "0006")
    tables = set(inspect(eng).get_table_names())
    assert not {"sync_sequence", "sync_outbox"} & tables
    assert "revision" not in {col["name"] for col in inspect(eng).get_columns("enterprises")}
    assert "revision" not in {
        col["name"] for col in inspect(eng).get_columns("enterprise_products")
    }
    assert c.get("/readyz").status_code == 503
    central_alembic(url, "upgrade", "head")
    assert c.get("/readyz").status_code == 200
    with eng.connect() as conn:
        assert conn.execute(text("select id, last_seq from sync_sequence")).all() == [(1, 0)]


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_metadata_matches_the_migrated_schema_on_postgres(make_db):  # noqa: F811
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    url = make_db()
    if not url.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    central_alembic(url, "upgrade", "head")
    with create_engine(url).connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "version_table": "central_alembic_version"}
        )
        assert compare_metadata(ctx, Base.metadata) == []


def test_singleton_sequence_and_outbox_constraints(db):
    for bad in ({"id": 2, "last_seq": 0}, {"id": 1, "last_seq": 5}):  # id=2 CHECK; id=1 PK dup
        db.add(SyncSequence(**bad))
        with pytest.raises(Exception):  # noqa: B017
            db.flush()
        db.rollback()
    e, _ = world(db)
    db.add(SyncOutbox(seq=0, enterprise_id=e.id, entity_type="enterprise", entity_id=e.id,
                      revision=1, event_type="x", payload={}))  # fmt: skip
    with pytest.raises(Exception):  # noqa: B017  (seq >= 1)
        db.flush()


# --- revision + outbox për ndryshime reale -------------------------------------------------------------


def test_enterprise_create_and_real_mutations_bump_revision_and_emit(db):
    e = ent.create(db, "Acme", now=T0)
    assert e.revision == 1
    ent.rename(db, e.id, " Acme 2 ")
    ent.suspend(db, e.id)
    ent.activate(db, e.id)
    db.commit()
    rows = outbox(db, e.id)
    assert [(r.seq, r.revision, r.event_type, r.entity_type) for r in rows] == [
        (i, i, "enterprise.upserted", "enterprise") for i in (1, 2, 3, 4)
    ]
    assert e.revision == 4 and counter(db) == 4
    assert all(r.enterprise_id == e.id for r in rows)


def test_assignment_assign_and_status_changes_bump_revision_and_emit(db):
    e, p = world(db)
    ep, _ = asg.assign_product(db, e.id, p.id)
    asg.suspend_assignment(db, e.id, ep.id)
    asg.activate_assignment(db, e.id, ep.id)
    db.commit()
    rows = outbox(db, ep.id)
    assert [(r.revision, r.event_type, r.entity_type) for r in rows] == [
        (1, "enterprise_product.upserted", "enterprise_product"),
        (2, "enterprise_product.upserted", "enterprise_product"),
        (3, "enterprise_product.upserted", "enterprise_product"),
    ]
    assert ep.revision == 3 and all(r.enterprise_id == e.id for r in rows)
    assert [r.seq for r in outbox(db)] == [1, 2, 3, 4]  # enterprise (1) + 3 assignment


def test_no_ops_do_not_bump_allocate_or_emit(db):
    e, p = world(db)
    ep, _ = asg.assign_product(db, e.id, p.id)
    ent.suspend(db, e.id)
    ent.activate(db, e.id)
    asg.suspend_assignment(db, e.id, ep.id)
    db.commit()
    before = (counter(db), len(outbox(db)), e.revision, ep.revision, e.updated_at, ep.updated_at)
    ent.rename(db, e.id, "  Acme ")  # emri i njëjti pas strip
    ent.activate(db, e.id)  # tashmë active
    asg.suspend_assignment(db, e.id, ep.id)  # tashmë suspended
    asg.set_status(db, e.id, ep.id, "suspended")
    db.commit()
    assert before == (
        counter(db),
        len(outbox(db)),
        e.revision,
        ep.revision,
        e.updated_at,
        ep.updated_at,
    )


def test_noop_takes_no_global_lock_and_allocates_no_seq(db):
    e, _ = world(db)
    start = counter(db)
    for _ in range(3):
        ent.rename(db, e.id, "Acme")
        ent.activate(db, e.id)
    assert counter(db) == start


def test_event_ids_are_unique_and_stable_across_reads(db):
    e, _ = world(db)
    ent.rename(db, e.id, "B")
    ent.rename(db, e.id, "C")
    db.commit()
    first = {r.seq: r.event_id for r in outbox(db)}
    db.expire_all()
    assert {r.seq: r.event_id for r in outbox(db)} == first
    assert len(set(first.values())) == len(first) and all(
        isinstance(v, uuid.UUID) for v in first.values()
    )
    row = outbox(db)[0]
    row.event_id = uuid.uuid4()
    with pytest.raises(SyncOutboxImmutableError):
        db.flush()
    db.rollback()


def test_global_seq_is_dense_unique_and_equals_the_counter(db):
    es = [ent.create(db, f"E{i}") for i in range(4)]
    for e in es:
        ent.rename(db, e.id, e.name + "x")
    db.commit()
    seqs = [r.seq for r in outbox(db)]
    assert seqs == list(range(1, 9)) and counter(db) == 8  # 1..N sipas rendit të alokimit
    with pytest.raises(IntegrityError):
        db.add(SyncOutbox(seq=3, enterprise_id=es[0].id, entity_type="enterprise", entity_id=es[0].id,
                          revision=99, event_type="x", payload={}))  # fmt: skip
        db.flush()
    db.rollback()


def test_unique_entity_revision_is_enforced(db):
    e, _ = world(db)
    db.add(SyncOutbox(seq=counter(db) + 1, enterprise_id=e.id, entity_type="enterprise",
                      entity_id=e.id, revision=1, event_type="x", payload={}))  # fmt: skip
    with pytest.raises(IntegrityError):  # (enterprise, e.id, 1) ekziston
        db.flush()
    db.rollback()


def test_multiple_mutations_in_one_transaction_are_consecutive(db):
    e, p = world(db)
    base = counter(db)
    ent.rename(db, e.id, "X")
    ep, _ = asg.assign_product(db, e.id, p.id)
    asg.suspend_assignment(db, e.id, ep.id)
    ent.suspend(db, e.id)
    db.commit()
    assert [r.seq for r in outbox(db)][-4:] == [base + 1, base + 2, base + 3, base + 4]


# --- rollback / atomicitet ---------------------------------------------------------------------------------


def test_caller_rollback_removes_mutation_revision_counter_and_outbox(db):
    e, _ = world(db)
    start = (counter(db), len(outbox(db)), e.revision)
    ent.rename(db, e.id, "Gone")
    ent.suspend(db, e.id)
    db.rollback()
    db.expire_all()
    assert (counter(db), len(outbox(db))) == start[:2]
    e2 = db.get(Enterprise, e.id)
    assert (e2.revision, e2.name, e2.status) == (start[2], "Acme", "active")


def test_failure_after_revision_bump_before_outbox_rolls_everything_back(db, monkeypatch):
    e, _ = world(db)
    start = counter(db)

    def boom(*_a, **_k):
        raise RuntimeError("outbox down")

    monkeypatch.setattr(sync, "emit", boom)
    with pytest.raises(RuntimeError):
        ent.rename(db, e.id, "Half")  # revision u rrit dhe u flush-ua para emit
    db.rollback()
    db.expire_all()
    e2 = db.get(Enterprise, e.id)
    assert (e2.name, e2.revision, counter(db), len(outbox(db))) == ("Acme", 1, start, start)


def test_outbox_constraint_failure_rolls_back_the_business_change(db):
    e, _ = world(db)
    start = counter(db)
    db.add(
        SyncOutbox(
            seq=start + 1000,
            enterprise_id=e.id,
            entity_type="enterprise",
            entity_id=e.id,
            revision=2,
            event_type="x",
            payload={},
        )
    )  # pre-ndërhyrje
    db.commit()
    with pytest.raises(IntegrityError):  # emit do të provojë (enterprise, e.id, 2)
        ent.rename(db, e.id, "Blocked")
    db.rollback()
    db.expire_all()
    e2 = db.get(Enterprise, e.id)
    assert (e2.name, e2.revision, counter(db)) == ("Acme", 1, start)


def test_audit_and_outbox_commit_or_roll_back_together_through_the_api(api, monkeypatch):
    pid = api.post(
        "/admin/products", json={"code": "sms", "name": "SMS", "channel": "sms"}, headers=api.admin
    ).json()["id"]
    with Session(api.eng) as s:
        eid = str(ent.create(s, "Acme").id)
        s.commit()
        base = (counter(s), s.scalar(select(func.count()).select_from(AuditLog)))
    ok = api.post(f"/admin/enterprises/{eid}/products", json={"product_id": pid}, headers=api.admin)
    assert ok.status_code == 201
    with Session(api.eng) as s:  # sukses: të dyja
        assert counter(s) == base[0] + 1
        assert s.scalar(select(func.count()).select_from(AuditLog)) == base[1] + 1
    from apps.central.services import audit

    def fail(*_a, **_k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(audit, "record", fail)
    from fastapi.testclient import TestClient

    c2 = TestClient(api.app, raise_server_exceptions=False)
    r = c2.patch(
        f"/admin/enterprises/{eid}/products/{ok.json()['id']}",
        json={"status": "suspended"},
        headers=api.admin,
    )
    assert r.status_code == 500
    with Session(api.eng) as s:  # dështim audit → as biznes, as revision, as outbox
        ep = s.get(EnterpriseProduct, uuid.UUID(ok.json()["id"]))
        assert ep.status == "active" and ep.revision == 1
        assert counter(s) == base[0] + 1 and len(outbox(s, ep.id)) == 1


# --- payload i ngrirë, kufijtë e emetimit --------------------------------------------------------------------


def test_payload_is_a_frozen_state_snapshot(db):
    e, p = world(db)
    ent.rename(db, e.id, "Second")
    ent.rename(db, e.id, "Third")
    ep, prod_row = asg.assign_product(db, e.id, p.id)
    asg.suspend_assignment(db, e.id, ep.id)
    db.commit()
    names = [r.payload["name"] for r in outbox(db, e.id)]
    assert names == ["Acme", "Second", "Third"]  # i ngrirë, jo rilexim i gjendjes së tanishme
    first, second = outbox(db, ep.id)
    assert first.payload == {"assignment_id": str(ep.id), "enterprise_id": str(e.id),
                             "product": {"id": str(p.id), "code": "sms", "channel": "sms"},
                             "status": "active"}  # fmt: skip
    assert second.payload["status"] == "suspended" and first.payload["status"] == "active"
    assert outbox(db, e.id)[0].payload == {
        "enterprise_id": str(e.id),
        "name": "Acme",
        "status": "active",
    }


def test_product_changes_and_enterprise_suspension_do_not_emit_assignment_events(db):
    e, p = world(db)
    ep, _ = asg.assign_product(db, e.id, p.id)
    db.commit()
    base = len(outbox(db))
    prod.update(db, p.id, name="Renamed", description="d")
    prod.update(db, p.id, status="retired")
    prod.create(db, "email", "Email", "email")
    db.commit()
    assert len(outbox(db)) == base  # produkti s'është entitet sync
    ent.suspend(db, e.id)
    db.commit()
    new = outbox(db)[base:]
    assert [(r.entity_type, r.revision) for r in new] == [("enterprise", 2)]
    assert db.get(EnterpriseProduct, ep.id).revision == 1 and len(outbox(db, ep.id)) == 1


def test_bootstrap_style_direct_inserts_have_revision_1_and_no_event(db):
    """Pranim i dokumentuar: import-i M4-c (ORM direkt) mbulohet nga snapshot-i, jo nga outbox."""
    db.add(Enterprise(id=uuid.uuid4(), name="Imported"))
    db.commit()
    assert db.query(Enterprise).one().revision == 1 and outbox(db) == []


# --- disiplina e revision në ORM ---------------------------------------------------------------------------


def test_orm_refuses_changes_without_a_proper_revision_bump(db):
    e, p = world(db)
    ep, _ = asg.assign_product(db, e.id, p.id)
    db.commit()
    e.name = "Sneaky"  # pa bump
    with pytest.raises(RevisionError):
        db.flush()
    db.rollback()
    e = db.get(Enterprise, e.id)
    e.revision = 2  # bump pa ndryshim real
    with pytest.raises(RevisionError):
        db.flush()
    db.rollback()
    e = db.get(Enterprise, e.id)
    e.status, e.revision = "suspended", 5  # kërcim +4
    with pytest.raises(RevisionError):
        db.flush()
    db.rollback()
    ep = db.get(EnterpriseProduct, ep.id)
    ep.status = "suspended"
    with pytest.raises(RevisionError):
        db.flush()
    db.rollback()
    assert db.get(Enterprise, e.id).revision == 1 and db.get(EnterpriseProduct, ep.id).revision == 1


# --- PostgreSQL: konkurrencë reale ---------------------------------------------------------------------------


@pytest.fixture
def pg(make_db):  # noqa: F811
    url = make_db()
    if not url.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    yield eng
    eng.dispose()


def run_threads(fns, timeout=30):
    errs, threads = [], []
    for fn in fns:

        def wrap(fn=fn):
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        threads.append(threading.Thread(target=wrap))
    [t.start() for t in threads]
    [t.join(timeout) for t in threads]
    assert not errs, errs
    assert not [t for t in threads if t.is_alive()], "thread i varur"


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_concurrent_mutations_of_the_same_entity_keep_revisions_monotonic(pg):
    with Session(pg, expire_on_commit=False) as s:
        eid = ent.create(s, "Start").id
        s.commit()
    barrier = threading.Barrier(2, timeout=20)

    def worker(name):
        def run():
            with Session(pg, expire_on_commit=False) as s:
                barrier.wait()
                ent.rename(s, eid, name)
                s.commit()

        return run

    run_threads([worker("A"), worker("B")])
    with Session(pg) as s:
        e = s.get(Enterprise, eid)
        rows = outbox(s, eid)
        assert e.revision == 3  # asnjë update i humbur
        assert [r.revision for r in rows] == [1, 2, 3]
        assert [r.seq for r in rows] == sorted(r.seq for r in rows) == [1, 2, 3]
        assert rows[-1].payload["name"] == e.name and {r.payload["name"] for r in rows[1:]} == {
            "A",
            "B",
        }


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_concurrent_mutations_of_different_entities_get_distinct_dense_seq(pg):
    with Session(pg, expire_on_commit=False) as s:
        ids = [ent.create(s, f"E{i}").id for i in range(6)]
        s.commit()
    barrier = threading.Barrier(6, timeout=20)

    def worker(eid):
        def run():
            with Session(pg, expire_on_commit=False) as s:
                barrier.wait()
                ent.rename(s, eid, "Renamed")
                s.commit()

        return run

    run_threads([worker(i) for i in ids])
    with Session(pg) as s:
        rows = outbox(s)
        assert [r.seq for r in rows] == list(
            range(1, 13)
        )  # 6 create + 6 rename, pa dublikim, pa boshllëk
        assert counter(s) == 12
        assert {r.entity_id for r in rows[6:]} == set(ids) and all(
            r.revision == 2 for r in rows[6:]
        )


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_seq_allocation_is_serialized_until_commit_so_no_late_lower_seq(pg):
    """Tx A merr seq N (pa commit); Tx B nuk mund të marrë N+1 derisa A të mbyllet."""
    with Session(pg, expire_on_commit=False) as s:
        a_id, b_id = ent.create(s, "A").id, ent.create(s, "B").id
        s.commit()
        base = counter(s)
    allocated, release, b_done = threading.Event(), threading.Event(), threading.Event()
    seen = {}

    def tx_a():
        with Session(pg, expire_on_commit=False) as s:
            ent.rename(s, a_id, "A2")  # alokon seq base+1, ende pa commit
            seen["a"] = s.scalar(select(func.max(SyncOutbox.seq)))
            allocated.set()
            assert release.wait(20)
            s.commit()

    def tx_b():
        with Session(pg, expire_on_commit=False) as s:
            ent.rename(s, b_id, "B2")  # duhet të presë kyçjen e numëruesit
            seen["b"] = s.scalar(select(func.max(SyncOutbox.seq)))
            s.commit()
            b_done.set()

    ta = threading.Thread(target=tx_a)
    ta.start()
    assert allocated.wait(20)
    tb = threading.Thread(target=tx_b)
    tb.start()
    time.sleep(1.0)
    assert not b_done.is_set(), "B s'duhej të alokonte seq para commit-it të A"
    with Session(pg) as reader:  # asnjë seq i ri i dukshëm ende
        assert reader.scalar(select(func.max(SyncOutbox.seq))) == base
    release.set()
    ta.join(20)
    tb.join(20)
    assert b_done.is_set()
    assert (seen["a"], seen["b"]) == (base + 1, base + 2)
    with Session(pg) as s:
        rows = [r for r in outbox(s) if r.seq > base]
        assert [(r.seq, r.entity_id) for r in rows] == [(base + 1, a_id), (base + 2, b_id)]


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_concurrent_duplicate_assignment_still_creates_one_row_and_one_event(pg):
    with Session(pg, expire_on_commit=False) as s:
        e, p = world(s)
        eid, pid = e.id, p.id
        base = counter(s)
    barrier = threading.Barrier(2, timeout=20)
    results = []

    def worker():
        with Session(pg, expire_on_commit=False) as s:
            barrier.wait()
            try:
                asg.assign_product(s, eid, pid)
                s.commit()
                results.append("created")
            except errors.Conflict:
                s.rollback()
                results.append("conflict")

    run_threads([worker, worker])
    assert sorted(results) == ["conflict", "created"]
    with Session(pg) as s:
        assert s.query(EnterpriseProduct).count() == 1
        assert counter(s) == base + 1 and len([r for r in outbox(s) if r.seq > base]) == 1


# --- kufijtë (AST) ---------------------------------------------------------------------------------------------


def test_sync_modules_have_no_network_workers_or_enterprise_imports():
    banned = {"httpx", "requests", "celery", "redis", "threading", "asyncio", "sched", "app"}
    for name in ("models/sync.py", "services/sync.py"):
        for n in ast.walk(ast.parse((ROOT / "apps/central" / name).read_text())):
            mods = ([n.module] if isinstance(n, ast.ImportFrom) and n.module else
                    [a.name for a in n.names] if isinstance(n, ast.Import) else [])  # fmt: skip
            assert not [m for m in mods if m.split(".")[0] in banned], (name, mods)
    api_dir = ROOT / "apps/central/api"
    assert not [
        p.name for p in api_dir.glob("*.py") if "sync" in p.name.lower()
    ]  # pa endpoint feed


def test_metadata_isolation_and_no_enterprise_tables_touched():
    import app.models  # noqa: F401
    from app.core.db import Base as EnterpriseBase

    assert {"sync_sequence", "sync_outbox"} <= set(Base.metadata.tables)
    assert not {"sync_sequence", "sync_outbox"} & set(EnterpriseBase.metadata.tables)
    assert not any(t.startswith("sms_") for t in Base.metadata.tables)
