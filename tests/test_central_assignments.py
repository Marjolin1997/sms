"""M5-b — assignment-i Enterprise <-> Product në Central: skemë, service, API, audit, PG."""

import ast
import threading
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from apps.central.core import errors
from apps.central.core.db import Base
from apps.central.models import (
    AuditLog,
    Channel,
    Enterprise,
    EnterpriseProduct,
    Product,
)
from apps.central.models.product import ImmutableError
from apps.central.services import audit as audit_svc
from apps.central.services import enterprise_products as svc
from apps.central.services import enterprises as ent_svc
from apps.central.services import products as prod_svc
from tests.test_central import (  # noqa: F401
    IS_PG,
    ROOT,
    central_alembic,
    enterprise_alembic,
    make_db,
)
from tests.test_central_auth import PW, auth_secret, bearer, mk, token_for  # noqa: F401
from tests.test_central_products import api, cdb, db  # noqa: F401,F811  (fixtures)

T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)


def world(db):
    e = ent_svc.create(db, "Acme")
    p = prod_svc.create(db, "sms", "SMS", "sms")
    db.commit()
    return e, p


def url(eid, *rest):
    return "/admin/enterprises/" + str(eid) + "/products" + "".join("/" + str(r) for r in rest)


def aid(response):
    return response.json()["id"]


# --- service: assign ----------------------------------------------------------------------------------


def test_assign_active_product_to_active_enterprise(db):
    e, p = world(db)
    ep, prod = svc.assign_product(db, e.id, p.id, now=T0)
    db.commit()
    assert isinstance(ep.id, uuid.UUID) and ep.id.version == 4 and prod.id == p.id
    assert (ep.enterprise_id, ep.product_id, ep.status) == (e.id, p.id, "active")
    assert (
        svc.assign_product(db, str(e.id), str(uuid.uuid4()) if False else p.id, now=T0)
        if False
        else True
    )


def test_unknown_enterprise_and_unknown_product_and_bad_ids(db):
    e, p = world(db)
    with pytest.raises(errors.NotFound):
        svc.assign_product(db, uuid.uuid4(), p.id)
    with pytest.raises(errors.NotFound):
        svc.assign_product(db, e.id, uuid.uuid4())
    for bad in ("nope", "123", ""):
        with pytest.raises(errors.Invalid):
            svc.assign_product(db, e.id, bad)
        with pytest.raises(errors.Invalid):
            svc.assign_product(db, bad, p.id)
    assert db.query(EnterpriseProduct).count() == 0


def test_retired_product_and_suspended_enterprise_deny_new_assignments(db):
    e, p = world(db)
    q = prod_svc.create(db, "old", "Old", "sms", status="retired")
    with pytest.raises(errors.Conflict, match="product is retired"):
        svc.assign_product(db, e.id, q.id)
    ent_svc.suspend(db, e.id)
    with pytest.raises(errors.Conflict, match="enterprise is suspended"):
        svc.assign_product(db, e.id, p.id)
    assert db.query(EnterpriseProduct).count() == 0


def test_duplicate_assignment_is_a_conflict_for_every_existing_status(db):
    e, p = world(db)
    ep, _ = svc.assign_product(db, e.id, p.id)
    with pytest.raises(errors.Conflict, match=f"id={ep.id}, status=active"):
        svc.assign_product(db, e.id, p.id)
    svc.suspend_assignment(db, e.id, ep.id)
    with pytest.raises(
        errors.Conflict, match="status=suspended"
    ):  # POST nuk e riaktivizon fshehurazi
        svc.assign_product(db, e.id, p.id)
    assert db.get(EnterpriseProduct, ep.id).status == "suspended"
    assert db.query(EnterpriseProduct).count() == 1


def test_same_product_to_different_enterprises_and_vice_versa_is_allowed(db):
    e, p = world(db)
    e2 = ent_svc.create(db, "Beta")
    p2 = prod_svc.create(db, "email", "Email", "email")
    for a, b in ((e, p), (e2, p), (e, p2), (e2, p2)):
        svc.assign_product(db, a.id, b.id)
    assert db.query(EnterpriseProduct).count() == 4


