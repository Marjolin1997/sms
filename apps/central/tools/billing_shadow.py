"""Krahasimi shadow i faturimit (M9-g4): projekton çfarë do të lëshonte Central dhe e krahason me faturat legacy të importuara. Pa numër fature, pa faturë autoritare, pa kursor.

    python -m apps.central.tools.billing_shadow [--recent N] [--json]

Shkruan vetëm krahasime të përhershme të reja (hash i ndryshuar). Kodi: 0 ok · 2 gabim."""

import argparse
import json
import sys
from collections import Counter

from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.services import billing_shadow


def main(argv: list[str] | None = None, engine=None) -> int:
    ap = argparse.ArgumentParser(description="Billing shadow comparison.")
    ap.add_argument("--recent", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.recent < 1:
        print("--recent must be >= 1", file=sys.stderr)  # noqa: T201
        return 2
    try:
        engine = engine or make_engine(settings.database_url)
        with Session(engine) as db:
            rows = billing_shadow.run(db, recent=args.recent)
            db.commit()
            counts = Counter(r.category for r in rows)
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {type(e).__name__}", file=sys.stderr)  # noqa: T201
        return 2
    print(
        json.dumps({"new_comparisons": len(rows), "by_category": dict(counts)}, sort_keys=True)
        if args.json
        else f"new={len(rows)} " + " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    )  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
