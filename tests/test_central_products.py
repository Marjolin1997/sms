"""M5-a — katalogu i produkteve të Central + audit minimal. Pa assignment, pa sync, pa çmime."""

import ast
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from apps.central.core import errors
from apps.central.core.db import Base
from apps.central.main import create_app
from apps.central.models import AuditLog, CentralUser, Channel, Product, ProductStatus
from apps.central.models.audit import AuditImmutableError
from apps.central.models.product import ImmutableError
from apps.central.services import audit as audit_svc
from apps.central.services import products as svc
from tests.test_central import (  # noqa: F401
    IS_PG,
    ROOT,
    central_alembic,
    enterprise_alembic,
    make_db,
)
from tests.test_central_auth import (  # noqa: F401
    PW,
    auth_secret,
    bearer,
    mk,
    token_for,
)

T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)


@pytest.fixture
def cdb(make_db):  # noqa: F811
    url = make_db()
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    yield url, eng
    eng.dispose()


@pytest.fixture
def db(cdb):
    with Session(cdb[1], expire_on_commit=False) as s:
        yield s


@pytest.fixture
def api(cdb):
    _, eng = cdb
    c = TestClient(create_app(eng))
    mk(eng, "adm@example.com", role="admin")
    mk(eng, "op@example.com", role="operator")
    c.admin = bearer(token_for(c, "adm@example.com"))
    c.operator = bearer(token_for(c, "op@example.com"))
    c.eng = eng
    return c


def body(**kw):
    return {"code": "sms", "name": "SMS", "channel": "sms", **kw}


# --- service: krijim, code, emër --------------------------------------------------------------------


def test_create_product_defaults(db):
    p = svc.create(db, "sms", "SMS Standard", Channel.SMS, now=T0)
    db.commit()
    assert isinstance(p.id, uuid.UUID) and p.id.version == 4
    assert (p.code, p.name, p.channel, p.status, p.description) == (
        "sms",
        "SMS Standard",
        "sms",
        "active",
        None,
    )
    assert svc.get(db, p.id).id == p.id and svc.get_by_code(db, " SMS ").id == p.id


def test_code_is_unique_and_normalized(db):
    svc.create(db, "  SMS_Premium ", "Premium", "sms")
    db.commit()
    assert svc.get_by_code(db, "sms_premium").code == "sms_premium"  # strip + lowercase
    for dup in ("sms_premium", "SMS_PREMIUM", " Sms_Premium "):
        with pytest.raises(errors.Conflict):
            svc.create(db, dup, "Other", "sms")


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "a",
        "x" * 33,
        "1sms",
        "sms premium",
        "sms-premium",
        "sms.x",
        "sms\n",
        "é",
        "_sms",
        None,
        5,
    ],
)
def test_invalid_codes_rejected(db, bad):
    if bad == "sms\n":  # whitespace në skaj hiqet nga strip → e vlefshme
        assert svc.normalize_code(bad) == "sms"
        return
    with pytest.raises(errors.Invalid):
        svc.create(db, bad, "Name", "sms")
    assert svc.list_products(db) == []


def test_code_limits_accepted(db):
    assert svc.create(db, "ab", "Two", "email").code == "ab"
    assert svc.create(db, "a" + "b" * 31, "ThirtyTwo", "sms").code == "a" + "b" * 31


@pytest.mark.parametrize("bad", ["", "   ", "x" * 121, "a\x00b", "a\nb", None, 7])
def test_invalid_names_rejected(db, bad):
    with pytest.raises(errors.Invalid):
        svc.create(db, "p1", bad, "sms")


def test_invalid_channel_status_and_description(db):
    with pytest.raises(errors.Invalid):
        svc.create(db, "p1", "N", "voice")
    with pytest.raises(errors.Invalid):
        svc.create(db, "p1", "N", "sms", status="draft")
    with pytest.raises(errors.Invalid):
        svc.create(db, "p1", "N", "sms", description="x" * 1001)
    with pytest.raises(errors.Invalid):
        svc.create(db, "p1", "N", "sms", description="a\x00b")
    ok = svc.create(db, "p1", "N", "sms", description="  line1\nline2\ttab  ")
    assert ok.description == "line1\nline2\ttab"
    assert svc.create(db, "p2", "N", "sms", description="   ").description is None


def test_channel_is_the_real_domain_set():
    assert [c.value for c in Channel] == ["sms", "email"]
    assert [s.value for s in ProductStatus] == ["active", "retired"]


# --- list/get/update/status -----------------------------------------------------------------------------