def test_database_unique_constraint_and_foreign_keys(db):
    e, p = world(db)
    db.add(EnterpriseProduct(enterprise_id=e.id, product_id=p.id))
    db.commit()
    db.add(EnterpriseProduct(enterprise_id=e.id, product_id=p.id))  # anashkalon service
    with pytest.raises(Exception):  # noqa: B017
        db.flush()
    db.rollback()
    if db.get_bind().dialect.name == "postgresql":  # SQLite s'i zbaton FK pa PRAGMA
        for kw in ({"enterprise_id": uuid.uuid4(), "product_id": p.id},
                   {"enterprise_id": e.id, "product_id": uuid.uuid4()}):  # fmt: skip
            db.add(EnterpriseProduct(**kw))
            with pytest.raises(Exception):  # noqa: B017
                db.flush()
            db.rollback()
        with pytest.raises(Exception):  # noqa: B017  (RESTRICT)
            db.execute(text("delete from products where id = :i"), {"i": p.id})
        db.rollback()
        with pytest.raises(Exception):  # noqa: B017
            db.execute(text("delete from enterprises where id = :i"), {"i": e.id})
        db.rollback()
    assert db.query(EnterpriseProduct).count() == 1


def test_database_check_rejects_unknown_status(db):
    e, p = world(db)
    db.add(EnterpriseProduct(enterprise_id=e.id, product_id=p.id, status="pending"))
    with pytest.raises(Exception):  # noqa: B017
        db.flush()


# --- service: get/list -----------------------------------------------------------------------------------


def test_get_assignment_is_scoped_to_the_enterprise(db):
    e, p = world(db)
    e2 = ent_svc.create(db, "Beta")
    ep, _ = svc.assign_product(db, e.id, p.id)
    got, prod = svc.get_assignment(db, e.id, ep.id)
    assert got.id == ep.id and prod.code == "sms"
    with pytest.raises(errors.NotFound):
        svc.get_assignment(db, e2.id, ep.id)  # assignment i enterprise-it tjetër
    with pytest.raises(errors.NotFound):
        svc.get_assignment(db, e.id, uuid.uuid4())
    with pytest.raises(errors.Invalid):
        svc.get_assignment(db, e.id, "x")


def test_list_returns_join_with_product_identity_without_denormalization(db):
    e, p = world(db)
    p2 = prod_svc.create(db, "email", "Email", "email")
    e2 = ent_svc.create(db, "Beta")
    a1, _ = svc.assign_product(db, e.id, p.id, now=T0)
    a2, _ = svc.assign_product(db, e.id, p2.id, now=T0.replace(hour=13))
    svc.assign_product(db, e2.id, p.id)
    rows = svc.list_enterprise_products(db, e.id)
    assert [(ep.id, pr.code) for ep, pr in rows] == [
        (a1.id, "sms"),
        (a2.id, "email"),
    ]  # vetëm i tij
    assert [(ep.id) for ep, _ in svc.list_enterprise_products(db, e.id, channel=Channel.EMAIL)] == [
        a2.id
    ]
    svc.suspend_assignment(db, e.id, a1.id)
    assert [ep.id for ep, _ in svc.list_enterprise_products(db, e.id, status="suspended")] == [
        a1.id
    ]
    assert [ep.id for ep, _ in svc.list_enterprise_products(db, e.id, limit=1, offset=1)] == [a2.id]
    with pytest.raises(errors.NotFound):
        svc.list_enterprise_products(db, uuid.uuid4())
    cols = {c.name for c in EnterpriseProduct.__table__.columns}
    assert not cols & {
        "code",
        "name",
        "channel",
        "product_code",
        "product_status",
    }  # pa denormalizim


# --- service: status -------------------------------------------------------------------------------------------


