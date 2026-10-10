"""Plotëson `enterprise_id` për rreshtat ekzistues (M1b), në batch. Idempotent dhe i rifillueshëm.
    SMS_DATABASE_URL=… python -m scripts.backfill_enterprise_id [--batch 5000] [--table sms_messages …]
    SMS_DATABASE_URL=… python -m scripts.backfill_enterprise_id --check     # vetëm raport (dalja 1 nëse jo-përputhje)
Para se të filloni ekzekutoni `python -m scripts.enterprises_audit`. Mund të ekzekutohet me sistemin
në punë (batch me commit për batch); ndaloni/rifilloni kur të doni. Dual-write mbulon rreshtat e rinj."""

import argparse
import sys

from app.core.db import SessionLocal
from app.services import enterprises


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=5000)
    ap.add_argument("--table", action="append", help="vetëm këto tabela")
    ap.add_argument("--check", action="store_true", help="vetëm kontrollo konsistencën")
    a = ap.parse_args()
    if not a.check:
        done = enterprises.backfill_enterprise_ids(
            SessionLocal, a.batch, tuple(a.table) if a.table else None, log_fn=print
        )
        print(
            "përditësuar:",
            {t: n for t, n in done.items() if n} or "asgjë (gjithçka ishte e plotësuar)",
        )
    with SessionLocal() as db:
        rep = enterprises.check_consistency(db)
    print(rep.report())
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