def test_list_get_filters_and_order(db):
    a = svc.create(db, "sms", "SMS", "sms", now=T0)
    b = svc.create(db, "email", "Email", "email", now=T0.replace(hour=13))
    c = svc.create(db, "sms_old", "Old", "sms", status="retired", now=T0.replace(hour=14))
    assert [p.id for p in svc.list_products(db)] == [a.id, b.id, c.id]
    assert [p.id for p in svc.list_products(db, channel="sms")] == [a.id, c.id]
    assert [p.id for p in svc.list_products(db, status="retired")] == [c.id]
    assert [p.id for p in svc.list_products(db, limit=1, offset=1)] == [b.id]
    with pytest.raises(errors.NotFound):
        svc.get(db, uuid.uuid4())
    with pytest.raises(errors.NotFound):
        svc.get_by_code(db, "nope")


def test_update_changes_only_allowed_fields_and_reports_diff(db):
    p = svc.create(db, "sms", "SMS", "sms", "old", now=T0)
    t1 = T0.replace(hour=15)
    _, ch = svc.update(db, p.id, name=" New ", description=None, now=t1)
    assert ch == {
        "before": {"name": "SMS", "description": "old"},
        "after": {"name": "New", "description": None},
    }
    assert (
        p.name == "New"
        and p.description is None
        and p.updated_at.replace(tzinfo=None) == t1.replace(tzinfo=None)
    )
    _, none = svc.update(db, p.id, name="New", now=T0.replace(hour=16))  # pa ndryshim
    assert none == {} and p.updated_at.replace(tzinfo=None) == t1.replace(tzinfo=None)
    with pytest.raises(TypeError):
        svc.update(db, p.id, code="other")  # code s'është argument i update
    with pytest.raises(TypeError):
        svc.update(db, p.id, channel="email")
    with pytest.raises(errors.Invalid):
        svc.update(db, p.id, name=" ")
    with pytest.raises(errors.NotFound):
        svc.update(db, uuid.uuid4(), name="x")


def test_activate_retire_is_reversible_and_deletes_nothing(db):
    p = svc.create(db, "sms", "SMS", "sms")
    assert svc.update(db, p.id, status="retired")[0].status == "retired"
    assert svc.update(db, p.id, status="active")[0].status == "active"
    assert svc.update(db, p.id, status="active")[1] == {}  # idempotent
    with pytest.raises(errors.Invalid):
        svc.update(db, p.id, status="deleted")
    assert len(svc.list_products(db)) == 1


def test_code_and_channel_are_immutable_at_orm_level(db):
    p = svc.create(db, "sms", "SMS", "sms")
    db.commit()
    p.code = "other"
    with pytest.raises(ImmutableError):
        db.flush()
    db.rollback()
    p = svc.get_by_code(db, "sms")
    p.channel = "email"
    with pytest.raises(ImmutableError):
        db.flush()
    db.rollback()
    assert svc.get_by_code(db, "sms").channel == "sms"


def test_no_hard_delete_anywhere(db):
    p = svc.create(db, "sms", "SMS", "sms")
    db.commit()
    db.delete(p)
    with pytest.raises(ImmutableError):
        db.flush()
    db.rollback()
    src = (ROOT / "apps/central/services/products.py").read_text()
    funcs = [n.name for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef)]
    assert not [f for f in funcs if "delete" in f or "remove" in f]
    assert "delete" not in (ROOT / "apps/central/api/products.py").read_text().lower().replace(
        "hard delete", ""
    )


def test_database_constraints_backstop_the_service(db):
    for kw in (
        {"code": "UPPER", "channel": "sms"}, {"code": "x", "channel": "sms"},
        {"code": "okcode", "channel": "voice"}, {"code": "okcode2", "channel": "sms", "status": "draft"},
        {"code": "okcode3", "channel": "sms", "name": "  "},
    ):  # fmt: skip
        row = Product(name=kw.pop("name", "N"), **kw)
        db.add(row)
        with pytest.raises(Exception):  # noqa: B017
            db.flush()
        db.rollback()


def test_service_never_commits(db):
    p = svc.create(db, "temp", "Temp", "sms")
    db.rollback()
    with pytest.raises(errors.NotFound):
        svc.get(db, p.id)


# --- API: auth, RBAC, kontratë -----------------------------------------------------------------------------


def test_unauthenticated_requests_are_denied(api):
    for call in (lambda: api.get("/admin/products"), lambda: api.post("/admin/products", json=body()),
                 lambda: api.get(f"/admin/products/{uuid.uuid4()}"),
                 lambda: api.patch(f"/admin/products/{uuid.uuid4()}", json={"name": "x"})):  # fmt: skip
        assert call().status_code == 401


