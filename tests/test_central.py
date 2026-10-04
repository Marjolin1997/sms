"""M4-a — Central: skelet i pavarur (app, DB, migrime, version table). Asnjë tabelë biznesi."""

import ast
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from apps.central.core import readiness
from apps.central.core.db import VERSION_TABLE, Base
from apps.central.main import create_app

ROOT = Path(__file__).resolve().parents[1]
PG_URL = os.environ.get("SMS_TEST_DATABASE_URL", "")
IS_PG = PG_URL.startswith("postgresql")
CENTRAL_INI = "apps/central/alembic.ini"


def central_alembic(url, *args):
    env = {**os.environ, "CENTRAL_DATABASE_URL": url}
    r = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", CENTRAL_INI, *args],
        env=env, cwd=ROOT, capture_output=True, text=True,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    return r


def enterprise_alembic(url, *args):
    env = {**os.environ, "SMS_DATABASE_URL": url}
    r = subprocess.run(
        [sys.executable, "-m", "alembic", *args], env=env, cwd=ROOT, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    return r


def drop_database(admin, name, attempts=8):
    """DROP DATABASE WITH (FORCE) pa superuser mund të dështojë nëse autovacuum (role postgres) ka
    sesion të hapur te DB e re ("permission denied to terminate process"): riprovo shkurt."""
    import time

    from sqlalchemy.exc import ProgrammingError

    for i in range(attempts):
        try:
            with admin.connect() as c:
                c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
            return
        except ProgrammingError as e:
            if "terminate process" not in str(e) or i == attempts - 1:
                raise
            time.sleep(0.5)


@pytest.fixture(params=["sqlite", "postgres"])
def make_db(request, tmp_path):
    """Fabrikë DB-sh logjike të veçanta (SQLite: skedarë; PG: databaza të reja)."""
    if request.param == "postgres" and not IS_PG:
        pytest.skip("needs PostgreSQL")
    created, admin = [], None
    if request.param == "postgres":
        admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")

    def make(prefix="central"):
        if request.param == "postgres":
            name = f"sms_{prefix}_{uuid.uuid4().hex[:8]}"
            with admin.connect() as c:
                c.execute(text(f'CREATE DATABASE "{name}"'))
            created.append(name)
            return make_url(PG_URL).set(database=name).render_as_string(hide_password=False)
        return f"sqlite:///{tmp_path / (prefix + '_' + uuid.uuid4().hex[:6] + '.db')}"

    yield make
    for name in created:
        drop_database(admin, name)


def client_for(url):
    eng = create_engine(url)
    return TestClient(create_app(eng)), eng


# --- app i pavarur ---------------------------------------------------------------------------


def test_central_imports_without_loading_any_enterprise_module():
    code = (
        "import sys\n"
        "import apps.central.main\n"
        "bad = [m for m in sys.modules if m == 'app' or m.startswith('app.')]\n"
        "assert not bad, bad\n"
        "print('OK')\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SMS_", "CENTRAL_"))}
    r = subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=ROOT, capture_output=True, text=True
    )
    assert "OK" in r.stdout, r.stdout + r.stderr


def test_healthz_is_200_without_any_database():
    c, _ = client_for("sqlite:////nonexistent-dir/none/central.db")
    r = c.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_readyz_is_503_when_the_database_is_unavailable():
    c, _ = client_for("sqlite:////nonexistent-dir/none/central.db")
    r = c.get("/readyz")
    assert r.status_code == 503 and r.json() == {
        "status": "not_ready", "reason": "database unavailable",
    }  # fmt: skip


def test_central_has_only_health_routes_and_no_docs():
    paths = set(create_app(create_engine("sqlite://")).openapi()["paths"])
    assert paths == {
        "/healthz", "/readyz", "/auth/token", "/auth/me", "/admin/ping",
        "/admin/products", "/admin/products/{product_id}",
        "/admin/enterprises/{enterprise_id}/products",
        "/admin/enterprises/{enterprise_id}/products/{assignment_id}",
        "/internal/sync/changes", "/internal/sync/snapshot",
        # M8-d: regjistrimi (publik + admin)
        "/registration", "/registration/products", "/registration/{registration_id}/status",
        "/admin/registrations", "/admin/registrations/{registration_id}",
        "/admin/registrations/{registration_id}/approve",
        "/admin/registrations/{registration_id}/reject",
        "/admin/registrations/{registration_id}/provision",
        "/admin/registration-policies", "/admin/products/{product_id}/registration-policy",
    }  # fmt: skip


# --- readiness: gjendjet e skemës --------------------------------------------------------------


def test_readyz_states_uninitialized_ready_unknown(make_db):
    url = make_db()
    c, eng = client_for(url)
    r = c.get("/readyz")
    assert r.status_code == 503 and "not initialized" in r.json()["reason"]
    central_alembic(url, "upgrade", "head")
    assert c.get("/readyz").json() == {"status": "ready"}
    with eng.begin() as conn:  # revision i panjohur / "më përpara" se kodi
        conn.execute(text(f"update {VERSION_TABLE} set version_num = 'zz_future'"))
    r = c.get("/readyz")
    assert r.status_code == 503 and "unknown" in r.json()["reason"]
    with eng.begin() as conn:  # version table bosh
        conn.execute(text(f"delete from {VERSION_TABLE}"))
    assert c.get("/readyz").status_code == 503


