"""M4-b — regjistri i Enterprise-ve në Central: skemë, migrim, service, izolim. Pa HTTP, pa sync."""

import ast
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from apps.central.core import errors
from apps.central.core.db import Base
from apps.central.models import Enterprise, EnterpriseStatus
from apps.central.services import enterprises as svc
from tests.test_central import (  # noqa: F401
    IS_PG,
    ROOT,
    central_alembic,
    client_for,
    enterprise_alembic,
    make_db,
)

T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)


def naive(dt):  # SQLite kthen datetime naive; PG aware (UTC)
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


@pytest.fixture
def cdb(make_db):  # noqa: F811
    url = make_db()
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    with sessionmaker(bind=eng, expire_on_commit=False)() as s:
        yield s
    eng.dispose()


# --- model / metadata ----------------------------------------------------------------------------


def test_enterprise_model_lives_only_in_central_metadata():
    import app.models  # noqa: F401
    from app.core.db import Base as EnterpriseBase

    assert Enterprise.metadata is Base.metadata and Enterprise.__tablename__ == "enterprises"
    assert "enterprises" not in EnterpriseBase.metadata.tables
    assert "sms_enterprises" not in Base.metadata.tables
    cols = {c.name for c in Enterprise.__table__.columns}
    assert cols == {"id", "name", "status", "revision", "created_at", "updated_at"}  # pa owner_ref


def test_enterprise_app_metadata_is_unchanged():
    """Golden i ORM-it të Enterprise (M3) mbetet burimi; këtu vetëm: asnjë tabelë e Central aty."""
    import app.models  # noqa: F401
    from app.core.db import Base as EnterpriseBase

    names = set(EnterpriseBase.metadata.tables)
    assert "sms_enterprises" in names and "enterprises" not in names
    assert not names & set(Base.metadata.tables)


def test_primary_key_is_a_uuid_type_not_an_integer():
    pk = Enterprise.__table__.c.id
    assert pk.primary_key and pk.type.__class__.__name__ == "Uuid"
    assert Enterprise.id.property.columns[0].default.arg.__name__ == "uuid4"  # v4, si Enterprise


# --- migrimi (up/down/up, readiness, izolim) --------------------------------------------------------


def test_migration_0002_up_down_up_and_readiness_follows_head(make_db):  # noqa: F811
    from apps.central.core import readiness

    url = make_db()
    c, eng = client_for(url)
    central_alembic(url, "upgrade", "0001")
    r = c.get("/readyz")
    assert (
        r.status_code == 503 and "not at the expected version" in r.json()["reason"]
    )  # 0001 ≠ head
    central_alembic(url, "upgrade", "head")
    assert c.get("/readyz").json() == {"status": "ready"}
    assert "enterprises" in inspect(eng).get_table_names()
    central_alembic(url, "downgrade", "0001")
    assert "enterprises" not in inspect(eng).get_table_names()
    assert c.get("/readyz").status_code == 503
    central_alembic(url, "upgrade", "head")
    assert readiness.check(eng) is None
    with eng.begin() as conn:
        conn.execute(text("update central_alembic_version set version_num = 'zz_future'"))
    assert c.get("/readyz").status_code == 503


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_alembic_autogenerate_sees_no_diff_for_central_on_postgres(make_db):  # noqa: F811
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    url = make_db()
    central_alembic(url, "upgrade", "head")
    with create_engine(url).connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "version_table": "central_alembic_version"}
        )
        assert compare_metadata(ctx, Base.metadata) == []


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_schema_isolation_between_enterprise_and_central_databases(make_db):  # noqa: F811
    ent_url, cen_url = make_db("ent"), make_db("central")
    enterprise_alembic(ent_url, "upgrade", "head")
    ent, cen = create_engine(ent_url), create_engine(cen_url)
    ent_before = set(inspect(ent).get_table_names())
    with ent.connect() as c:
        ent_rows = c.execute(text("select version_num from sms_alembic_version")).scalar()

    central_alembic(cen_url, "upgrade", "head")
    central_alembic(cen_url, "downgrade", "0001")
    central_alembic(cen_url, "upgrade", "head")

    ent_after = set(inspect(ent).get_table_names())
    assert ent_after == ent_before  # migrimi i Central nuk e preku Enterprise DB
    assert "enterprises" not in ent_after and "central_alembic_version" not in ent_after
    assert "sms_alembic_version" in ent_after and "sms_enterprises" in ent_after
    with ent.connect() as c:
        assert c.execute(text("select version_num from sms_alembic_version")).scalar() == ent_rows
    cen_tables = set(inspect(cen).get_table_names())
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
        "notification_outbox",
        "money_sequence",
        "credit_accounts",
        "commercial_ledger_entries",
        "payments",
        "credit_grants",
        "money_events",
        "usage_reports",
        "price_books",
        "price_versions",
        "price_rules",
        "price_assignments",
        "pricing_sequence",
        "commercial_plans",
        "plan_versions",
        "billing_profiles",
        "billing_subscriptions",
        "invoice_number_sequence",
        "invoices",
        "invoice_lines",
        "billing_periods",
        "registration_products",
        "product_registration_policy",
    }
    assert not any(t.startswith("sms_") for t in cen_tables)


