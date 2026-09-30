"""Gatishmëria: DB përgjigjet dhe skema është në versionin që e pret kodi."""

from pathlib import Path

from sqlalchemy import inspect, text

from app.core.db import SessionLocal, engine


def _head() -> str | None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parents[2]
    ini = root / "alembic.ini"
    if not ini.exists():
        return None
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(root / "alembic"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def check() -> str | None:
    """→ None nëse gati, përndryshe arsyeja (pa detaje të brendshme)."""
    try:
        with SessionLocal() as db:
            db.execute(text("select 1"))
            if not inspect(engine).has_table("alembic_version"):
                return None  # dev/test pa Alembic (create_all)
            current = db.execute(text("select version_num from alembic_version")).scalar()
    except Exception:
        return "database unavailable"
    head = _head()
    if head and current != head:
        return "database schema is not at the expected version (run migrations)"
    return None
