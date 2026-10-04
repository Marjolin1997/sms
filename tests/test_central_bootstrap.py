"""M4-c — bootstrap manual i Enterprise-ve ekzistues në Central (idempotent, pa sync, pa schema)."""

import ast
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from apps.central.models import Enterprise
from apps.central.tools import bootstrap_enterprises as bs
from tests.test_central import (  # noqa: F401
    IS_PG,
    ROOT,
    central_alembic,
    enterprise_alembic,
    make_db,
)

T1 = datetime(2029, 5, 4, 10, 30, 15, 123456, tzinfo=UTC)
T2 = datetime(2029, 6, 1, 8, 0, tzinfo=UTC)


def make_source(url, rows):
    """Tabela `sms_enterprises` e lirshme (pa PK/unik): përfshin anomali që DB reale s'i lejon."""
    eng = create_engine(url)
    sqlite = eng.dialect.name == "sqlite"

    def uid(v):  # SQLAlchemy Uuid ruhet CHAR(32) hex në SQLite
        return None if v is None else (v.hex if sqlite else v)

    def ts(v):  # SQLAlchemy DateTime ruhet "YYYY-MM-DD HH:MM:SS.ffffff" (UTC naive) në SQLite
        return v.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ") if sqlite else v

    with eng.begin() as c:
        c.execute(text(
            "create table sms_enterprises (id uuid, owner_ref text, external_id text,"
            " legal_name text, short_name text, status text,"
            " created_at timestamp with time zone, updated_at timestamp with time zone)"
        ))  # fmt: skip
        for r in rows:
            c.execute(text(
                "insert into sms_enterprises values (:id, :o, :x, :l, :s, :st, :c, :u)"
            ), {"id": uid(r["id"] if "id" in r else uuid.uuid4()), "o": r.get("owner_ref"),
                "x": r.get("external_id"), "l": r.get("legal_name"), "s": r.get("short_name"),
                "st": r.get("status", "active"), "c": ts(r.get("created_at", T1)),
                "u": ts(r.get("updated_at", T1))})  # fmt: skip
    eng.dispose()


@pytest.fixture
def dbs(make_db):  # noqa: F811
    ent, cen = make_db("ent"), make_db("central")
    central_alembic(cen, "upgrade", "head")
    return ent, cen


def central_rows(url):
    eng = create_engine(url)
    with Session(eng) as s:
        out = {e.id: (e.name, e.status, e.created_at, e.updated_at) for e in s.query(Enterprise)}
    eng.dispose()
    return out


def source_snapshot(url):
    eng = create_engine(url)
    with eng.connect() as c:
        rows = c.execute(text("select * from sms_enterprises order by owner_ref")).fetchall()
        tables = set(inspect(c).get_table_names())
    eng.dispose()
    return [tuple(r) for r in rows], tables


def naive(dt):
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


# --- rastet e kërkuara --------------------------------------------------------------------------


def test_empty_enterprise_db_is_a_noop(dbs):
    ent, cen = dbs
    make_source(ent, [])
    rep = bs.run(ent, cen)
    assert rep.scanned == 0 and rep.ok and rep.written == 0 and central_rows(cen) == {}
    assert rep.render().startswith("Scanned: 0\nCreate: 0\nMatching: 0\nConflicts: 0\nInvalid: 0")


def test_one_valid_row_creates_the_same_uuid_with_history_preserved(dbs):
    ent, cen = dbs
    eid = uuid.uuid4()
    make_source(ent, [{"id": eid, "owner_ref": "acme", "created_at": T1, "updated_at": T2}])
    rep = bs.run(ent, cen)
    assert rep.ok and len(rep.create) == 1 and rep.written == 1
    got = central_rows(cen)
    assert list(got) == [eid]  # i njëjti UUID, asnjë e re
    name, status, created, updated = got[eid]
    assert (name, status) == ("acme", "active")  # fallback owner_ref (burim i vetëm real)
    assert naive(created) == naive(T1) and naive(updated) == naive(T2)
    assert rep.name_sources == {"owner_ref": 1}