# --- service ----------------------------------------------------------------------------------------


def test_create_generates_uuid_v4_and_defaults(cdb):
    e = svc.create(cdb, "  Acme Sh.p.k.  ", now=T0)
    cdb.commit()
    assert isinstance(e.id, uuid.UUID) and e.id.version == 4
    assert e.name == "Acme Sh.p.k." and e.status == "active"  # vetëm strip
    got = svc.get(cdb, e.id)
    assert got.id == e.id and naive(got.created_at) == naive(got.updated_at) == naive(T0)
    assert svc.get(cdb, str(e.id)).id == e.id  # string kanonik pranohet
    assert str(e.id) == str(e.id).lower() and len(str(e.id)) == 36


def test_create_with_explicit_id_and_duplicate_id_fails(cdb):
    eid = uuid.uuid4()
    assert svc.create(cdb, "A", enterprise_id=eid).id == eid
    cdb.commit()
    with pytest.raises(errors.Conflict):
        svc.create(cdb, "B", enterprise_id=eid)
    with pytest.raises(errors.Conflict):
        svc.create(cdb, "B", enterprise_id=str(eid))
    assert len(svc.list_enterprises(cdb)) == 1


def test_duplicate_id_race_is_reported_as_conflict_by_the_database(cdb):
    eid = uuid.uuid4()
    svc.create(cdb, "A", enterprise_id=eid)
    cdb.commit()
    cdb.add(Enterprise(id=eid, name="X"))  # anashkalon pre-kontrollin: DB duhet ta refuzojë
    with pytest.raises(Exception) as e:
        cdb.flush()
    assert "UNIQUE" in str(e.value).upper() or "duplicate" in str(e.value).lower()
    cdb.rollback()


def test_equal_names_are_allowed_name_is_not_an_identity(cdb):
    a, b = svc.create(cdb, "Acme"), svc.create(cdb, "Acme")
    assert a.id != b.id and len(svc.list_enterprises(cdb)) == 2


@pytest.mark.parametrize("bad", ["", "   ", "x" * 201, "a\x00b", "a\nb", "\t"])
def test_invalid_names_are_rejected(cdb, bad):
    with pytest.raises(errors.Invalid):
        svc.create(cdb, bad)
    assert svc.list_enterprises(cdb) == []


def test_name_limit_and_non_ascii_names_are_accepted(cdb):
    assert svc.create(cdb, "x" * 200).name == "x" * 200
    assert svc.create(cdb, "Shoqëria Ç ë 你好").name == "Shoqëria Ç ë 你好"


def test_get_unknown_and_malformed_ids(cdb):
    with pytest.raises(errors.NotFound):
        svc.get(cdb, uuid.uuid4())
    with pytest.raises(errors.Invalid):
        svc.get(cdb, "not-a-uuid")
    with pytest.raises(errors.Invalid):
        svc.get(cdb, "12345")  # id numerike s'janë identitet


def test_rename_updates_name_and_updated_at_only_on_change(cdb):
    e = svc.create(cdb, "Old", now=T0)
    later = T0 + timedelta(hours=1)
    svc.rename(cdb, e.id, " New ", now=later)
    cdb.commit()
    e = svc.get(cdb, e.id)
    assert (
        e.name == "New" and naive(e.updated_at) == naive(later) and naive(e.created_at) == naive(T0)
    )
    svc.rename(cdb, e.id, "New", now=later + timedelta(hours=1))  # pa ndryshim
    assert naive(svc.get(cdb, e.id).updated_at) == naive(later)
    with pytest.raises(errors.Invalid):
        svc.rename(cdb, e.id, " ")
    with pytest.raises(errors.NotFound):
        svc.rename(cdb, uuid.uuid4(), "X")


