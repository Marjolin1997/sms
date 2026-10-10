"""Alertat e sender policy (M10-S5). VETËM LEXIM, pa vlera sender.

python -m scripts.sender_alerts [--json]     # kodi 1 kur ka alertë critical, 0 përndryshe, 2 gabim i brendshëm"""

import argparse
import json
import sys
from dataclasses import asdict

from app.core.db import SessionLocal
from app.services import sender_alerts as al


def main(argv: list[str] | None = None, factory=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        with (factory or SessionLocal)() as db:
            alerts = al.evaluate(db)
            db.rollback()
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {type(e).__name__}", file=sys.stderr)  # noqa: T201
        return 2
    if a.json:
        print(json.dumps([asdict(x) for x in alerts], sort_keys=True))  # noqa: T201
    else:
        for x in alerts:
            print(f"{x.level:8} {x.name} value={x.value} threshold={x.threshold}")  # noqa: T201
    return 1 if any(x.level == "critical" for x in alerts) else 0


if __name__ == "__main__":
    sys.exit(main())