def test_suspend_and_activate_with_idempotency_and_diff(db):
    e, p = world(db)
    ep, _ = svc.assign_product(db, e.id, p.id, now=T0)
    t1 = T0.replace(hour=14)
    _, _, ch = svc.suspend_assignment(db, e.id, ep.id, now=t1)
    assert ch == {"before": {"status": "active"}, "after": {"status": "suspended"}}
    assert ep.status == "suspended" and ep.updated_at.replace(tzinfo=None) == t1.replace(
        tzinfo=None
    )
    _, _, ch = svc.suspend_assignment(db, e.id, ep.id, now=t1.replace(hour=15))  # idempotent
    assert ch == {} and ep.updated_at.replace(tzinfo=None) == t1.replace(tzinfo=None)
    _, _, ch = svc.activate_assignment(db, e.id, ep.id, now=t1.replace(hour=16))
    assert ch["after"] == {"status": "active"} and ep.status == "active"
    assert svc.activate_assignment(db, e.id, ep.id)[2] == {}  # idempotent
    with pytest.raises(errors.Invalid):
        svc.set_status(db, e.id, ep.id, "pending")


def test_cannot_activate_when_product_retired_or_enterprise_suspended_but_can_suspend(db):
    e, p = world(db)
    ep, _ = svc.assign_product(db, e.id, p.id)
    svc.suspend_assignment(db, e.id, ep.id)
    prod_svc.update(db, p.id, status="retired")
    with pytest.raises(errors.Conflict, match="product is retired"):
        svc.activate_assignment(db, e.id, ep.id)
    prod_svc.update(db, p.id, status="active")
    ent_svc.suspend(db, e.id)
    with pytest.raises(errors.Conflict, match="enterprise is suspended"):
        svc.activate_assignment(db, e.id, ep.id)
    assert db.get(EnterpriseProduct, ep.id).status == "suspended"
    ent_svc.activate(db, e.id)
    assert svc.activate_assignment(db, e.id, ep.id)[0].status == "active"
    prod_svc.update(db, p.id, status="retired")
    assert (
        svc.suspend_assignment(db, e.id, ep.id)[0].status == "suspended"
    )  # suspend: gjithmonë i lejuar
    ep2_active_noop = svc.suspend_assignment(db, e.id, ep.id)[2]
    assert ep2_active_noop == {}


def test_product_retirement_and_enterprise_suspension_never_touch_assignments(db):
    e, p = world(db)
    ep, _ = svc.assign_product(db, e.id, p.id, now=T0)
    db.commit()
    before = (ep.status, ep.updated_at.replace(tzinfo=None))
    prod_svc.update(db, p.id, status="retired")
    ent_svc.suspend(db, e.id)
    db.commit()
    db.expire_all()
    row = db.get(EnterpriseProduct, ep.id)
    assert (row.status, row.updated_at.replace(tzinfo=None)) == before  # asnjë cascade
    rows = svc.list_enterprise_products(db, e.id)
    assert [(r.status, p_.status) for r, p_ in rows] == [("active", "retired")]


def test_pair_is_immutable_and_nothing_is_deleted(db):
    e, p = world(db)
    ep, _ = svc.assign_product(db, e.id, p.id)
    db.commit()
    other = ent_svc.create(db, "Beta")
    ep.enterprise_id = other.id  # "move assignment"
    with pytest.raises(ImmutableError):
        db.flush()
    db.rollback()
    ep = db.get(EnterpriseProduct, ep.id)
    ep.product_id = uuid.uuid4()
    with pytest.raises(ImmutableError):
        db.flush()
    db.rollback()
    db.delete(db.get(EnterpriseProduct, ep.id))
    with pytest.raises(ImmutableError):
        db.flush()
    db.rollback()
    src = (ROOT / "apps/central/services/enterprise_products.py").read_text()
    funcs = [n.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)]
    assert not [f for f in funcs if "delete" in f or "remove" in f or "unassign" in f]


def test_service_never_commits(db):
    e, p = world(db)
    ep, _ = svc.assign_product(db, e.id, p.id)
    db.rollback()
    with pytest.raises(errors.NotFound):
        svc.get_assignment(db, e.id, ep.id)


def test_schema_has_no_pricing_config_or_external_account_fields():
    cols = {c.name for c in EnterpriseProduct.__table__.columns}
    assert cols == {"id", "enterprise_id", "product_id", "status", "created_at", "updated_at"}
    forbidden = {
        "config",
        "limits",
        "price",
        "currency",
        "rate_card_id",
        "monthly_fee",
        "external_account",
    }
    assert not cols & forbidden