def test_admin_can_write_operator_is_read_only(api):
    r = api.post("/admin/products", json=body(code=" SMS "), headers=api.admin)
    assert r.status_code == 201
    pid = r.json()["id"]
    assert (
        r.json()["code"] == "sms"
        and r.json()["status"] == "active"
        and r.json()["channel"] == "sms"
    )
    # operator: lexim po, shkrim jo
    assert api.get("/admin/products", headers=api.operator).status_code == 200
    assert api.get(f"/admin/products/{pid}", headers=api.operator).json()["id"] == pid
    assert (
        api.post(
            "/admin/products", json=body(code="email", channel="email"), headers=api.operator
        ).status_code
        == 403
    )
    assert (
        api.patch(f"/admin/products/{pid}", json={"name": "X"}, headers=api.operator).status_code
        == 403
    )
    assert (
        api.get(f"/admin/products/{pid}", headers=api.admin).json()["name"] == "SMS"
    )  # pa ndryshim


def test_api_list_get_filters_and_pagination(api):
    for code, ch in (("sms", "sms"), ("email", "email"), ("sms_x", "sms")):
        assert (
            api.post(
                "/admin/products", json=body(code=code, name=code, channel=ch), headers=api.admin
            ).status_code
            == 201
        )
    r = api.get("/admin/products", headers=api.operator)
    assert [p["code"] for p in r.json()] == ["sms", "email", "sms_x"]
    assert [
        p["code"] for p in api.get("/admin/products?channel=email", headers=api.operator).json()
    ] == ["email"]
    assert len(api.get("/admin/products?limit=2&offset=1", headers=api.operator).json()) == 2
    assert api.get("/admin/products?limit=0", headers=api.operator).status_code == 422
    assert api.get("/admin/products?status=bogus", headers=api.operator).status_code == 422
    assert api.get("/admin/products/not-a-uuid", headers=api.operator).status_code == 422
    r = api.get(f"/admin/products/{uuid.uuid4()}", headers=api.operator)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "not_found"


