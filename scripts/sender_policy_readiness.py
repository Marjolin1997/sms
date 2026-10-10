"""Gatishmëria FINALE e M10 Sender Policy (Enterprise). VETËM LEXIM, pa rrjet, pa vlera sender.

    python -m scripts.sender_policy_readiness [--json] [--strict] [--target central] [--central-readiness central.json]
                                              [--min-samples N] [--window-hours H]

`--target central`: vlerëson gatishmërinë për kalimin në central (pa kërkuar ACK). `--central-readiness`: dalja JSON e `apps.central.tools.sender_central_readiness --json`.
Kodi: 0 PASS (ose WARN pa `--strict`) · 1 FAIL (ose WARN me `--strict`) · 2 gabim i brendshëm."""

import argparse
import json
import sys
from dataclasses import asdict

from app.core.db import SessionLocal
from app.services import sender_authority_readiness as ar
from app.services import sender_policy_readiness as pr


def main(argv: list[str] | None = None, factory=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--target", choices=("central",))
    ap.add_argument("--central-readiness")
    ap.add_argument("--min-samples", type=int)
    ap.add_argument("--window-hours", type=int)
    a = ap.parse_args(argv)
    try:
        central = None
        if a.central_readiness:
            with open(a.central_readiness, encoding="utf-8") as f:
                central = json.load(f)
        with (factory or SessionLocal)() as db:
            items = pr.checks(
                db,
                target=a.target,
                central_readiness=central,
                min_samples=a.min_samples,
                window_hours=a.window_hours,
            )
            metrics = ar.metrics(db, window_hours=a.window_hours)
            db.rollback()
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {type(e).__name__}", file=sys.stderr)  # noqa: T201
        return 2
    status = pr.overall(items)
    if a.json:
        print(
            json.dumps(
                {
                    "status": status,
                    "readiness_hash": pr.readiness_hash(items),
                    "checks": [asdict(c) for c in items],
                    "metrics": metrics,
                },
                sort_keys=True,
                default=str,
            )
        )  # noqa: T201
    else:
        print(status)  # noqa: T201
        for c in items:
            print(f"  {c.level} {c.name}: {c.reason}")  # noqa: T201
    return 1 if status == pr.FAIL or (a.strict and status == pr.WARN) else 0


if __name__ == "__main__":
    sys.exit(main())
