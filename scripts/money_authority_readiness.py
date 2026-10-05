"""M9-c: gatishmëria për SMS_MONEY_AUTHORITY=central (VETËM LEXIM; nuk ndryshon konfigurimin as DB-në).

    python -m scripts.money_authority_readiness [--json] [--skip-queue]

Dalja: `PASS|WARN|FAIL emri: arsyeja`. Kodi: 0 vetëm nëse s'ka FAIL · 1 nëse ka FAIL · 2 gabim i brendshëm.
Vetëm pas PASS lejohet `SMS_MONEY_AUTHORITY=central` (në prodhim edhe `SMS_MONEY_AUTHORITY_ACK=true`).
"""

import argparse
import json
import sys
from dataclasses import asdict

from app.core.db import SessionLocal
from app.services import money_readiness as mr


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Money authority readiness (read-only).")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--skip-queue", action="store_true", help="vetëm për diagnostikim; jo për gate")
    args = ap.parse_args(argv)
    try:
        with SessionLocal() as db:
            checks = mr.evaluate(db, include_queue=not args.skip_queue)
            db.rollback()  # asnjë shkrim: leximi i kursorit mund të krijojë rreshtin singleton
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {type(e).__name__}: {e}", file=sys.stderr)  # noqa: T201
        return 2
    if args.json:
        print(json.dumps([asdict(c) for c in checks], indent=1))  # noqa: T201
    else:
        for c in checks:
            print(f"{c.level} {c.name}: {c.reason}")  # noqa: T201
    return 0 if mr.ok(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