def test_multiple_rows_name_priority_status_mapping_and_report(dbs):
    ent, cen = dbs
    ids = [uuid.uuid4() for _ in range(4)]
    make_source(ent, [
        {"id": ids[0], "owner_ref": "a", "legal_name": " Acme SHPK ", "short_name": "AC"},
        {"id": ids[1], "owner_ref": "b", "short_name": "Beta"},
        {"id": ids[2], "owner_ref": "c", "status": "suspended"},
        {"id": ids[3], "owner_ref": "d", "legal_name": "   ", "short_name": "", "external_id": "ext-9"},
    ])  # fmt: skip
    rep = bs.run(ent, cen)
    got = central_rows(cen)
    assert [got[i][0] for i in ids] == ["Acme SHPK", "Beta", "c", "d"]  # external_id s'është emër
    assert got[ids[2]][1] == "suspended" and got[ids[0]][1] == "active"
    assert rep.name_sources == {"legal_name": 1, "short_name": 1, "owner_ref": 2}
    assert rep.render().splitlines()[:5] == [
        "Scanned: 4", "Create: 4", "Matching: 0", "Conflicts: 0", "Invalid: 0",
    ]  # fmt: skip
    assert "provisional" in rep.render()


def test_rerun_is_idempotent_and_existing_matching_rows_are_noops(dbs):
    ent, cen = dbs
    make_source(ent, [{"owner_ref": "a"}, {"owner_ref": "b"}])
    first = bs.run(ent, cen)
    before = central_rows(cen)
    again = bs.run(ent, cen)
    assert len(first.create) == 2 and first.written == 2
    assert len(again.create) == 0 and len(again.matching) == 2 and again.written == 0 and again.ok
    assert central_rows(cen) == before  # asnjë ndryshim, as timestamps


def test_conflicting_existing_row_fails_without_overwriting_anything(dbs):
    ent, cen = dbs
    eid, other = uuid.uuid4(), uuid.uuid4()
    make_source(ent, [{"id": eid, "owner_ref": "acme"}, {"id": other, "owner_ref": "new"}])
    with Session(create_engine(cen)) as s:
        s.add(Enterprise(id=eid, name="Renamed in Central", status="suspended"))
        s.commit()
    before = central_rows(cen)
    rep = bs.run(ent, cen)
    assert not rep.ok and len(rep.conflicts) == 1 and rep.written == 0
    c = rep.conflicts[0]
    assert c["id"] == eid and c["source"] == {"name": "acme", "status": "active"}
    assert c["target"] == {"name": "Renamed in Central", "status": "suspended"}
    assert central_rows(cen) == before and other not in central_rows(cen)  # asgjë e shkruar
    assert f"CONFLICT enterprise_id={eid}" in rep.render()


def test_duplicate_owner_ref_and_duplicate_id_anomalies_are_invalid_not_written(dbs):
    ent, cen = dbs
    dup = uuid.uuid4()
    make_source(ent, [
        {"owner_ref": "Acme"}, {"owner_ref": "acme "}, {"owner_ref": " ACME"},  # variante
        {"id": dup, "owner_ref": "x1"}, {"id": dup, "owner_ref": "x2"},  # id i dyfishtë
        {"owner_ref": "fine"},
    ])  # fmt: skip
    rep = bs.run(ent, cen)
    reasons = sorted(i["reason"] for i in rep.invalid)
    assert reasons.count("duplicate owner_ref in source (case/space-insensitive)") == 3
    assert reasons.count("duplicate enterprise id in source") == 2
    assert len(rep.create) == 1 and rep.written == 0 and central_rows(cen) == {}  # asgjë


