"""Gatishmëria e sinkronizimit të sender-ave (M10-S2). VETËM LEXIM, pa PII, pa rrjet. Jo gatishmëria finale e autoritetit.

    python -m scripts.sender_sync_readiness [--json] [--strict]

Kodi: 0 PASS (ose WARN pa `--strict`) · 1 FAIL (ose WARN me `--strict`) · 2 gabim i brendshëm."""

import argparse
import json
import sys
from dataclasses import asdict

from app.core.db import SessionLocal
from app.services import sender_sync_readiness as sr


def main(argv: list[str] | None = None, factory=None) -> int:
    ap = argparse.ArgumentParser(description="Sender sync readiness (read-only).")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)
    try:
        with (factory or SessionLocal)() as db:
            items = sr.checks(db)
            st = sr.status(db)
            db.rollback()
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {type(e).__name__}", file=sys.stderr)  # noqa: T201
        return 2
    status = sr.overall(items)
    if args.json:
        print(
            json.dumps(
                {"status": status, "checks": [asdict(c) for c in items], "metrics": st},
                sort_keys=True,
                default=str,
            )
        )  # noqa: T201
    else:
        print(status)  # noqa: T201
        for c in items:
            print(f"  {c.level} {c.name}: {c.reason}")  # noqa: T201
    return 1 if status == sr.FAIL or (args.strict and status == sr.WARN) else 0


if __name__ == "__main__":
    sys.exit(main())
