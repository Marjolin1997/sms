"""Gatishmëria e autoritetit të sender-ave (M10-S4). VETËM LEXIM, pa PII, pa rrjet.

    python -m scripts.sender_authority_readiness [--json] [--strict] [--min-samples N] [--window-hours H | --all-time]
    python -m scripts.sender_authority_readiness --rollback-to shadow|local [--accept-divergence] [--json]

Kodi: 0 PASS (ose WARN pa `--strict`) · 1 FAIL (ose WARN me `--strict`) · 2 gabim i brendshëm."""

import argparse
import json
import sys
from dataclasses import asdict

from app.core.db import SessionLocal
from app.services import sender_authority_readiness as ar


def main(argv: list[str] | None = None, factory=None) -> int:
    ap = argparse.ArgumentParser(description="Sender authority readiness (read-only).")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--min-samples", type=int, default=20)
    ap.add_argument("--window-hours", type=int, default=168)
    ap.add_argument("--all-time", action="store_true")
    ap.add_argument("--rollback-to", choices=("shadow", "local"))
    ap.add_argument("--accept-divergence", action="store_true")
    args = ap.parse_args(argv)
    window = None if args.all_time else args.window_hours
    try:
        with (factory or SessionLocal)() as db:
            if args.rollback_to:
                items = ar.rollback_checks(
                    db, args.rollback_to, accept_divergence=args.accept_divergence
                )
                metrics = {}
            else:
                items = ar.checks(db, min_samples=args.min_samples, window_hours=window)
                metrics = ar.metrics(db, window_hours=window)
            db.rollback()
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {type(e).__name__}", file=sys.stderr)  # noqa: T201
        return 2
    status = ar.overall(items)
    if args.json:
        print(
            json.dumps(
                {"status": status, "checks": [asdict(c) for c in items], "metrics": metrics},
                sort_keys=True,
                default=str,
            )
        )  # noqa: T201
    else:
        print(status)  # noqa: T201
        for c in items:
            print(f"  {c.level} {c.name}: {c.reason}")  # noqa: T201
    return 1 if status == ar.FAIL or (args.strict and status == ar.WARN) else 0


if __name__ == "__main__":
    sys.exit(main())
