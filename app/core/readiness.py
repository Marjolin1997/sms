"""Gatishmëria: DB përgjigjet dhe skema është në versionin që e pret kodi."""

from pathlib import Path

from sqlalchemy import inspect, text

from app.core.db import SessionLocal, engine

# Duhet të përputhet me `version_table` te alembic/env.py (kontrolluar nga testi).
VERSION_TABLE = "sms_alembic_version"


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
            if not inspect(engine).has_table(VERSION_TABLE):
                return None  # dev/test pa Alembic (create_all)
            current = db.execute(text(f"select version_num from {VERSION_TABLE}")).scalar()  # noqa: S608
    except Exception:
        return "database unavailable"
    head = _head()
    if head and current != head:
        return "database schema is not at the expected version (run migrations)"
    return None
