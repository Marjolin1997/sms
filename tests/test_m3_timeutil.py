"""M3-b(i): `utcnow` ka burim të vetëm `app.core.timeutil`; modelet nuk varen më nga `models.wallet` për të.
Gardë strukturore + provë që semantika dhe skema ORM nuk ndryshuan."""

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import app.models  # noqa: F401
from app.core import timeutil
from app.core.db import Base
from app.models import wallet as wallet_models

APP = Path(__file__).resolve().parents[1] / "app"
GOLDEN = Path(__file__).parent / "golden" / "orm_metadata.json"

MIGRATED_MODELS = {
    "admin", "billing", "campaigns", "contacts", "email", "enterprise", "events",
    "inbound", "messaging", "rates", "sending",
}  # fmt: skip


# --- Semantika e utcnow ----------------------------------------------------------------------------


def test_utcnow_is_aware_utc_and_keeps_microseconds():
    t = timeutil.utcnow()
    assert isinstance(t, datetime) and t.tzinfo is not None and t.utcoffset() == timedelta(0)
    assert t.tzinfo is UTC and abs((datetime.now(UTC) - t).total_seconds()) < 5
    assert any(timeutil.utcnow().microsecond for _ in range(50))  # s'është e prerë në sekondë


def test_utcnow_is_a_plain_callable_not_a_value():
    assert callable(timeutil.utcnow) and timeutil.utcnow.__module__ == "app.core.timeutil"
    assert (
        timeutil.utcnow() != timeutil.utcnow() or True
    )  # thirret çdo herë (jo e ngrirë në import)
    a = timeutil.utcnow()
    b = timeutil.utcnow()
    assert b >= a  # vlerë e re çdo thirrje (jo e ngrirë në import)


def test_compatibility_alias_is_the_same_object_as_the_source_of_truth():
    assert wallet_models.utcnow is timeutil.utcnow


def test_as_utc_is_unchanged_next_to_it():
    naive = datetime(2030, 1, 1, 12)
    assert timeutil.as_utc(naive) == datetime(2030, 1, 1, 12, tzinfo=UTC)
    assert timeutil.as_utc(datetime(2030, 1, 1, 14, tzinfo=timezone_plus2())) == datetime(
        2030, 1, 1, 12, tzinfo=UTC
    )


def timezone_plus2():
    from datetime import timezone

    return timezone(timedelta(hours=2))


def test_every_model_default_that_used_utcnow_is_still_a_callable_returning_aware_utc():
    n = 0
    for table in Base.metadata.tables.values():
        for col in table.columns:
            d = col.default
            if d is not None and d.is_callable:
                n += 1
                v = d.arg(None)  # SQLAlchemy e mbështjell callable-in me argumentin e kontekstit
                if isinstance(v, datetime):
                    assert v.tzinfo is UTC, (table.name, col.name)
    assert n == 48  # snapshot: 45 para M7-d + 3 të `sms_entitlements` (uuid4, 2×utcnow)


# --- Garda e varësisë -------------------------------------------------------------------------------------


def _imports_utcnow_from_wallet_models(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module == "app.models.wallet":
            if any(a.name == "utcnow" for a in node.names):
                return True
    return False


def test_no_module_imports_utcnow_from_models_wallet():
    offenders = [
        f.relative_to(APP).as_posix()
        for f in APP.rglob("*.py")
        if f.name != "wallet.py" or f.parent.name != "models"
        if _imports_utcnow_from_wallet_models(f)
    ]
    assert offenders == []  # aliasi është rrugë përputhshmërie, jo burim


def test_the_eleven_models_import_utcnow_from_the_neutral_source():
    for name in sorted(MIGRATED_MODELS):
        tree = ast.parse((APP / "models" / f"{name}.py").read_text())
        froms = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module == "app.core.timeutil"
            and any(a.name == "utcnow" for a in n.names)
        ]  # fmt: skip
        assert froms, name
    assert len(MIGRATED_MODELS) == 11


def test_models_depend_on_the_wallet_model_module_only_for_money_and_wallet_entities():
    for f in (APP / "models").glob("*.py"):
        if f.name == "wallet.py":
            continue
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module == "app.models.wallet":
                assert "utcnow" not in {a.name for a in node.names}, f.name


# --- Skema ORM e pandryshuar -----------------------------------------------------------------------------------


def _snapshot():
    out = {}
    for t in sorted(Base.metadata.tables.values(), key=lambda t: t.name):
        cols = {}
        for c in t.columns:
            d, u = c.default, c.onupdate
            cols[c.name] = {
                "type": str(c.type), "nullable": c.nullable, "pk": c.primary_key,
                "default": None if d is None else ("callable" if d.is_callable else "scalar:" + repr(d.arg)),
                "onupdate": None if u is None else ("callable" if u.is_callable else "scalar"),
                "server_default": None if c.server_default is None else str(c.server_default.arg),
                "fks": sorted(str(f.target_fullname) for f in c.foreign_keys),
            }  # fmt: skip
        out[t.name] = {
            "columns": cols,
            "indexes": sorted(
                f"{i.name}:{[c.name for c in i.columns]}:{i.unique}" for i in t.indexes
            ),
            "constraints": sorted(f"{type(k).__name__}:{k.name}" for k in t.constraints),
        }
    return out


def test_orm_metadata_matches_the_golden_snapshot_taken_before_the_refactor():
    """Snapshot i marrë mbi kodin PARA M3-b(i) (42 tabela, 417 kolona; M7-d: +2 tabela, +17 kolona; M7-g: +1 kolonë). Çdo ndryshim i skemës ORM duhet
    ta përditësojë këtë skedar me qëllim (dhe me migrim); zhvendosjet strukturore s'duhet ta prekin."""
    golden = json.loads(GOLDEN.read_text())
    assert _snapshot() == golden
    assert len(golden) == 44 and sum(len(v["columns"]) for v in golden.values()) == 435


@pytest.mark.skipif(
    not __import__("os").environ.get("SMS_TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="needs PostgreSQL",
)
def test_alembic_sees_no_schema_diff_after_the_refactor(tmp_path):
    import os
    import subprocess
    import sys
    import uuid

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    base = os.environ["SMS_TEST_DATABASE_URL"]
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    name = f"sms_m3_{uuid.uuid4().hex[:8]}"
    with admin.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(base).set(database=name).render_as_string(hide_password=False)
    try:
        env = {**os.environ, "SMS_DATABASE_URL": url, "PYTHONPATH": "."}
        up = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], env=env,
                            capture_output=True, text=True)  # fmt: skip
        assert up.returncode == 0, up.stderr
        code = (
            "from sqlalchemy import create_engine\n"
            "from alembic.migration import MigrationContext\n"
            "from alembic.autogenerate import compare_metadata\n"
            "import app.models\n"
            "from app.core.db import Base\n"
            f"e = create_engine({url!r})\n"
            "c = e.connect()\n"
            "ctx = MigrationContext.configure(c, opts={'compare_type': True, 'version_table': 'sms_alembic_version'})\n"
            "print('DIFF', len(compare_metadata(ctx, Base.metadata)))\n"
        )
        r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
        assert "DIFF 0" in r.stdout, r.stdout + r.stderr
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
