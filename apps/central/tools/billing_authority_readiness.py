"""Gatishmëria e cutover-it të faturimit drejt Central (M9-g4). VETËM LEXIM, pa PII.

    python -m apps.central.tools.billing_authority_readiness [--json] [--strict]

Kodi: 0 PASS (ose WARN pa `--strict`) · 1 FAIL (ose WARN me `--strict`) · 2 gabim i brendshëm. Pjesa Enterprise: `python -m scripts.billing_authority_readiness`."""

import argparse
import json
import sys
from dataclasses import asdict

from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.services import billing_authority as ba


def main(argv: list[str] | None = None, engine=None) -> int:
    ap = argparse.ArgumentParser(description="Billing authority readiness (Central, read-only).")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)
    try:
        engine = engine or make_engine(settings.database_url)
        with Session(engine) as db:
            items = ba.readiness(db)
            mode = ba.mode(db)
            db.rollback()
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {type(e).__name__}", file=sys.stderr)  # noqa: T201
        return 2
    status = ba.overall(items)
    if args.json:
        print(
            json.dumps(
                {"status": status, "mode": mode, "checks": [asdict(c) for c in items]},
                sort_keys=True,
            )
        )  # noqa: T201
    else:
        print(f"{status} (mode={mode})")  # noqa: T201
        for c in items:
            print(f"  {c.level} {c.name}: {c.reason}")  # noqa: T201
    return 1 if status == ba.FAIL or (args.strict and status == ba.WARN) else 0


if __name__ == "__main__":
    sys.exit(main())
