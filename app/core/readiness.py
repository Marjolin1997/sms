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


_backfill_cache: tuple[float, str | None] = (0.0, None)
_BACKFILL_TTL_S = 60.0


def _unbackfilled(db) -> str | None:
    """M1c: me skopim `enterprise` rreshtat pa `enterprise_id` do të fshiheshin nga tenant-ët. Nëse
    ekzistojnë, aplikacioni nuk është gati (rruga: backfill, ose SMS_TENANT_SCOPING=owner_ref)."""
    import time

    from app.core.config import settings
    from app.services.enterprises import LEGACY_OWNER_TABLES

    global _backfill_cache
    if settings.tenant_scoping != "enterprise":
        return None
    now = time.monotonic()
    if now - _backfill_cache[0] < _BACKFILL_TTL_S:
        return _backfill_cache[1]
    have = set(inspect(engine).get_table_names())
    reason = None
    for t in LEGACY_OWNER_TABLES:
        if t not in have:
            continue
        # emri i tabelës vjen nga konstanta e mësipërme, jo nga input i jashtëm
        row = db.execute(
            text(f"select 1 from {t} where enterprise_id is null and owner_ref is not null limit 1")  # noqa: S608
        ).first()
        if row:
            reason = (
                "enterprise_id backfill incomplete (run scripts.backfill_enterprise_id, "
                "or set SMS_TENANT_SCOPING=owner_ref temporarily)"
            )
            break
    _backfill_cache = (now, reason)
    return reason


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
    try:
        with SessionLocal() as db:
            return _unbackfilled(db)
    except Exception:
        return "database unavailable"
