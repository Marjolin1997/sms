"""Gatishmëria e faturimit (M9-g2). VETËM LEXIM.

    python -m apps.central.tools.billing_readiness [--json] [--strict]

Dalja: PASS | WARN | FAIL + një rresht per kontroll. Kodi: 0 PASS (ose WARN pa `--strict`) · 1 FAIL (ose WARN me `--strict`) · 2 gabim i brendshëm."""

import argparse
import json
import sys
from dataclasses import asdict

from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.services import billing_readiness as br


def main(argv: list[str] | None = None, engine=None) -> int:
    ap = argparse.ArgumentParser(description="Billing readiness (read-only).")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)
    try:
        engine = engine or make_engine(settings.database_url)
        with Session(engine) as db:
            items = br.checks(db)
            detail = br.summary(db)
            db.rollback()
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {type(e).__name__}", file=sys.stderr)  # noqa: T201
        return 2
    status = br.overall(items)
    if args.json:
        print(
            json.dumps(
                {
                    "status": status,
                    "checks": [asdict(c) for c in items],
                    "summary": detail,
                },
                sort_keys=True,
            )
        )
    else:
        print(status)  # noqa: T201
        for c in items:
            print(f"  {c.level} {c.name}: {c.reason}")  # noqa: T201
    return 1 if status == br.FAIL or (args.strict and status == br.WARN) else 0


if __name__ == "__main__":
    sys.exit(main())