def test_api_validation_and_conflict_errors(api):
    h = api.admin
    assert api.post("/admin/products", json=body(), headers=h).status_code == 201
    r = api.post("/admin/products", json=body(code="SMS"), headers=h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "conflict"
    for bad in (body(code="bad code"), body(name=" "), body(channel="voice"), body(status="draft"),
                {**body(), "extra": 1}, {"name": "x"}, body(name="a\x00b")):  # fmt: skip
        assert api.post("/admin/products", json=bad, headers=h).status_code == 422, bad
    assert len(api.get("/admin/products", headers=h).json()) == 1


def test_api_patch_semantics_and_immutability(api):
    h = api.admin
    pid = api.post("/admin/products", json=body(description="d"), headers=h).json()["id"]
    r = api.patch(
        f"/admin/products/{pid}",
        json={"name": " New ", "description": None, "status": "retired"},
        headers=h,
    )
    assert r.status_code == 200
    assert (r.json()["name"], r.json()["description"], r.json()["status"]) == (
        "New",
        None,
        "retired",
    )
    for forbidden in (
        {"code": "other"},
        {"channel": "email"},
        {"id": str(uuid.uuid4())},
        {"created_at": "x"},
    ):
        assert api.patch(f"/admin/products/{pid}", json=forbidden, headers=h).status_code == 422
    assert api.patch(f"/admin/products/{pid}", json={"name": None}, headers=h).status_code == 422
    assert api.patch(f"/admin/products/{pid}", json={"name": " "}, headers=h).status_code == 422
    assert api.patch(f"/admin/products/{pid}", json={}, headers=h).status_code == 200  # no-op
    assert (
        api.patch(f"/admin/products/{uuid.uuid4()}", json={"name": "x"}, headers=h).status_code
        == 404
    )
    got = api.get(f"/admin/products/{pid}", headers=h).json()
    assert got["code"] == "sms" and got["channel"] == "sms"


def test_api_exposes_no_delete_and_no_assignment_routes(api):
    paths = api.app.openapi()["paths"]
    methods = {m for p in paths.values() for m in p}
    assert "delete" not in methods and "put" not in methods
    assert api.delete("/admin/products/" + str(uuid.uuid4()), headers=api.admin).status_code == 405
    assert all(p.endswith("/products") or "/products/" in p for p in paths if "enterprises" in p)


# --- audit -------------------------------------------------------------------------------------------------------


def test_every_write_is_audited_in_the_same_transaction(api):
    h = api.admin
    pid = api.post("/admin/products", json=body(description="d"), headers=h).json()["id"]
    api.patch(f"/admin/products/{pid}", json={"name": "New", "status": "retired"}, headers=h)
    api.patch(f"/admin/products/{pid}", json={"name": "New"}, headers=h)  # no-op → pa audit
    api.post("/admin/products", json=body(), headers=h)  # 409 → pa audit
    api.post("/admin/products", json=body(code="bad code"), headers=h)  # 422 → pa audit
    api.post(
        "/admin/products", json=body(code="email", channel="email"), headers=api.operator
    )  # 403
    with Session(api.eng) as s:
        rows = s.query(AuditLog).order_by(AuditLog.created_at).all()
        admin = s.query(CentralUser).filter_by(email="adm@example.com").one()
        assert [r.action for r in rows] == ["product.create", "product.update"]
        assert all(
            r.actor_id == admin.id and r.resource_type == "product" and r.resource_id == pid
            for r in rows
        )
        assert rows[0].detail == {"after": {"code": "sms", "channel": "sms", "name": "SMS",
                                            "description": "d", "status": "active"}}  # fmt: skip
        assert rows[1].detail == {"before": {"name": "SMS", "status": "active"},
                                  "after": {"name": "New", "status": "retired"}}  # fmt: skip
        assert rows[0].created_at is not None


def test_audit_rows_roll_back_with_the_business_change(db):
    admin = mk(db.get_bind(), "a2@example.com", role="admin")
    p = svc.create(db, "sms", "SMS", "sms")
    audit_svc.record(db, admin, "product.create", "product", p.id)
    db.rollback()
    assert db.query(AuditLog).count() == 0 and db.query(Product).count() == 0


def test_audit_log_is_append_only_and_actor_is_required(db):
    admin = mk(db.get_bind(), "a3@example.com", role="admin")
    row = audit_svc.record(db, admin, "x.y", "thing", "1", {"k": "v"})
    db.commit()
    row.action = "tampered"
    with pytest.raises(AuditImmutableError):
        db.flush()
    db.rollback()
    db.delete(db.get(AuditLog, row.id))
    with pytest.raises(AuditImmutableError):
        db.flush()
    db.rollback()
    db.add(
        AuditLog(actor_id=uuid.uuid4(), action="a", resource_type="t", resource_id="1")
    )  # actor i panjohur
    if db.get_bind().dialect.name == "postgresql":
        with pytest.raises(Exception):  # noqa: B017  (FK)
            db.flush()
    db.rollback()


def test_audit_never_contains_secrets(api):
    api.post("/admin/products", json=body(), headers=api.admin)
    with api.eng.connect() as c:
        dump = " ".join(str(r) for r in c.execute(text("select detail, action from audit_log")))
    assert PW not in dump and "$argon2" not in dump and "eyJ" not in dump


# --- migrim / readiness / izolim ---------------------------------------------------------------------------


def test_migrations_0004_0005_up_down_up_and_readiness(make_db):  # noqa: F811
    url = make_db()
    eng = create_engine(url)
    c = TestClient(create_app(eng))
    central_alembic(url, "upgrade", "0003")
    r = c.get("/readyz")
    assert r.status_code == 503 and "not at the expected version" in r.json()["reason"]  # prapa
    central_alembic(url, "upgrade", "head")
    assert c.get("/readyz").json() == {"status": "ready"}
    assert {"products", "audit_log", "users", "enterprises"} <= set(inspect(eng).get_table_names())
    central_alembic(url, "downgrade", "0003")
    tables = set(inspect(eng).get_table_names())
    assert not {"products", "audit_log"} & tables and {"users", "enterprises"} <= tables
    assert c.get("/readyz").status_code == 503
    central_alembic(url, "upgrade", "head")
    assert c.get("/readyz").status_code == 200


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_schema_matches_metadata_and_stays_in_central_db(make_db):  # noqa: F811
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
    assert not ent_tables & {"products", "audit_log", "users", "enterprises"}
    assert not any(t.startswith("sms_") for t in cen_tables)
    assert {"products", "audit_log"} <= cen_tables


def test_metadata_isolation_new_tables_only_in_central():
    import app.models  # noqa: F401
    from app.core.db import Base as EnterpriseBase

    new = {"products", "audit_log"}
    assert new <= set(Base.metadata.tables) and not new & set(EnterpriseBase.metadata.tables)
    assert "sms_audit_log" in EnterpriseBase.metadata.tables  # i Enterprise mbetet i ndarë
    assert "enterprise_products" in Base.metadata.tables  # M5-b


def test_product_domain_has_no_network_sync_or_enterprise_imports():
    banned = {"httpx", "requests", "celery", "redis", "threading", "asyncio", "sched"}
    for name in (
        "models/product.py",
        "models/audit.py",
        "services/products.py",
        "services/audit.py",
        "api/products.py",
    ):
        tree = ast.parse((ROOT / "apps/central" / name).read_text())
        for n in ast.walk(tree):
            mods = (
                [n.module]
                if isinstance(n, ast.ImportFrom) and n.module
                else ([a.name for a in n.names] if isinstance(n, ast.Import) else [])
            )
            for m in mods:
                assert m.split(".")[0] not in banned | {"app"}, (name, m)  # fmt: skip