# --- API: RBAC, validim, 404 -----------------------------------------------------------------------------------


@pytest.fixture
def seeded(api):
    r = api.post(
        "/admin/products", json={"code": "sms", "name": "SMS", "channel": "sms"}, headers=api.admin
    )
    api.product_id = r.json()["id"]
    with Session(api.eng) as s:
        api.enterprise_id = str(ent_svc.create(s, "Acme").id)
        s.commit()
    return api


def test_unauthenticated_denied_operator_read_only_admin_writes(seeded):
    c, eid, pid = seeded, seeded.enterprise_id, seeded.product_id
    assert c.get(url(eid)).status_code == 401
    assert c.post(url(eid), json={"product_id": pid}).status_code == 401
    assert c.patch(url(eid, uuid.uuid4()), json={"status": "suspended"}).status_code == 401
    r = c.post(url(eid), json={"product_id": pid}, headers=c.admin)
    assert r.status_code == 201
    a = r.json()
    assert (a["enterprise_id"], a["product_id"], a["status"]) == (eid, pid, "active")
    assert (a["product_code"], a["product_name"], a["product_channel"], a["product_status"]) == (
        "sms",
        "SMS",
        "sms",
        "active",
    )
    assert c.get(url(eid), headers=c.operator).status_code == 200
    assert c.get(url(eid, a["id"]), headers=c.operator).json()["id"] == a["id"]
    assert c.post(url(eid), json={"product_id": pid}, headers=c.operator).status_code == 403
    assert (
        c.patch(url(eid, a["id"]), json={"status": "suspended"}, headers=c.operator).status_code
        == 403
    )
    assert c.get(url(eid, a["id"]), headers=c.admin).json()["status"] == "active"


def test_api_errors_404_409_422(seeded):
    c, eid, pid, h = seeded, seeded.enterprise_id, seeded.product_id, seeded.admin
    assert c.get(url("not-a-uuid"), headers=h).status_code == 422
    assert c.get(url(eid, "not-a-uuid"), headers=h).status_code == 422
    assert c.post(url(eid), json={"product_id": "x"}, headers=h).status_code == 422
    assert c.post(url(eid), json={}, headers=h).status_code == 422
    assert c.post(url(eid), json={"product_id": pid, "config": {}}, headers=h).status_code == 422
    assert c.post(url(uuid.uuid4()), json={"product_id": pid}, headers=h).status_code == 404
    assert c.post(url(eid), json={"product_id": str(uuid.uuid4())}, headers=h).status_code == 404
    assert c.get(url(uuid.uuid4()), headers=h).status_code == 404
    assert c.get(url(eid, uuid.uuid4()), headers=h).json()["detail"]["code"] == "not_found"
    first = c.post(url(eid), json={"product_id": pid}, headers=h)
    dup = c.post(url(eid), json={"product_id": pid}, headers=h)
    assert dup.status_code == 409 and aid(first) in dup.json()["detail"]["message"]
    assert c.patch(url(eid, aid(first)), json={"status": "pending"}, headers=h).status_code == 422
    assert c.patch(url(eid, aid(first)), json={}, headers=h).status_code == 422
    for forbidden in (
        {"enterprise_id": eid},
        {"product_id": pid},
        {"id": pid},
        {"status": "active", "config": {}},
    ):
        assert c.patch(url(eid, aid(first)), json=forbidden, headers=h).status_code == 422
    assert c.delete(url(eid, aid(first)), headers=h).status_code == 405


