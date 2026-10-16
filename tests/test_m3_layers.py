# ruff: noqa: F811
"""M3-c: kufijtë e shtresave (api → services → models → core), rruga e vetme e audit-it, resolveri i
Enterprise-it në shtresën e modeleve, dhe readiness pa varësi nga services. Pa ndryshim sjelljeje."""

import ast
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text

from app.core import context, readiness, scope
from app.core.context import SystemContext, TenantContext, TenantUnresolved, for_owner
from app.core.db import SessionLocal, engine
from app.core.tenancy import TenantMismatch
from app.main import create_app
from app.models import enterprise_registry as registry
from app.models.admin import AuditLog
from app.models.contacts import Contact
from app.models.enterprise import Enterprise
from app.services import audit as audit_svc
from app.services import enterprises
from app.services import wallet as wallets
from tests.test_pipeline import world  # noqa: F401
from tests.test_tenant_scoping import BOOT, ab, key_headers  # noqa: F401

APP = Path(__file__).resolve().parents[1] / "app"


# --- Grafi i importeve (top-level + lazy) ---------------------------------------------------------------------


def _modules():
    mods = {}
    for f in APP.rglob("*.py"):
        name = ".".join(("app",) + f.relative_to(APP).with_suffix("").parts)
        mods[name[:-9] if name.endswith(".__init__") else name] = f
    return mods


def _graph():
    mods = _modules()

    def resolve(c):
        while c and c not in mods:
            c = c.rsplit(".", 1)[0] if "." in c else ""
        return c or None

    graph = {}
    for name, f in mods.items():
        deps = set()
        for n in ast.walk(ast.parse(f.read_text())):
            cs = []
            if isinstance(n, ast.ImportFrom) and n.module:
                cs = [n.module] + [f"{n.module}.{a.name}" for a in n.names]
            elif isinstance(n, ast.Import):
                cs = [a.name for a in n.names]
            deps |= {r for c in cs if c.startswith("app") and (r := resolve(c)) and r != name}
        graph[name] = deps
    return graph


def _layer(mod):
    parts = mod.split(".")
    return parts[1] if len(parts) > 1 else "root"


def _edges(src_layer, dst_layer):
    return sorted(
        f"{m} -> {d}"
        for m, ds in _graph().items()
        if _layer(m) == src_layer
        for d in ds
        if _layer(d) == dst_layer
    )


def test_core_never_imports_services_or_api():
    assert _edges("core", "services") == []  # 0 përjashtime (lazy përfshirë)
    assert _edges("core", "api") == []


def test_models_never_import_services_or_api():
    assert _edges("models", "services") == []
    assert _edges("models", "api") == []


def test_services_never_import_api_and_queue_imports_nothing():
    assert _edges("services", "api") == []
    assert [d for d in _graph().get("app.queue.dispatch", ())] == []
    assert all(not d.startswith("app.") or d.startswith("app.queue") for m, ds in _graph().items()
               if m.startswith("app.queue") for d in ds)  # fmt: skip


def test_core_to_models_edges_are_an_explicit_shrinking_allowlist():
    """Borxh i dokumentuar (emërtimi/pronësia e `core/*` Enterprise-specifik): çdo edge i ri dështon këtu."""
    allowed = {
        "app.core.security -> app.models.admin",  # auth me ApiKey/AuthFailure ORM (Enterprise)
        "app.core.tenancy -> app.models.tenant",  # dual-write mbi TenantOwned (Enterprise)
        "app.core.tenancy -> app.models.enterprise_registry",
        "app.core.context -> app.models.enterprise_registry",
        "app.core.readiness -> app.models.enterprise_registry",  # vetëm LEGACY_OWNER_TABLES
    }
    assert set(_edges("core", "models")) <= allowed


def test_no_import_cycles_even_counting_lazy_imports():
    graph = _graph()
    state, stack, cycles = {}, [], []

    def dfs(v):
        state[v] = 1
        stack.append(v)
        for w in graph[v]:
            if state.get(w) == 1:
                cycles.append(stack[stack.index(w) :] + [w])
            elif w not in state:
                dfs(w)
        stack.pop()
        state[v] = 2

    for v in graph:
        if v not in state:
            dfs(v)
    assert cycles == []


def test_wallet_to_events_is_a_same_layer_edge_left_for_later():
    """M3-later: wallet → services.events (lazy, domain event `wallet.low_balance`). Shtresë e njëjtë, pa cikël."""
    g = _graph()
    assert "app.services.events" in g["app.services.wallet"]
    assert "app.services.wallet" not in g["app.services.events"]


