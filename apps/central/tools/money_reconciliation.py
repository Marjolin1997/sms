"""Rakordimi financiar Central ↔ Enterprise (M9-d). VETËM-LEXIM: asnjë mutacion, asnjë korrigjim automatik.

    python -m apps.central.tools.money_reconciliation [--enterprise-id <uuid>] [--strict] [--json]

Dalja: statusi i përgjithshëm (PASS|WARN|FAIL|CRITICAL), pastaj një rresht per mospërputhje:
`SEVERITY code enterprise/product/currency subject expected=… reported=… detail`, dhe përmbledhje per çelës
(totalet, mosha/watermark e raportit, projeksioni i shadow). Kodi: 0 nëse s'ka FAIL/CRITICAL (me `--strict`:
as WARN) · 1 nëse ka · 2 gabim i brendshëm. Pragjet: `CENTRAL_MONEY_*` (shih docs/M9_MONEY_AUDIT.md)."""

import argparse
import json
import sys
import uuid

from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.services import money_reconciliation as mr


def main(argv: list[str] | None = None, engine=None) -> int:
    ap = argparse.ArgumentParser(description="Money reconciliation (read-only).")
    ap.add_argument("--enterprise-id")
    ap.add_argument("--strict", action="store_true", help="edhe WARN jep kod 1")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        eid = uuid.UUID(args.enterprise_id) if args.enterprise_id else None
        engine = engine or make_engine(settings.database_url)
        with Session(engine) as db:
            res = mr.reconcile(db, enterprise_id=eid)
            db.rollback()  # asnjë shkrim
    except ValueError as e:
        print(f"invalid argument: {e}", file=sys.stderr)  # noqa: T201
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {type(e).__name__}: {e}", file=sys.stderr)  # noqa: T201
        return 2
    if args.json:
        print(json.dumps(res.to_dict(), indent=1))  # noqa: T201
    else:
        print(f"{res.status} reconciliation at {res.generated_at} {res.counts()}")  # noqa: T201
        for d in res.discrepancies:
            print(f"{d.severity} {d.code} {d.enterprise_id}/{d.product_id}/{d.currency} {d.subject or ''} "  # noqa: T201
                  f"expected={d.expected} reported={d.reported} {d.detail}")  # fmt: skip
        for k in res.keys:
            age = "n/a" if k.report_age_seconds is None else f"{int(k.report_age_seconds)}s"
            print(f"KEY {k.enterprise_id}/{k.product_id}/{k.currency} mode={k.authority_mode} seq={k.report_seq} "  # noqa: T201
                  f"age={age} watermark={k.ledger_max_id} cursor={k.money_cursor_seq} issued={k.central_issued_total} "
                  f"reversed={k.central_reversed_total} received={k.enterprise_received_total} applied={k.applied_total} "
                  f"deferred={k.deferred_total} unresolved={k.unresolved_reversal_total} gross={k.gross}")  # fmt: skip
            if k.shadow_projection:
                print(f"  SHADOW {k.shadow_projection}")  # noqa: T201
    return mr.exit_code(res, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
