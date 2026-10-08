"""Gatishmëria FINALE e faturimit (M9-g5): një raport i vetëm, VETËM LEXIM, pa PII, pa rrjet.

    python -m apps.central.tools.billing_final_readiness [--json] [--strict] [--observability]

Bashkon autoritetin/ACK/import/baseline/sekuencat/shadow/freeze/worker/periudhat/shlyerjen/invariantet. Del PASS | WARN | FAIL + alarmet (CRITICAL/WARN).
Kodi: 0 PASS (ose WARN pa `--strict`) · 1 FAIL (ose WARN me `--strict`) · 2 gabim i brendshëm."""

import argparse
import json
import sys

from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.services import billing_closure as bc


def main(argv: list[str] | None = None, engine=None) -> int:
    ap = argparse.ArgumentParser(description="Final billing readiness (Central, read-only).")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--observability", action="store_true", help="include the operational snapshot")
    args = ap.parse_args(argv)
    try:
        engine = engine or make_engine(settings.database_url)
        with Session(engine) as db:
            doc = bc.final_readiness(db)
            doc["alerts"] = bc.alerts(doc["checks"], doc["mode"])
            if args.observability:
                doc["observability"] = bc.observability(db)
            db.rollback()
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {type(e).__name__}", file=sys.stderr)  # noqa: T201
        return 2
    if args.json:
        print(json.dumps(doc, sort_keys=True))  # noqa: T201
    else:
        print(f"{doc['status']} (mode={doc['mode']})")  # noqa: T201
        for c in doc["checks"]:
            if c["level"] != bc.PASS:
                print(f"  {c['level']} {c['name']}: {c['reason']}")  # noqa: T201
        for a in doc["alerts"]:
            print(f"  ALERT {a['severity']} {a['code']}: {a['message']}")  # noqa: T201
    bad = doc["status"] == bc.FAIL or (args.strict and doc["status"] == bc.WARN)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
