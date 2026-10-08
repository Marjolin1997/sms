"""Gatishmëria lokale e autoritetit të sender-ave në Central (M10-S1). VETËM LEXIM, pa PII, pa rrjet. Jo gatishmëria finale (vjen pas sync-ut).

    python -m apps.central.tools.sender_central_readiness [--json] [--strict]

Kodi: 0 PASS (ose WARN pa `--strict`) · 1 FAIL (ose WARN me `--strict`) · 2 gabim i brendshëm."""

import argparse
import json
import sys
from dataclasses import asdict

from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.services import sender_central_readiness as sr


def main(argv: list[str] | None = None, engine=None) -> int:
    ap = argparse.ArgumentParser(
        description="Central sender authority readiness (local, read-only)."
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)
    try:
        engine = engine or make_engine(settings.database_url)
        with Session(engine) as db:
            items = sr.checks(db)
            db.rollback()
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {type(e).__name__}", file=sys.stderr)  # noqa: T201
        return 2
    status = sr.overall(items)
    if args.json:
        print(json.dumps({"status": status, "checks": [asdict(c) for c in items]}, sort_keys=True))  # noqa: T201
    else:
        print(status)  # noqa: T201
        for c in items:
            print(f"  {c.level} {c.name}: {c.reason}")  # noqa: T201
    return 1 if status == sr.FAIL or (args.strict and status == sr.WARN) else 0


if __name__ == "__main__":
    sys.exit(main())
