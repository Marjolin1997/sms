"""Gatishmëria e Central: DB përgjigjet dhe skema e Central është te koka e migrimeve të Central.

Vetëm gjendja e `central_alembic_version`; asgjë nga Enterprise (owner_ref, queue, wallet).
"""

from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from apps.central.core.db import VERSION_TABLE

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def check(engine: Engine, migrations_dir: Path = MIGRATIONS_DIR) -> str | None:
    """→ None nëse gati, përndryshe arsyeja (pa detaje të brendshme)."""
    try:
        with engine.connect() as conn:
            conn.execute(text("select 1"))
            if not inspect(conn).has_table(VERSION_TABLE):
                return "schema not initialized (run central migrations)"
            current = conn.execute(text(f"select version_num from {VERSION_TABLE}")).scalar()  # noqa: S608
    except Exception:
        return "database unavailable"
    try:
        script = ScriptDirectory(str(migrations_dir))
        heads = script.get_heads()
        if len(heads) != 1:
            return "migration scripts have multiple heads"
        if current == heads[0]:
            return None
        if current is None:
            return "database schema is not at the expected version (run migrations)"
        known = script.get_revision(current) is not None
    except Exception:
        known = False
    if known:
        return "database schema is not at the expected version (run migrations)"
    return "database schema revision is unknown to this version of the code"
