"""Bootstrap i senderave ekzistues drejt Central (M10-S4), pjesa Enterprise.

    python -m scripts.sender_bootstrap export --out senders.json [--source-revision REV]     # artefakt VETËM-LEXIM për `apps.central.tools.sender_import`
    python -m scripts.sender_bootstrap reconcile [--record] [--central-report REPORT.json] [--json]   # rakordim lokal-vs-projeksion; --record shënon çështjet + gjendjen
    python -m scripts.sender_bootstrap resolve --sender-id N --category C --resolution accepted_not_migrated|sender_deactivated --actor A --reason R [--evidence-ref X]
    python -m scripts.sender_bootstrap issues

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
    r.add_argument("--central-report")
    z = sub.add_parser("resolve")
    z.add_argument("--sender-id", type=int, required=True)
    z.add_argument("--category", required=True)
    z.add_argument("--resolution", required=True, choices=sb.OPERATOR_RESOLUTIONS)
    z.add_argument("--actor", required=True)
    z.add_argument("--reason", required=True)
    z.add_argument("--evidence-ref")
    sub.add_parser("issues")
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
            if a.cmd == "resolve":
                try:
                    sb.resolve(
                        db,
                        sender_id=a.sender_id,
                        category=a.category,
                        resolution=a.resolution,
                        actor=a.actor,
                        reason=a.reason,
                        evidence_ref=a.evidence_ref,
                    )
                except sb.ResolutionError as ex:
                    db.rollback()
                    print(f"refused: {ex}", file=sys.stderr)  # noqa: T201
                    return 1
                db.commit()
                print("resolved")  # noqa: T201
                return 0
            if a.cmd == "issues":
                from sqlalchemy import select

                from app.models.sender_authority import SenderBootstrapIssue as Iss

                rows = [
                    {
                        "sender_id": i.sender_id,
                        "category": i.category,
                        "resolution": i.resolution,
                        "resolved_by": i.resolved_by,
                        "open": i.resolved_at is None,
                    }
                    for i in db.scalars(select(Iss).order_by(Iss.id))
                ]
                db.rollback()
                print(json.dumps(rows, sort_keys=True))  # noqa: T201
                return 0
            central = None
            if a.central_report:
                with open(a.central_report, encoding="utf-8") as f:
                    central = json.load(f)
            rep = sb.reconcile(
                db, record=a.record, source_revision=a.source_revision, central_report=central
            )
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
