# ruff: noqa: F811
"""M8-b — politika e regjistrimit për produkt + miratim manual/automatik (Central; pa provisioning)."""

import ast
import threading
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session

from apps.central.core import errors, readiness
from apps.central.core.config import settings
from apps.central.core.db import Base
from apps.central.models import (
    AuditLog,
    CentralUser,
    Enterprise,
    EnterpriseProduct,
    ProductRegistrationPolicy,
    RegistrationProduct,
    RegistrationRequest,
    SyncOutbox,
)
from apps.central.models.product import ImmutableError
from apps.central.services import audit as audit_svc
from apps.central.services import products as prod_svc
from apps.central.services import registration_policy as pol
from apps.central.services import registrations as reg
from apps.central.services import users
from tests.test_central import IS_PG, ROOT, central_alembic, make_db  # noqa: F401
from tests.test_central_products import cdb, db  # noqa: F401  (fixtures)
from tests.test_central_sync_outbox import run_threads

T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)
AUTO = "system:registration_auto_approval"
REG = AuditLog.action.like("registration.%")
POLICY_AUDIT = AuditLog.action.like("registration_policy.%")


@pytest.fixture
def w(db):
    sms = prod_svc.create(db, "sms", "SMS", "sms")
    email = prod_svc.create(db, "email", "Email", "email")
    admin = users.create_user(db, "adm@example.com", "pw-Long-Enough-123", "admin")
    db.commit()
    return db, sms, email, admin


@pytest.fixture
def gate(monkeypatch):
    """Gate-i i sigurisë i hapur eksplicit (vetëm test/dev)."""
    monkeypatch.setattr(settings, "allow_unverified_auto_registration", True)


def count(db, model, *where):
    return db.scalar(select(func.count()).select_from(model).where(*where))


def policy(db, product, admin, **kw):
    row, ch = pol.set_policy(db, product.id, admin, **kw)
    db.commit()
    return row


def submit(db, products, **kw):
    kw = {"enterprise_name": "Acme Ltd", "contact_email": "ana@example.com"} | kw
    return reg.submit(db, product_ids=[p.id for p in products], **kw)


# --- policy ------------------------------------------------------------------------------------------------


def test_missing_policy_means_self_registration_is_disabled(w):
    db, sms, *_ = w
    assert pol.get_policy(db, sms.id) is None
    with pytest.raises(errors.Invalid, match="products_unavailable"):
        submit(db, [sms])
    assert count(db, RegistrationRequest) == 0


def test_enabled_manual_policy_allows_submit_and_leaves_the_decision_open(w):
    db, sms, _, admin = w
    policy(db, sms, admin, self_registration_enabled=True)
    row = submit(db, [sms]).request
    assert (row.status, row.decision_mode, row.provisioning_status) == ("submitted", None, None)


def test_disabled_policy_and_retired_product_reject_submit_with_one_generic_error(w):
    db, sms, email, admin = w
    policy(db, sms, admin, self_registration_enabled=True)
    policy(db, email, admin, self_registration_enabled=True)
    policy(db, sms, admin, self_registration_enabled=False)  # çaktivizim eksplicit
    prod_svc.update(db, email.id, status="retired")  # politika mbetet e aktivizuar
    db.commit()
    for products in ([sms], [email], [sms, email]):
        with pytest.raises(errors.Invalid) as ex:
            submit(db, products)
        assert str(ex.value) == "products_unavailable"  # pa detaje se pse (politikë/retired/mungon)
    with pytest.raises(errors.Invalid) as ex:
        reg.submit(db, enterprise_name="X", contact_email="a@b.co", product_ids=[uuid.uuid4()])
    assert str(ex.value) == "products_unavailable"


def test_policy_defaults_manual_and_creation_is_audited_by_a_human(w):
    db, sms, _, admin = w
    row, ch = pol.set_policy(db, sms.id, admin, now=T0)
    db.commit()
    assert (row.self_registration_enabled, row.approval_mode) == (
        False,
        "manual",
    )  # parazgjedhje mbyllëse
    assert ch == {}  # asnjë fushë e ndryshuar nga parazgjedhja…
    a = db.scalar(select(AuditLog).where(POLICY_AUDIT))  # …por krijimi auditohet
    assert (a.action, a.actor_kind, a.actor_id, a.resource_id) == (
        "registration_policy.create",
        "user",
        admin.id,
        str(sms.id),
    )
    assert a.detail["after"] == {"self_registration_enabled": False, "approval_mode": "manual"}