def test_models_init_still_registers_the_dual_write_listener():
    from sqlalchemy import event as sa_event
    from sqlalchemy.orm import Session

    import app.models  # noqa: F401
    from app.core.tenancy import _dual_write

    assert sa_event.contains(Session, "before_flush", _dual_write)


# --- Resolveri (shtresa e modeleve) --------------------------------------------------------------------------------


def test_compat_reexports_are_the_same_functions_as_the_registry():
    assert enterprises.resolve_id is registry.resolve_id
    assert enterprises.lookup_id is registry.lookup_id
    assert enterprises.valid_owner_ref is registry.valid_owner_ref
    assert enterprises.LEGACY_OWNER_TABLES is registry.LEGACY_OWNER_TABLES
    assert enterprises.log is registry.log and registry.log.name == "sms.enterprises"


def test_valid_owner_ref_resolves_creates_once_and_is_stable(db):
    a = registry.resolve_id(db, "acme")
    db.commit()
    assert a == db.scalar(select(Enterprise.id).where(Enterprise.owner_ref == "acme"))
    assert registry.resolve_id(db, "acme") == a and registry.lookup_id(db, "acme") == a
    with SessionLocal() as other:
        assert registry.lookup_id(other, "acme") == a
        assert db.scalar(select(func.count()).select_from(Enterprise)) == 1


def test_lookup_never_creates(db):
    assert registry.lookup_id(db, "ghost") is None
    assert db.scalar(select(func.count()).select_from(Enterprise)) == 0
    with pytest.raises(TenantUnresolved):
        for_owner(db, "ghost")
    assert for_owner(db, "ghost", create=True).owner_ref == "ghost"


@pytest.mark.parametrize("bad", ["", " acme", "acme ", "a\nb", "x" * 65, None, 5])
def test_invalid_owner_refs_resolve_to_none_without_normalization_or_creation(db, bad, caplog):
    with caplog.at_level(logging.WARNING, logger="sms.enterprises"):
        assert registry.resolve_id(db, bad) is None
    assert registry.lookup_id(db, bad) is None
    assert db.scalar(select(func.count()).select_from(Enterprise)) == 0
    if isinstance(bad, str):
        assert any("anomaly" in r.message for r in caplog.records)  # i njëjti log, i njëjti logger


def test_case_variant_is_not_merged_and_not_created(db, caplog):
    registry.resolve_id(db, "CLIENT_A")
    db.commit()
    with caplog.at_level(logging.WARNING, logger="sms.enterprises"):
        assert registry.resolve_id(db, "client_a") is None  # pa bashkim të heshtur
    assert db.scalar(select(func.count()).select_from(Enterprise)) == 1


def test_resolver_does_not_commit_rollback_removes_the_new_enterprise(db):
    registry.resolve_id(db, "tmp")
    assert db.in_transaction()
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Enterprise)) == 0


def test_mismatched_explicit_enterprise_id_still_fails(db):
    a = registry.resolve_id(db, "A")
    other = registry.resolve_id(db, "B")
    db.commit()
    from app.models.wallet import Wallet

    db.add(Wallet(owner_ref="A", enterprise_id=other, currency="EUR"))
    with pytest.raises(TenantMismatch):
        db.flush()
    db.rollback()
    assert a != other


def test_resolver_sql_counts_are_the_baseline(db):
    n = {"n": 0}

    def count(*_):
        n["n"] += 1

    registry.resolve_id(db, "seed")
    db.commit()

    def measure(fn):
        n["n"] = 0
        event.listen(engine, "before_cursor_execute", count)
        try:
            with SessionLocal() as s:
                fn(s)
        finally:
            event.remove(engine, "before_cursor_execute", count)
        return n["n"]

    assert measure(lambda s: registry.resolve_id(s, "seed")) == 1  # cold: një SELECT
    assert (
        measure(lambda s: (registry.resolve_id(s, "seed"), registry.resolve_id(s, "seed"))) == 1
    )  # warm: 0 shtesë
    assert measure(lambda s: registry.lookup_id(s, "seed")) == 1
    assert measure(lambda s: registry.resolve_id(s, "brand-new")) == 3  # SELECT + INSERT + SELECT


def test_registry_and_context_import_no_services():
    for f in (
        "models/enterprise_registry.py",
        "core/context.py",
        "core/tenancy.py",
        "core/readiness.py",
    ):
        text_ = (APP / f).read_text()
        assert "app.services" not in text_, f


# --- Audit: rruga e vetme ----------------------------------------------------------------------------------------------


def test_audit_log_rows_are_constructed_only_in_the_audit_service():
    offenders = []
    for f in APP.rglob("*.py"):
        rel = f.relative_to(APP).as_posix()
        if rel in {"services/audit.py", "models/admin.py"}:
            continue
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "AuditLog":
                offenders.append(rel)
    assert offenders == []
    assert not hasattr(scope, "cross_tenant")  # s'ka më rrugë të dytë në core