def test_lifecycle_suspend_activate_is_idempotent_and_reversible(cdb):
    e = svc.create(cdb, "Acme", now=T0)
    t1, t2 = T0 + timedelta(hours=1), T0 + timedelta(hours=2)
    assert svc.activate(cdb, e.id, now=t1).status == "active"  # i njëjti: pa ndryshim
    assert naive(e.updated_at) == naive(T0)
    assert svc.suspend(cdb, e.id, now=t1).status == "suspended" and naive(e.updated_at) == naive(t1)
    assert svc.suspend(cdb, e.id, now=t2).status == "suspended" and naive(e.updated_at) == naive(t1)
    assert svc.activate(cdb, e.id, now=t2).status == "active" and naive(e.updated_at) == naive(t2)
    assert [x.id for x in svc.list_enterprises(cdb, status=EnterpriseStatus.ACTIVE)] == [e.id]
    assert svc.list_enterprises(cdb, status=EnterpriseStatus.SUSPENDED) == []
    with pytest.raises(errors.NotFound):
        svc.suspend(cdb, uuid.uuid4())


def test_database_rejects_unknown_status_and_blank_name(cdb):
    cdb.add(Enterprise(id=uuid.uuid4(), name="X", status="deleted"))
    with pytest.raises(Exception):  # noqa: B017  (CHECK constraint)
        cdb.flush()
    cdb.rollback()
    cdb.add(Enterprise(id=uuid.uuid4(), name="   "))
    with pytest.raises(Exception):  # noqa: B017
        cdb.flush()
    cdb.rollback()


def test_list_is_ordered_by_created_at_then_id_and_paginated(cdb):
    ids = [svc.create(cdb, f"E{i}", now=T0 + timedelta(minutes=i)).id for i in range(5)]
    assert [e.id for e in svc.list_enterprises(cdb)] == ids
    assert [e.id for e in svc.list_enterprises(cdb, limit=2, offset=1)] == ids[1:3]
    same = [svc.create(cdb, "S", now=T0 + timedelta(days=1)).id for _ in range(3)]
    tail = [e.id for e in svc.list_enterprises(cdb)][5:]
    assert tail == sorted(same, key=lambda x: x.bytes) or tail == sorted(
        same
    )  # renditje e qëndrueshme


def test_service_never_commits(cdb):
    e = svc.create(cdb, "Temp")
    cdb.rollback()
    with pytest.raises(errors.NotFound):
        svc.get(cdb, e.id)


def test_no_hard_delete_exists_anywhere_in_central():
    mod = ROOT / "apps" / "central"
    src = (mod / "services" / "enterprises.py").read_text()
    funcs = [n.name.lower() for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)]
    assert not [f for f in funcs if "delete" in f or "remove" in f or "purge" in f]
    for f in mod.rglob("*.py"):
        if "migrations" in f.parts:
            continue
        if f.relative_to(mod).as_posix() == "services/pricing.py":
            continue  # M9-e: vetëm fshirja e rregullës në DRAFT (versioni aktiv është i pandryshueshëm, trigger PG)
        if f.relative_to(mod).as_posix() == "services/service_auth.py":
            continue  # përjashtim i vetëm: rreshti i objektivit të klientit (revoke) — jo entitet biznesi
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                assert n.func.attr not in {"delete", "merge"}, (f, n.func.attr)


def test_central_has_no_management_routes_yet():
    paths = set(client_for("sqlite://")[0].app.openapi()["paths"])
    # pa CRUD Enterprise: vetëm rrugët e assignment-it nën /admin/enterprises/{id}/products
    # M9-g1: rrugët e faturimit marrin `enterprise_id` si çelës lidhjeje, jo si CRUD i enterprise-it
    assert all(
        "/products" in p or p.startswith("/admin/billing/") for p in paths if "enterprise" in p
    )


def test_service_layer_has_no_enterprise_or_sync_dependencies():
    mod = ROOT / "apps" / "central"
    for f in mod.rglob("*.py"):
        for n in ast.walk(ast.parse(f.read_text())):
            names = []
            if isinstance(n, ast.ImportFrom) and n.module:
                names = [n.module]
            elif isinstance(n, ast.Import):
                names = [a.name for a in n.names]
            for m in names:
                assert m.split(".")[0] not in {"app", "httpx", "requests", "celery", "redis"}, (
                    f,
                    m,
                )
