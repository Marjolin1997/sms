"""M9-f: pamja operacionale financiare e Enterprise (vetëm lexim, pa mutacion).

    python -m scripts.financial_ops [--json]

Printon numrat e sigurt (UNKNOWN, wallet/hold, kursori i parave, reversal-et e pazgjidhura, outbox i raporteve, snapshot-i i çmimeve,
mospërputhjet shadow) dhe alarmet `CRITICAL`/`WARN`. Kodi: 0 pa CRITICAL · 1 me CRITICAL · 2 gabim i brendshëm.
"""

import argparse
import json
import sys

from app.core.db import SessionLocal
from app.services import financial_ops


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Financial operations view (read-only).")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        with SessionLocal() as db:
            snap = financial_ops.snapshot(db)
            db.rollback()
        alerts = financial_ops.alerts(snap)
        financial_ops.emit(alerts)
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {type(e).__name__}: {e}", file=sys.stderr)  # noqa: T201
        return 2
    if a.json:
        print(json.dumps({"snapshot": snap, "alerts": alerts}, indent=1, default=str))  # noqa: T201
    else:
        for al in alerts:
            print(f"{al['level']} {al['code']} {al['subject']}: {al['message']}")  # noqa: T201
        print(json.dumps(snap, indent=1, default=str))  # noqa: T201
    return 1 if any(x["level"] == financial_ops.CRITICAL for x in alerts) else 0


if __name__ == "__main__":
    sys.exit(main())