def test_automatic_is_stored_only_under_the_security_gate(w, monkeypatch):
    db, sms, email, admin = w
    policy(db, sms, admin, self_registration_enabled=True)
    with pytest.raises(errors.Conflict, match="contact verification"):
        pol.set_policy(db, sms.id, admin, approval_mode="automatic")  # gate false (default)
    db.rollback()
    assert pol.get_policy(db, sms.id).approval_mode == "manual"
    with pytest.raises(errors.Conflict):
        pol.set_policy(
            db, email.id, admin, self_registration_enabled=True, approval_mode="automatic"
        )
    db.rollback()
    assert pol.get_policy(db, email.id) is None  # asgjë s'u krijua pjesërisht
    monkeypatch.setattr(settings, "allow_unverified_auto_registration", True)
    row = policy(db, sms, admin, approval_mode="automatic")
    assert row.approval_mode == "automatic"
    monkeypatch.setattr(settings, "allow_unverified_auto_registration", False)
    assert (
        policy(db, sms, admin, approval_mode="manual").approval_mode == "manual"
    )  # rikthimi lejohet


@pytest.mark.parametrize("kw", [{"approval_mode": "auto"}, {"approval_mode": ""}, {"approval_mode": None},
                                {"approval_mode": "MANUAL"}, {"self_registration_enabled": "yes"},
                                {"self_registration_enabled": 1}, {"self_registration_enabled": None}])  # fmt: skip
def test_invalid_policy_values_are_rejected(w, kw):
    db, sms, _, admin = w
    with pytest.raises(errors.Invalid):
        pol.set_policy(db, sms.id, admin, **kw)
    assert pol.get_policy(db, sms.id) is None


def test_policy_actor_product_and_retired_rules(w):
    db, sms, email, admin = w
    for bad in ("system:x", None, object()):
        with pytest.raises(errors.Invalid):
            pol.set_policy(db, sms.id, bad, self_registration_enabled=True)
    with pytest.raises(errors.NotFound):
        pol.set_policy(db, uuid.uuid4(), admin, self_registration_enabled=True)
    with pytest.raises(errors.Invalid):
        pol.set_policy(db, "not-a-uuid", admin)
    policy(db, email, admin, self_registration_enabled=True)
    prod_svc.update(db, email.id, status="retired")
    db.commit()
    prod_svc.update(db, sms.id, status="retired")
    db.commit()
    with pytest.raises(errors.Conflict, match="retired"):
        pol.set_policy(db, sms.id, admin, self_registration_enabled=True)  # s'hapet produkt retired
    db.rollback()
    assert (
        policy(db, email, admin, self_registration_enabled=False).self_registration_enabled is False
    )  # mbyllja lejohet
    assert pol.get_policy(db, email.id) is not None  # historia mbetet pas retire


def test_policy_noop_update_writes_no_audit_and_real_changes_record_before_after(w):
    db, sms, _, admin = w
    policy(db, sms, admin, self_registration_enabled=True)
    n = count(db, AuditLog, POLICY_AUDIT)
    row, ch = pol.set_policy(
        db, sms.id, admin, self_registration_enabled=True, approval_mode="manual"
    )
    db.commit()
    assert ch == {} and count(db, AuditLog, POLICY_AUDIT) == n  # no-op ⇒ zero audit
    updated_at = row.updated_at
    _, ch = pol.set_policy(db, sms.id, admin, self_registration_enabled=False, now=T0)
    db.commit()
    assert ch == {"self_registration_enabled": {"before": True, "after": False}}
    a = list(db.scalars(select(AuditLog).where(POLICY_AUDIT).order_by(AuditLog.created_at)))[-1]
    assert (a.action, a.actor_kind, a.actor_id) == ("registration_policy.update", "user", admin.id)
    assert a.detail["before"] == {"self_registration_enabled": True}
    assert (
        a.detail["after"] == {"self_registration_enabled": False}
        and a.detail["product_code"] == "sms"
    )
    assert count(db, AuditLog, POLICY_AUDIT) == n + 1 and row.updated_at != updated_at


