"""M3-e — mbyllja: pronësia e moduleve të paketave të vogla + kufijtë e mbetur (guard-e të thjeshta).

Gate-et e tjera të M3 jetojnë tjetër: `test_m3_layers.py` (core/models/cikle), `test_queue_boundaries.py`,
`test_contracts_*.py`, `test_webhook_golden.py`, golden ORM, Alembic diff (PG). Shih docs/M3_AUDIT.md.
"""

import ast
from pathlib import Path

from tests.test_m3_layers import _graph, _layer

APP = Path(__file__).resolve().parents[1] / "app"

# Klasat: KERNEL (primitiva të qëndrueshme), CONTRACT, ENTERPRISE, INFRA, GATEWAY (logjik).
OWNERSHIP = {
    "core": {
        "errors": "KERNEL", "timeutil": "KERNEL", "totp": "KERNEL",
        "crypto": "INFRA", "config": "INFRA", "db": "INFRA", "openapi": "INFRA",
        "readiness": "INFRA",
        "context": "ENTERPRISE", "scope": "ENTERPRISE", "tenancy": "ENTERPRISE",
        "security": "ENTERPRISE", "texts": "ENTERPRISE",
    },
    "queue": {"dispatch": "INFRA", "delivery": "INFRA", "postgres": "INFRA"},
    "contracts": {"events": "CONTRACT", "signature": "CONTRACT"},
    "providers": {
        "base": "GATEWAY", "twilio": "GATEWAY", "email": "GATEWAY", "http": "GATEWAY",
        "fake": "GATEWAY", "payments": "GATEWAY",
    },
}  # fmt: skip


def test_every_module_of_the_small_packages_has_an_explicit_owner():
    for pkg, owners in OWNERSHIP.items():
        names = {p.stem for p in (APP / pkg).glob("*.py") if p.stem != "__init__"}
        assert names == set(owners), (pkg, names ^ set(owners))  # modul i ri → vendim pronësie


def test_providers_are_a_leaf_over_core_only():
    g = _graph()
    for m, deps in g.items():
        if _layer(m) == "providers":
            assert {_layer(d) for d in deps} <= {"core", "providers"}, (m, deps)


def test_queue_and_contracts_do_not_depend_on_any_app_layer():
    g = _graph()
    for m, deps in g.items():
        if _layer(m) in ("queue", "contracts"):
            assert {_layer(d) for d in deps} <= {_layer(m)}, (m, deps)


def test_nothing_imports_the_worker_or_main_entrypoints():
    for m, deps in _graph().items():
        assert not {"app.worker", "app.main"} & deps or m in {"app.worker", "app.main"}, m


def test_enterprise_registry_stays_a_persistence_identity_registry():
    """Lejohet: lookup/resolve/krijim ekzistues/cache/invariante. Jo biznes (çmime, miratime, billing, RBAC, HTTP)."""
    allowed = {
        "logging", "re", "uuid", "datetime", "sqlalchemy",
        "app.models.enterprise", "app.models.tenant",
    }  # fmt: skip
    src = (APP / "models" / "enterprise_registry.py").read_text()
    seen = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.ImportFrom):
            seen.add(n.module)
        elif isinstance(n, ast.Import):
            seen |= {a.name for a in n.names}
    seen = {
        m if m.startswith("app.") else m.split(".")[0] for m in seen
    }  # sqlalchemy.* → sqlalchemy
    assert seen <= allowed, seen - allowed
    funcs = {n.name for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)}
    assert funcs == {"valid_owner_ref", "_insert_if_absent", "lookup_id", "_from_loaded_rows",
                     "resolve_id"}, funcs  # funksion i ri = vendim i qëllimshëm për kufirin  # fmt: skip