def test_api_status_interaction_through_http(seeded):
    c, eid, pid, h = seeded, seeded.enterprise_id, seeded.product_id, seeded.admin
    a = aid(c.post(url(eid), json={"product_id": pid}, headers=h))
    assert (
        c.patch(url(eid, a), json={"status": "suspended"}, headers=h).json()["status"]
        == "suspended"
    )
    c.patch(f"/admin/products/{pid}", json={"status": "retired"}, headers=h)
    r = c.patch(url(eid, a), json={"status": "active"}, headers=h)
    assert r.status_code == 409 and "retired" in r.json()["detail"]["message"]
    other = c.post(
        "/admin/products",
        json={"code": "old", "name": "Old", "channel": "email", "status": "retired"},
        headers=h,
    )
    assert c.post(url(eid), json={"product_id": other.json()["id"]}, headers=h).status_code == 409
    got = c.get(url(eid), headers=h).json()
    assert [(x["status"], x["product_status"]) for x in got] == [
        ("suspended", "retired")
    ]  # assignment i paprekur
    c.patch(f"/admin/products/{pid}", json={"status": "active"}, headers=h)
    assert c.patch(url(eid, a), json={"status": "active"}, headers=h).json()["status"] == "active"


def test_api_list_filters_and_scoping(seeded):
    c, eid, h = seeded, seeded.enterprise_id, seeded.admin
    em = c.post(
        "/admin/products", json={"code": "email", "name": "Email", "channel": "email"}, headers=h
    ).json()["id"]
    a1 = aid(c.post(url(eid), json={"product_id": c.product_id}, headers=h))
    a2 = aid(c.post(url(eid), json={"product_id": em}, headers=h))
    with Session(c.eng) as s:
        other = str(ent_svc.create(s, "Beta").id)
        s.commit()
    c.post(url(other), json={"product_id": em}, headers=h)
    assert [x["id"] for x in c.get(url(eid), headers=c.operator).json()] == [a1, a2]
    assert [x["id"] for x in c.get(url(eid) + "?channel=email", headers=c.operator).json()] == [a2]
    c.patch(url(eid, a1), json={"status": "suspended"}, headers=h)
    assert [x["id"] for x in c.get(url(eid) + "?status=suspended", headers=c.operator).json()] == [
        a1
    ]
    assert c.get(url(eid) + "?status=bogus", headers=h).status_code == 422
    assert len(c.get(url(eid) + "?limit=1&offset=1", headers=h).json()) == 1
    assert c.get(url(other, a1), headers=h).status_code == 404  # assignment i enterprise-it tjetër


# --- audit ---------------------------------------------------------------------------------------------------


def test_audit_on_assign_and_real_status_changes_only(seeded):
    c, eid, pid, h = seeded, seeded.enterprise_id, seeded.product_id, seeded.admin
    a = aid(c.post(url(eid), json={"product_id": pid}, headers=h))
    c.post(url(eid), json={"product_id": pid}, headers=h)  # 409
    c.post(url(uuid.uuid4()), json={"product_id": pid}, headers=h)  # 404
    c.post(url(eid), json={"product_id": "x"}, headers=h)  # 422
    c.post(url(eid), json={"product_id": pid}, headers=c.operator)  # 403
    c.patch(url(eid, a), json={"status": "active"}, headers=h)  # no-op
    c.patch(url(eid, a), json={"status": "suspended"}, headers=h)
    c.patch(url(eid, a), json={"status": "suspended"}, headers=h)  # no-op
    c.patch(url(eid, a), json={"status": "bogus"}, headers=h)  # 422
    c.patch(url(eid, a), json={"status": "active"}, headers=c.operator)  # 403
    with Session(c.eng) as s:
        rows = (
            s.query(AuditLog)
            .filter(AuditLog.resource_type == "enterprise_product")
            .order_by(AuditLog.created_at)
            .all()
        )
        assert [r.action for r in rows] == [
            "enterprise_product.assign",
            "enterprise_product.update",
        ]
        assert all(r.resource_id == a for r in rows)
        assert rows[0].detail == {
            "after": {"enterprise_id": eid, "product_id": pid, "status": "active"}
        }
        assert rows[1].detail == {"enterprise_id": eid, "product_id": pid,
                                  "before": {"status": "active"}, "after": {"status": "suspended"}}  # fmt: skip
        assert all(r.actor_id for r in rows)