def test_policy_rows_are_never_hard_deleted_and_the_product_link_is_immutable(w):
    db, sms, _, admin = w
    row = policy(db, sms, admin, self_registration_enabled=True)
    db.delete(row)
    with pytest.raises(ImmutableError):
        db.flush()
    db.rollback()
    row = db.get(ProductRegistrationPolicy, sms.id)
    row.product_id = uuid.uuid4()
    with pytest.raises(ImmutableError):
        db.flush()
    db.rollback()
    tree = ast.parse((ROOT / "apps/central/services/registration_policy.py").read_text())
    assert not [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") in {"delete", "merge"}
    ]
    assert ".commit(" not in (ROOT / "apps/central/services/registration_policy.py").read_text()


# --- manual flow -----------------------------------------------------------------------------------------------


def test_manual_policies_leave_requests_submitted_even_when_mixed_with_automatic(w, gate):
    db, sms, email, admin = w
    policy(db, sms, admin, self_registration_enabled=True)  # manual
    policy(db, email, admin, self_registration_enabled=True, approval_mode="automatic")
    one = submit(db, [sms]).request
    mixed = submit(
        db, [sms, email], contact_email="b@example.com"
    ).request  # një manual ⇒ gjithë kërkesa manual
    db.commit()
    for r in (one, mixed):
        assert (r.status, r.decision_mode, r.decided_by_label, r.provisioning_status) == (
            "submitted",
            None,
            None,
            None,
        )
    assert count(db, AuditLog, REG) == 0 and count(db, RegistrationProduct) == 3


def test_manual_approve_revalidates_policy_enabled_policy_and_product_status(w):
    db, sms, email, admin = w
    policy(db, sms, admin, self_registration_enabled=True)
    policy(db, email, admin, self_registration_enabled=True)
    row = submit(db, [sms, email]).request
    db.commit()
    policy(db, email, admin, self_registration_enabled=False)  # çaktivizuar PAS submit-it
    with pytest.raises(errors.Conflict, match="no longer available: email"):
        reg.approve(db, row.id, admin)
    db.rollback()
    assert (
        db.get(RegistrationRequest, row.id).status == "submitted" and count(db, AuditLog, REG) == 0
    )
    policy(db, email, admin, self_registration_enabled=True)
    prod_svc.update(db, sms.id, status="retired")
    db.commit()
    with pytest.raises(errors.Conflict, match="no longer available: sms"):
        reg.approve(db, row.id, admin)  # retired pas submit
    db.rollback()
    prod_svc.update(db, sms.id, status="active")
    db.commit()
    out = reg.approve(db, row.id, admin)  # politika live përsëri e vlefshme ⇒ miratohet
    db.commit()
    assert out.status == "approved" and out.decision_mode == "manual"
    a = db.scalar(select(AuditLog).where(REG))
    assert a.detail["products"] == [
        {"code": "email", "approval_mode": "manual", "self_registration_enabled": True},
        {"code": "sms", "approval_mode": "manual", "self_registration_enabled": True},
    ]  # metadata e politikës në audit; asnjë sekret


def test_policy_without_row_can_never_be_approved(w):
    """Politika që s'ekziston më (p.sh. rresht i hequr jashtë aplikacionit) = e mbyllur edhe te approve."""
    db, sms, _, admin = w
    policy(db, sms, admin, self_registration_enabled=True)
    row = submit(db, [sms]).request
    db.commit()
    db.execute(
        text("delete from product_registration_policy")
    )  # SQL i drejtpërdrejtë, jashtë shërbimit
    db.commit()
    with pytest.raises(errors.Conflict, match="no longer available"):
        reg.approve(db, row.id, admin)


# --- automatic flow -------------------------------------------------------------------------------------------


@pytest.fixture
def auto(w, gate):
    db, sms, email, admin = w
    policy(db, sms, admin, self_registration_enabled=True, approval_mode="automatic")
    policy(db, email, admin, self_registration_enabled=True, approval_mode="automatic")
    return db, sms, email, admin