def test_missing_name_source_and_unknown_status_are_invalid(dbs):
    ent, cen = dbs
    make_source(ent, [
        {"owner_ref": None}, {"owner_ref": "   "}, {"owner_ref": "ok1", "status": "deleted"},
        {"owner_ref": "ok2", "status": None}, {"id": None, "owner_ref": "noid"},
        {"owner_ref": "x" * 201},
    ])  # fmt: skip
    rep = bs.run(ent, cen)
    reasons = " | ".join(i["reason"] for i in rep.invalid)
    assert "missing owner_ref" in reasons and "unknown status 'deleted'" in reasons
    assert "unknown status None" in reasons and "missing id" in reasons
    assert not rep.create and rep.written == 0 and len(rep.invalid) == 6 and central_rows(cen) == {}


def test_over_long_name_source_is_invalid_not_truncated(dbs):
    ent, cen = dbs
    make_source(ent, [{"owner_ref": "a", "legal_name": "x" * 201}])
    rep = bs.run(ent, cen)
    assert (
        len(rep.invalid) == 1 and "1..200" in rep.invalid[0]["reason"] and central_rows(cen) == {}
    )


def test_dry_run_writes_zero_rows_and_reports_the_plan(dbs):
    ent, cen = dbs
    make_source(ent, [{"owner_ref": "a"}, {"owner_ref": "b"}])
    rep = bs.run(ent, cen, dry_run=True)
    assert rep.dry_run and len(rep.create) == 2 and rep.written == 0
    assert central_rows(cen) == {} and "dry-run (0 writes)" in rep.render()
    bs.run(ent, cen)
    assert len(bs.run(ent, cen, dry_run=True).matching) == 2


def test_enterprise_db_is_unchanged_and_central_only_rows_are_untouched(dbs):
    ent, cen = dbs
    make_source(ent, [{"owner_ref": "a"}, {"owner_ref": "b"}])
    only = uuid.uuid4()
    with Session(create_engine(cen)) as s:
        s.add(Enterprise(id=only, name="Central only"))
        s.commit()
    snap = source_snapshot(ent)
    rep = bs.run(ent, cen)
    assert rep.central_only == 1 and rep.ok
    assert source_snapshot(ent) == snap  # Enterprise: rreshta dhe tabela të pandryshuara
    assert central_rows(cen)[only][0] == "Central only"  # s'u fshi/prek


def test_same_url_for_both_databases_is_refused(dbs):
    ent, _ = dbs
    with pytest.raises(ValueError):
        bs.run(ent, ent)


def test_cli_exit_codes_and_report(dbs, tmp_path):
    ent, cen = dbs
    make_source(ent, [{"owner_ref": "a"}])
    env = {**os.environ, "ENTERPRISE_DATABASE_URL": ent, "CENTRAL_DATABASE_URL": cen}

    def cli(*args, e=env):
        return subprocess.run([sys.executable, "-m", "apps.central.tools.bootstrap_enterprises", *args],
                              env=e, cwd=ROOT, capture_output=True, text=True)  # fmt: skip

    r = cli("--dry-run")
    assert r.returncode == 0 and "Create: 1" in r.stdout and central_rows(cen) == {}
    r = cli()
    assert r.returncode == 0 and "written: 1" in r.stdout and len(central_rows(cen)) == 1
    assert ent not in r.stdout + r.stderr and cen not in r.stdout + r.stderr  # pa URL/sekrete
    no_url = {k: v for k, v in env.items() if k != "ENTERPRISE_DATABASE_URL"}
    assert cli(e=no_url).returncode == 2
    make_source_bad = {**env, "ENTERPRISE_DATABASE_URL": f"sqlite:///{tmp_path / 'empty.db'}"}
    assert cli(e=make_source_bad).returncode == 2  # pa tabelë sms_enterprises


