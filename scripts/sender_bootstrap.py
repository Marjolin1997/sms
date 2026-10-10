"""Bootstrap i senderave ekzistues drejt Central (M10-S4), pjesa Enterprise.

    python -m scripts.sender_bootstrap export --out senders.json [--source-revision REV]     # artefakt VETËM-LEXIM për `apps.central.tools.sender_import`
    python -m scripts.sender_bootstrap reconcile [--record] [--source-revision REV] [--json]  # rakordim lokal-vs-projeksion; --record shënon gjendjen e qëndrueshme

Kodi i `reconcile`: 0 nëse asgjë e pazgjidhur · 1 nëse ka të pazgjidhura · 2 gabim i brendshëm."""

import argparse
import json
import sys

from app.core.db import SessionLocal
from app.services import sender_bootstrap as sb


def main(argv: list[str] | None = None, factory=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--out", required=True)
    e.add_argument("--source-revision", default="unknown")
    r = sub.add_parser("reconcile")
    r.add_argument("--record", action="store_true")
    r.add_argument("--source-revision")
    r.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        with (factory or SessionLocal)() as db:
            if a.cmd == "export":
                art = sb.export(db, a.source_revision)
                db.rollback()
                with open(a.out, "w", encoding="utf-8") as f:
                    json.dump(art, f, sort_keys=True)
                print(f"exported {len(art['senders'])} sender(s) to {a.out}")  # noqa: T201
                return 0
            rep = sb.reconcile(db, record=a.record, source_revision=a.source_revision)
            if a.record:
                db.commit()
            else:
                db.rollback()
    except Exception as ex:  # noqa: BLE001
        print(f"internal error: {type(ex).__name__}", file=sys.stderr)  # noqa: T201
        return 2
    if a.json:
        print(json.dumps(rep, sort_keys=True))  # noqa: T201
    else:
        print(
            f"senders={rep['senders']} tenants={rep['tenants']} unresolved={rep['unresolved']} report_hash={rep['report_hash']}"
        )  # noqa: T201
        for k, v in rep["summary"].items():
            print(f"  {k}: {v}")  # noqa: T201
    return 0 if rep["unresolved"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