def test_all_automatic_products_auto_approve_to_pending_with_a_system_audit_row(auto):
    db, sms, email, _ = auto
    r = submit(db, [sms, email], submission_key="key-auto-0001")
    db.commit()
    row = r.request
    assert r.created and r.access_token  # token lëshohet
    assert (row.status, row.decision_mode, row.provisioning_status) == (
        "approved",
        "automatic",
        "pending",
    )
    assert (row.decided_by_id, row.decided_by_label, row.enterprise_id) == (None, AUTO, None)
    assert row.decided_at is not None and row.decision_reason is None
    rows = list(db.scalars(select(AuditLog).where(REG)))
    assert len(rows) == 1  # saktësisht një
    a = rows[0]
    assert (a.actor_kind, a.actor_id, a.actor_label, a.action) == (
        "system",
        None,
        AUTO,
        "registration.approve",
    )
    assert (a.resource_type, a.resource_id) == ("registration_request", str(row.id))
    assert (
        a.detail["decision_mode"] == "automatic"
        and a.detail["unverified_auto_registration_gate"] is True
    )
    assert [p["code"] for p in a.detail["products"]] == ["sms", "email"]
    assert all(
        p["approval_mode"] == "automatic" and p["self_registration_enabled"]
        for p in a.detail["products"]
    )


def test_replay_never_auto_approves_again_audits_again_or_reissues_a_token(auto):
    db, sms, _, _ = auto
    first = submit(db, [sms], submission_key="key-auto-0002")
    db.commit()
    for _ in range(3):
        again = submit(db, [sms], submission_key="key-auto-0002")
        assert (again.created, again.access_token, again.request.id) == (
            False,
            None,
            first.request.id,
        )
    db.commit()
    assert count(db, RegistrationRequest) == 1 and count(db, AuditLog, REG) == 1
    assert db.get(RegistrationRequest, first.request.id).decision_mode == "automatic"


def test_auto_approval_provisions_nothing_and_emits_no_outbox(auto):
    db, sms, email, _ = auto
    row = submit(db, [sms, email]).request
    db.commit()
    assert (
        count(db, Enterprise) == 0
        and count(db, EnterpriseProduct) == 0
        and count(db, SyncOutbox) == 0
    )
    assert all(rp.assignment_id is None for rp in db.scalars(select(RegistrationProduct)))
    assert db.get(RegistrationRequest, row.id).enterprise_id is None


def test_auto_approved_requests_follow_the_m8a_rules_afterwards(auto):
    db, sms, _, admin = auto
    row = submit(db, [sms]).request
    db.commit()
    assert (
        reg.approve(db, row.id, admin).decision_mode == "automatic"
    )  # approved ⇒ no-op, mode s'ndryshon
    db.commit()
    assert count(db, AuditLog, REG) == 1
    with pytest.raises(errors.Conflict, match="awaiting provisioning"):
        reg.reject(
            db, row.id, admin, "x"
        )  # sistemi s'auto-refuzon; njeriu s'refuzon approved/pending