def test_readyz_is_503_when_schema_is_behind_a_newer_head(make_db, tmp_path):
    url = make_db()
    central_alembic(url, "upgrade", "head")
    scripts = tmp_path / "migrations"
    shutil.copytree(
        ROOT / "apps/central/migrations", scripts, ignore=shutil.ignore_patterns("__pycache__")
    )
    (scripts / "versions" / "0016_next.py").write_text(
        'revision = "0016"\ndown_revision = "0015"\nbranch_labels = None\ndepends_on = None\n\n\n'
        "def upgrade() -> None:\n    pass\n\n\ndef downgrade() -> None:\n    pass\n"
    )
    eng = create_engine(url)
    assert readiness.check(eng) is None  # kodi aktual: në kokë
    reason = readiness.check(eng, scripts)  # kod më i ri: DB mbetet prapa
    assert reason and "not at the expected version" in reason


# --- version table, metadata ------------------------------------------------------------------


def test_central_uses_its_own_version_table_and_only_central_tables(make_db):
    url = make_db()
    assert VERSION_TABLE == "central_alembic_version"
    central_alembic(url, "upgrade", "0001")  # baseline: vetëm version table
    assert set(inspect(create_engine(url)).get_table_names()) == {"central_alembic_version"}
    central_alembic(url, "upgrade", "head")
    tables = set(inspect(create_engine(url)).get_table_names())
    assert tables == {
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
    }
    assert not any(t.startswith("sms_") for t in tables)
    central_alembic(url, "downgrade", "base")
    central_alembic(url, "upgrade", "head")  # up/down/up


def test_central_metadata_is_independent_from_enterprise_metadata():
    import app.models  # noqa: F401
    from app.core.db import Base as EnterpriseBase

    assert Base is not EnterpriseBase and Base.metadata is not EnterpriseBase.metadata
    assert set(Base.metadata.tables) == {
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
    }
    assert "enterprises" not in EnterpriseBase.metadata.tables
    assert "sms_enterprises" not in Base.metadata.tables
    assert not set(Base.metadata.tables) & set(EnterpriseBase.metadata.tables)
    assert not any(t.startswith("sms_") for t in Base.metadata.tables)


# --- izolimi i DB-ve (PostgreSQL) -----------------------------------------------------------------


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_enterprise_and_central_databases_do_not_affect_each_other(make_db):
    ent_url, cen_url = make_db("ent"), make_db("central")
    enterprise_alembic(ent_url, "upgrade", "head")
    central_alembic(cen_url, "upgrade", "head")
    ent, cen = create_engine(ent_url), create_engine(cen_url)
    ent_tables = set(inspect(ent).get_table_names())
    assert "sms_alembic_version" in ent_tables and "central_alembic_version" not in ent_tables
    assert set(inspect(cen).get_table_names()) == {
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
    }
    assert "enterprises" not in ent_tables and not any(
        t.startswith("sms_") for t in inspect(cen).get_table_names()
    )
    ent_rev = text("select version_num from sms_alembic_version")
    with ent.connect() as c:
        ent_head = c.execute(ent_rev).scalar()
    assert readiness.check(cen) is None

    enterprise_alembic(ent_url, "downgrade", "-1")  # Enterprise në revision tjetër
    with ent.connect() as c:
        assert c.execute(ent_rev).scalar() != ent_head
    assert readiness.check(cen) is None  # Central i padukshëm ndaj Enterprise

    ent_before = set(inspect(ent).get_table_names())
    central_alembic(cen_url, "downgrade", "base")  # Central poshtë: Enterprise i paprekur
    assert set(inspect(ent).get_table_names()) == ent_before
    with ent.connect() as c:
        assert c.execute(ent_rev).scalar() != ent_head  # ende në revision-in e vet
    assert readiness.check(cen) is not None  # base: skema e Central prapa

    # Central i drejtuar gabimisht te DB e Enterprise: s'është gati dhe nuk e ndryshon
    assert "not initialized" in readiness.check(ent)
    assert set(inspect(ent).get_table_names()) == ent_before
    assert "central_alembic_version" not in ent_before


# --- guard-e AST -------------------------------------------------------------------------------


def _import_roots(path: Path):
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.ImportFrom):
            assert n.level == 0 or path.parts[-2] != "central", "importe relative nuk përdoren"
            if n.module:
                yield n.module
        elif isinstance(n, ast.Import):
            yield from (a.name for a in n.names)


def test_central_imports_nothing_from_the_enterprise_app_package():
    files = list((ROOT / "apps").rglob("*.py"))
    assert len(files) >= 10
    for f in files:
        for mod in _import_roots(f):
            assert mod != "app" and not mod.startswith("app."), (
                f,
                mod,
            )  # as contracts/models/services


def test_enterprise_code_never_imports_central():
    for base in ("app", "alembic", "scripts"):
        for f in (ROOT / base).rglob("*.py"):
            for mod in _import_roots(f):
                assert mod != "apps" and not mod.startswith("apps."), (f, mod)


def test_central_settings_are_isolated_from_enterprise_settings():
    from apps.central.core.config import Settings

    assert Settings.model_config["env_prefix"] == "CENTRAL_"
    assert Settings.model_config["env_file"] == ".env.central"
    assert set(Settings.model_fields) == {
        "env",
        "database_url",
        "db_pool_size",
        "db_statement_timeout_ms",
        "auth_secret",
        "auth_ttl_seconds",
        "allow_unverified_auto_registration",
        "public_registration_enabled",
        "public_registration_max_per_email_24h",
    }
    assert Settings().database_url != "sqlite:///./sms_dev.db"