# --- PostgreSQL: dy DB reale, Enterprise me migrimet reale ----------------------------------------


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_two_real_postgres_databases_with_real_enterprise_schema(make_db):  # noqa: F811
    ent, cen = make_db("ent"), make_db("central")
    if not ent.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    enterprise_alembic(ent, "upgrade", "head")
    central_alembic(cen, "upgrade", "head")
    owners = ["c1", "c2", "ACME"]
    eng = create_engine(ent)
    with eng.begin() as c:
        for o in owners:
            c.execute(text(
                "insert into sms_enterprises (id, owner_ref, status, created_at, updated_at)"
                " values (:id, :o, 'active', now(), now())"
            ), {"id": uuid.uuid4(), "o": o})  # fmt: skip
        src_ids = {r[0] for r in c.execute(text("select id from sms_enterprises"))}
    before, tables = source_snapshot(ent)

    assert bs.run(ent, cen, dry_run=True).written == 0 and central_rows(cen) == {}
    rep = bs.run(ent, cen)
    assert rep.ok and rep.written == 3 and set(central_rows(cen)) == src_ids  # UUID-të e njëjta
    assert bs.run(ent, cen).written == 0  # rerun
    assert source_snapshot(ent) == (before, tables)  # Enterprise i paprekur
    cen_tables = set(inspect(create_engine(cen)).get_table_names())
    assert cen_tables == {
        "central_alembic_version",
        "enterprises",
        "users",
        "products",
        "audit_log",
        "enterprise_products",
        "sync_sequence",
        "sync_outbox",
        "service_clients",
        "service_keys",
        "service_client_enterprises",
        "service_assertion_jti",
        "registration_requests",
        "registration_products",
        "product_registration_policy",
    }  # bootstrap s'ndryshon skemë
    assert not any(t.startswith("sms_") for t in cen_tables)
    assert "enterprises" not in tables and "central_alembic_version" not in tables


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_source_connection_is_read_only_on_postgres(make_db):  # noqa: F811
    ent = make_db("ent")
    if not ent.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    enterprise_alembic(ent, "upgrade", "head")
    eng = create_engine(ent)
    with eng.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        with pytest.raises(Exception, match="read-only"):
            conn.execute(text("delete from sms_enterprises"))


# --- kufijtë (AST) ------------------------------------------------------------------------------------


def _imports(path):
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.ImportFrom) and n.module:
            yield n.module
        elif isinstance(n, ast.Import):
            yield from (a.name for a in n.names)


def test_bootstrap_imports_no_enterprise_code_and_no_runtime_sync_machinery():
    tool = ROOT / "apps/central/tools/bootstrap_enterprises.py"
    mods = set(_imports(tool))
    assert not [m for m in mods if m == "app" or m.startswith("app.")]
    assert not mods & {"httpx", "requests", "celery", "redis", "threading", "sched", "asyncio"}
    assert "sms_enterprises" in tool.read_text()  # lexim me SQL minimal, jo ORM


def test_central_runtime_never_imports_the_bootstrap_tool():
    for f in (ROOT / "apps/central").rglob("*.py"):
        if "tools" in f.parts:
            continue
        for m in _imports(f):
            assert "apps.central.tools" not in m, (f, m)


def test_central_schema_has_no_owner_ref_anywhere():
    from apps.central.core.db import Base

    for t in Base.metadata.tables.values():
        assert "owner_ref" not in t.columns and "legacy_owner_ref" not in t.columns
    versions = ROOT / "apps/central/migrations/versions"
    assert sorted(p.name for p in versions.glob("0*.py")) == [
        "0001_baseline.py",
        "0002_enterprises.py",
        "0003_users.py",
        "0004_products.py",
        "0005_audit_log.py",
        "0006_enterprise_products.py",
        "0007_sync_outbox.py",
        "0008_service_credentials.py",
        "0009_service_assertion_jti.py",
        "0010_sync_epoch.py",
        "0011_audit_actor_kind.py",
        "0012_assignment_rate_limit.py",
        "0013_registration.py",
        "0014_product_registration_policy.py",
    ]