def test_cross_tenant_row_fields_are_exactly_as_before(db):
    ctx = SystemContext("staff:7", "support ticket 9")
    audit_svc.cross_tenant(db, ctx, "messages", "list", {"owner": "x"})
    db.commit()
    r = db.scalar(select(AuditLog))
    assert (r.actor, r.role, r.action, r.target_type, r.target_id) == (
        "staff:7", "system", "cross_tenant.list", "messages", "*")  # fmt: skip
    assert r.detail == '{"owner": "x", "reason": "support ticket 9"}'  # JSON me çelësa të renditur


def test_cross_tenant_dedup_window_is_unchanged(db):
    ctx = SystemContext("staff:1", "r")
    for _ in range(3):
        audit_svc.cross_tenant(db, ctx, "messages", "list")
    db.commit()
    assert db.scalar(select(func.count()).select_from(AuditLog)) == 1  # brenda 5 min: një rresht
    audit_svc.cross_tenant(db, SystemContext("staff:2", "r"), "messages", "list")  # aktor tjetër
    audit_svc.cross_tenant(db, ctx, "topups", "list")  # burim tjetër
    audit_svc.cross_tenant(db, ctx, "messages", "export")  # veprim tjetër
    db.commit()
    assert db.scalar(select(func.count()).select_from(AuditLog)) == 4
    db.execute(
        AuditLog.__table__.update().values(created_at=datetime.now(UTC) - timedelta(minutes=6))
    )
    db.commit()
    audit_svc.cross_tenant(db, ctx, "messages", "list")  # jashtë dritares → rresht i ri
    db.commit()
    assert db.scalar(select(func.count()).select_from(AuditLog)) == 5
    assert audit_svc.DEDUP_MINUTES == 5


def test_audit_rows_share_the_business_transaction(db):
    audit_svc.cross_tenant(db, SystemContext("staff:1", "r"), "messages", "list")
    assert db.in_transaction()
    db.rollback()
    assert db.scalar(select(func.count()).select_from(AuditLog)) == 0  # rollback → pa audit


def test_audit_requires_an_explicit_system_context(db):
    ctx = TenantContext(registry.resolve_id(db, "t"), "t")
    with pytest.raises(TypeError):
        audit_svc.cross_tenant(db, ctx, "messages", "list")  # type: ignore[arg-type]


def test_principal_audit_format_is_unchanged(db):
    from app.core.security import Principal

    audit_svc.audit(
        db, Principal("key:abc", "superadmin"), "plan.upsert", "plan", 5, {"b": 1, "a": 2}
    )
    audit_svc.audit(db, Principal("key:abc", "superadmin"), "plan.upsert", "plan", "x")
    db.commit()
    a, b = db.scalars(select(AuditLog).order_by(AuditLog.id)).all()
    assert (a.actor, a.role, a.action, a.target_type, a.target_id) == (
        "key:abc", "superadmin", "plan.upsert", "plan", "5")  # fmt: skip
    assert a.detail == '{"a": 2, "b": 1}' and b.detail is None


def test_tenant_requests_never_write_cross_tenant_audit_rows(ab, db):
    c, a, b, _ = ab
    for path in ("/v1/sender-ids", "/v1/templates", "/v1/wallets", "/v1/contacts"):
        assert c.get(path, headers=a).status_code == 200
    assert (
        db.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action.like("cross_tenant.%"))
        )
        == 0
    )
    c.get("/v1/sender-ids", headers=BOOT)  # staf, ndër-tenant → audit
    c.get("/v1/admin/accounts", headers=BOOT)
    db.expire_all()
    actions = {
        r.target_type
        for r in db.scalars(select(AuditLog).where(AuditLog.action.like("cross_tenant.%")))
    }
    assert actions == {"sender_ids", "accounts"}


# --- Readiness ---------------------------------------------------------------------------------------------------------------


def test_readyz_is_503_when_the_database_is_unavailable(client, monkeypatch):
    class Boom:
        def __enter__(self):
            raise RuntimeError("db down")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(readiness, "SessionLocal", lambda: Boom())
    r = client.get("/readyz")
    assert r.status_code == 503 and r.json() == {
        "status": "not_ready",
        "reason": "database unavailable",
    }


def test_readiness_uses_the_models_layer_constant_and_no_service():
    assert "app.services" not in (APP / "core" / "readiness.py").read_text()
    assert (
        readiness.__dict__.get("LEGACY_OWNER_TABLES") is None
    )  # importohet lazy brenda funksionit
    _ = (context, time, text, wallets, TestClient, create_app, Contact)
