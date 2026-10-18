"""Retention i kontrolluar i `usage_reports` (M9-f). Dry-run parazgjedhje; `--apply` fshin dhe auditon.

    python -m apps.central.tools.retention [--json]            # vetëm plan, pa shkrim
    python -m apps.central.tools.retention --apply             # fshin sipas CENTRAL_USAGE_REPORT_*

Me `CENTRAL_USAGE_REPORT_RETENTION_DAYS=0` (parazgjedhja) nuk fshihet asgjë. Shih `apps/central/services/retention.py`
për atë që NUK preket kurrë. Kodet: 0 ok · 2 gabim."""

import argparse
import json
import sys

from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.services import retention


def main(argv: list[str] | None = None, engine=None) -> int:
    ap = argparse.ArgumentParser(description="usage_reports retention (dry-run by default).")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        engine = engine or make_engine(settings.database_url)
        with Session(engine, expire_on_commit=False) as db:
            p = retention.plan(db)
            deleted = 0
            if a.apply:
                deleted = retention.apply(db, p)
                db.commit()
            else:
                db.rollback()
    except Exception as e:  # noqa: BLE001
        print(f"retention failed: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)  # noqa: T201
        return 2
    doc = {**p.as_dict(), "applied": a.apply, "deleted": deleted}
    if not a.json:
        doc.pop("by_key")
    print(json.dumps(doc, indent=1) if a.json else f"{'APPLIED' if a.apply else 'DRY-RUN'} {doc}")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