def test_audit_failure_rolls_back_the_whole_auto_approval(auto, monkeypatch):
    db, sms, _, _ = auto

    def boom(*a, **k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(audit_svc, "record_system", boom)
    with pytest.raises(RuntimeError):
        submit(db, [sms])  # rruga pa çelës: pa savepoint
    db.rollback()
    assert count(db, RegistrationRequest) == 0 and count(db, RegistrationProduct) == 0
    assert count(db, AuditLog, REG) == 0  # asnjë kërkesë e miratuar pa audit


@pytest.fixture
def pg(make_db):  # noqa: F811
    url = make_db()
    if not url.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    yield eng
    eng.dispose()


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_keyed_audit_failure_rolls_back_inside_the_savepoint(pg, gate, monkeypatch):
    with Session(pg, expire_on_commit=False) as s:
        sms = prod_svc.create(s, "sms", "SMS", "sms")
        admin = users.create_user(s, "adm@example.com", "pw-Long-Enough-123", "admin")
        pol.set_policy(s, sms.id, admin, self_registration_enabled=True, approval_mode="automatic")
        s.commit()
    with Session(pg, expire_on_commit=False) as s:
        monkeypatch.setattr(
            audit_svc, "record_system", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        )
        with pytest.raises(RuntimeError):
            reg.submit(
                s,
                enterprise_name="A",
                contact_email="a@b.co",
                product_ids=[sms.id],
                submission_key="key-pg-0001",
            )
        s.rollback()
    with Session(pg) as s:
        assert count(s, RegistrationRequest) == 0 and count(s, AuditLog, REG) == 0


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_concurrent_identical_automatic_submits_create_one_request_and_one_audit(pg, gate):
    with Session(pg, expire_on_commit=False) as s:
        sms = prod_svc.create(s, "sms", "SMS", "sms")
        admin = users.create_user(s, "adm@example.com", "pw-Long-Enough-123", "admin")
        pol.set_policy(s, sms.id, admin, self_registration_enabled=True, approval_mode="automatic")
        s.commit()
    barrier = threading.Barrier(6, timeout=20)
    results = []

    def worker():
        with Session(pg, expire_on_commit=False) as s:
            barrier.wait()
            r = reg.submit(s, enterprise_name="Acme", contact_email="ana@example.com",
                           product_ids=[sms.id], submission_key="key-conc-0001")  # fmt: skip
            s.commit()
            results.append((r.created, r.access_token is not None, r.request.id))

    run_threads([worker] * 6)
    assert sum(1 for c, _t, _i in results if c) == 1 and sum(1 for _c, t, _i in results if t) == 1
    with Session(pg) as s:
        assert count(s, RegistrationRequest) == 1 and count(s, AuditLog, REG) == 1
        row = s.scalar(select(RegistrationRequest))
        assert (row.status, row.decision_mode, row.decided_by_label) == (
            "approved",
            "automatic",
            AUTO,
        )


# --- porta e sigurisë ---------------------------------------------------------------------------------------------


def test_default_config_blocks_unverified_automatic_registration_in_both_places(w, monkeypatch):
    db, sms, _, admin = w
    assert settings.allow_unverified_auto_registration is False  # default
    policy(db, sms, admin, self_registration_enabled=True)
    with pytest.raises(errors.Conflict):
        pol.set_policy(db, sms.id, admin, approval_mode="automatic")  # 1) s'vendoset
    db.rollback()
    db.execute(
        text("update product_registration_policy set approval_mode = 'automatic'")
    )  # forco jashtë shërbimit
    db.commit()
    row = submit(db, [sms]).request  # 2) as submit-i s'auto-miraton: kthehet te manual
    assert (row.status, row.decision_mode) == ("submitted", None)
    assert count(db, AuditLog, REG) == 0


def test_explicit_gate_enables_automatic_behavior_and_claims_no_contact_verification(auto):
    db, sms, _, _ = auto
    assert submit(db, [sms]).request.status == "approved"  # gate eksplicit (fixture)
    cols = {c.name for c in RegistrationRequest.__table__.columns}
    assert not [
        c for c in cols if "token" in c and "verif" in c
    ]  # M8-e: s'ruhet kurrë token verifikimi
    assert "unverified" in (ROOT / "apps/central/core/config.py").read_text()


def test_production_refuses_to_start_with_the_unverified_gate_open(monkeypatch):
    from apps.central import main
    from apps.central.core import tokens

    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "auth_secret", "x" * 64)
    monkeypatch.setattr(tokens, "configured", lambda: True)
    monkeypatch.setattr(settings, "allow_unverified_auto_registration", True)
    with pytest.raises(RuntimeError, match="UNVERIFIED_AUTO_REGISTRATION"):
        main.create_app(create_engine("sqlite://"))
    monkeypatch.setattr(settings, "allow_unverified_auto_registration", False)
    assert main.create_app(create_engine("sqlite://")) is not None


def test_no_email_or_network_is_used_by_registration_modules():
    for rel in ("models/registration.py", "models/registration_policy.py",
                "services/registrations.py", "services/registration_policy.py"):  # fmt: skip
        tree = ast.parse((ROOT / "apps/central" / rel).read_text())
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not [
            m
            for m in mods
            if m.split(".")[0]
            in {"smtplib", "email", "httpx", "requests", "socket", "urllib", "ssl"}
        ], rel
        assert not [m for m in mods if m == "app" or m.startswith("app.")], (
            rel
        )  # Central ↛ Enterprise


# --- kufij ----------------------------------------------------------------------------------------------------------


def test_policy_table_has_only_the_approved_columns_and_no_money_or_sender_fields(w):
    cols = {c.name for c in ProductRegistrationPolicy.__table__.columns}
    assert cols == {
        "product_id",
        "self_registration_enabled",
        "approval_mode",
        "created_at",
        "updated_at",
    }
    assert not {"requires_payment_approval", "requires_sid_registration", "country", "provisioning_mode",
                "price", "external_account"} & cols  # fmt: skip
    assert "product_registration_policy" in Base.metadata.tables
    assert not any(n.startswith("sms_") for n in Base.metadata.tables)