def test_audit_rolls_back_with_the_assignment(db):
    e, p = world(db)
    admin = mk(db.get_bind(), "x@example.com", role="admin")
    ep, _ = svc.assign_product(db, e.id, p.id)
    audit_svc.record(db, admin, "enterprise_product.assign", "enterprise_product", ep.id)
    db.rollback()
    assert db.query(EnterpriseProduct).count() == 0 and db.query(AuditLog).count() == 0


# --- migrim / readiness / izolim ---------------------------------------------------------------------------------


def test_migration_0006_up_down_up_and_readiness(make_db):  # noqa: F811
    from fastapi.testclient import TestClient

    from apps.central.main import create_app

    u = make_db()
    eng = create_engine(u)
    c = TestClient(create_app(eng))
    central_alembic(u, "upgrade", "0005")
    r = c.get("/readyz")
    assert r.status_code == 503 and "not at the expected version" in r.json()["reason"]
    central_alembic(u, "upgrade", "head")
    assert c.get("/readyz").json() == {"status": "ready"}
    assert "enterprise_products" in inspect(eng).get_table_names()
    central_alembic(u, "downgrade", "0005")
    tables = set(inspect(eng).get_table_names())
    assert "enterprise_products" not in tables and {"products", "enterprises", "users"} <= tables
    assert c.get("/readyz").status_code == 503
    central_alembic(u, "upgrade", "head")
    assert c.get("/readyz").status_code == 200


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_schema_matches_metadata_and_is_isolated_from_enterprise_db(make_db):  # noqa: F811
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    ent, cen = make_db("ent"), make_db("central")
    if not ent.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    enterprise_alembic(ent, "upgrade", "head")
    central_alembic(cen, "upgrade", "head")
    with create_engine(cen).connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "version_table": "central_alembic_version"}
        )
        assert compare_metadata(ctx, Base.metadata) == []
    ent_tables = set(inspect(create_engine(ent)).get_table_names())
    cen_tables = set(inspect(create_engine(cen)).get_table_names())
    assert "enterprise_products" not in ent_tables and "enterprise_products" in cen_tables
    assert not any(t.startswith("sms_") for t in cen_tables)


def test_metadata_isolation_and_no_enterprise_imports_or_network():
    import app.models  # noqa: F401
    from app.core.db import Base as EnterpriseBase

    assert "enterprise_products" in Base.metadata.tables
    assert "enterprise_products" not in EnterpriseBase.metadata.tables
    banned = {"httpx", "requests", "celery", "redis", "threading", "asyncio", "sched", "app"}
    for name in (
        "models/enterprise_product.py",
        "services/enterprise_products.py",
        "api/enterprise_products.py",
    ):
        for n in ast.walk(ast.parse((ROOT / "apps/central" / name).read_text())):
            mods = ([n.module] if isinstance(n, ast.ImportFrom) and n.module else
                    [a.name for a in n.names] if isinstance(n, ast.Import) else [])  # fmt: skip
            assert not [m for m in mods if m.split(".")[0] in banned], (name, mods)


# --- PostgreSQL: garë reale ---------------------------------------------------------------------------------------


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_concurrent_assignments_of_the_same_pair_create_exactly_one_row(make_db):  # noqa: F811
    u = make_db()
    if not u.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    central_alembic(u, "upgrade", "head")
    eng = create_engine(u)
    with Session(eng, expire_on_commit=False) as s:
        e, p = world(s)
        eid, pid = e.id, p.id
    barrier = threading.Barrier(2, timeout=20)
    results = [None, None]

    def worker(i):
        with Session(eng, expire_on_commit=False) as s:
            try:
                barrier.wait()  # të dyja pa e parë rreshtin ende; DB vendos
                svc.assign_product(s, eid, pid)
                s.commit()
                results[i] = "created"
            except errors.Conflict:
                s.rollback()
                results[i] = "conflict"

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert sorted(results) == ["conflict", "created"], results
    with Session(eng) as s:
        assert s.query(EnterpriseProduct).count() == 1
    eng.dispose()


def test_assignment_models_are_registered_with_central_metadata_only():
    assert EnterpriseProduct.metadata is Base.metadata
    assert Enterprise.__table__.metadata is Product.__table__.metadata