def test_no_registration_routes_provisioning_service_or_m7_coupling(w):
    from apps.central.main import create_app

    db = w[0]
    paths = create_app(db.get_bind()).openapi()["paths"]
    # M8-d: rrugët ekzistojnë (publike + admin), por asnjë DELETE dhe asnjë thirrje drejt Enterprise
    assert not [p for p in paths if "registr" in p.lower() and "delete" in paths[p]]
    services = {p.stem for p in (ROOT / "apps/central/services").glob("*.py")}
    assert (
        "provisioning" in services
    )  # M8-c: service ekziston; HTTP provisioning vjen vetëm në M8-d
    src = (ROOT / "apps/central/services/registrations.py").read_text()
    assert (
        "enterprise_products" not in src
        and "assign_product" not in src
        and "sync" not in src.split('"""')[2]
    )
    for f in (
        "sync.py",
        "sync_contract.py",
        "sync_feed.py",
        "service_auth.py",
    ):  # M7 s'varet nga regjistrimi
        text_ = (ROOT / "apps/central/services" / f).read_text()
        assert "registration" not in text_, f


def test_enterprise_schema_and_migrations_are_untouched_by_m8b(w):
    from app.core.db import Base as EnterpriseBase

    assert not set(Base.metadata.tables) & set(EnterpriseBase.metadata.tables)
    versions = sorted(p.name for p in (ROOT / "alembic/versions").glob("0*.py"))
    assert versions[-1].startswith("0021")  # koka e Enterprise e pandryshuar nga M7-g


# --- migrimi -----------------------------------------------------------------------------------------------------------


def test_migration_0014_up_down_up_readiness_and_metadata(make_db):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    url = make_db("central")
    central_alembic(url, "upgrade", "0013")
    eng = create_engine(url)
    assert "product_registration_policy" not in set(inspect(eng).get_table_names())
    assert readiness.check(eng) is not None  # prapa kokës
    central_alembic(url, "upgrade", "0014")
    assert "product_registration_policy" in set(inspect(eng).get_table_names())
    assert readiness.check(eng) is not None  # 0015 (M8-c) është tani koka
    central_alembic(url, "downgrade", "0013")
    assert "product_registration_policy" not in set(inspect(eng).get_table_names())
    central_alembic(url, "upgrade", "head")
    with eng.connect() as c:
        ctx = MigrationContext.configure(
            c, opts={"compare_type": True, "version_table": "central_alembic_version"}
        )
        assert compare_metadata(ctx, Base.metadata) == []
        assert c.execute(text("select version_num from central_alembic_version")).scalar() == "0016"
    assert readiness.check(eng) is None
    eng.dispose()


def test_migration_0015_adds_auto_grant_flag_default_false_and_is_reversible(make_db):
    url = make_db("central")
    central_alembic(url, "upgrade", "0014")
    eng = create_engine(url)
    assert "auto_grant_new_enterprises" not in {
        c["name"] for c in inspect(eng).get_columns("service_clients")
    }
    with eng.begin() as c:
        c.execute(
            text(
                "insert into service_clients (id, client_id, status, scopes, auth_generation, created_at, updated_at) "
                "values ('00000000-0000-0000-0000-000000000001', 'old', 'active', '[]', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    central_alembic(url, "upgrade", "0015")
    col = {c["name"]: c for c in inspect(eng).get_columns("service_clients")}[
        "auto_grant_new_enterprises"
    ]
    assert col["nullable"] is False
    with eng.connect() as c:
        assert not c.execute(
            text("select auto_grant_new_enterprises from service_clients")
        ).scalar()  # ekzistuesit: false
    central_alembic(url, "downgrade", "0014")
    assert "auto_grant_new_enterprises" not in {
        c["name"] for c in inspect(eng).get_columns("service_clients")
    }
    central_alembic(url, "upgrade", "head")
    assert readiness.check(eng) is None
    eng.dispose()


def test_server_defaults_are_closed(w):
    db, sms, *_ = w
    db.execute(text("insert into product_registration_policy (product_id, created_at, updated_at) "
                    "values (:p, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
               {"p": sms.id.hex if db.get_bind().dialect.name == "sqlite" else sms.id})  # fmt: skip
    db.commit()
    row = db.get(ProductRegistrationPolicy, sms.id, populate_existing=True)
    assert (row.self_registration_enabled, row.approval_mode) == (False, "manual")


def test_central_user_is_not_a_customer_identity():
    assert "registration" not in {c.name for c in CentralUser.__table__.columns}
