"""Importi i faturimit legacy në Central nga artifact i eksportuar (M9-g4). Dry-run si parazgjedhje (zero shkrime); apply kërkon hash dhe aktor admin njeri.

    python -m apps.central.tools.billing_import --artifact export.json                       # dry-run: klasifikim, seed-e, bllokues
    python -m apps.central.tools.billing_import --artifact export.json --apply --evidence-hash <content_hash> --actor-email admin@example.com [--require-clean]
    python -m apps.central.tools.billing_import --issues   |   --resolve <issue_id> --reason "..." [--evidence-ref TICKET-123] --actor-email admin@example.com

Dry-run: kodi 0 pa rreshta bllokues · 1 me bllokues · 2 gabim argumenti/artifact i pavlefshëm. Apply: 0 ok (ose no-op idempotent) · 1 konflikt/hash i gabuar · 2 gabim.
Central s'lidhet kurrë me DB-në e Enterprise: vetëm lexon skedarin."""

import argparse
import json
import sys

from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.core.errors import CentralError, Conflict
from apps.central.services import billing_authority, billing_import, users
from packages.contracts.control_plane.billing import legacy_export_v1 as lx


def _actor(db, email):
    if not email:
        raise CentralError("--actor-email (a human Central admin) is required")
    u = users.get_by_email(db, email)
    if u is None:
        raise CentralError("unknown actor")
    return u


def main(argv: list[str] | None = None, engine=None) -> int:
    ap = argparse.ArgumentParser(description="Import legacy billing into Central.")
    ap.add_argument("--artifact")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--evidence-hash")
    ap.add_argument("--actor-email")
    ap.add_argument("--require-clean", action="store_true")
    ap.add_argument("--resolve")
    ap.add_argument("--reason")
    ap.add_argument(
        "--evidence-ref",
        help="ticket/document reference for a documented waiver (requires_manual_review only)",
    )
    ap.add_argument(
        "--issues",
        action="store_true",
        help="list unresolved issues with category/allowed/forbidden (read-only)",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    try:
        engine = engine or make_engine(settings.database_url)
        if args.resolve:
            with Session(engine) as db:
                row = billing_import.resolve_issue(
                    db,
                    _actor(db, args.actor_email),
                    args.resolve,
                    args.reason,
                    evidence_ref=args.evidence_ref,
                )
                db.commit()
                print(json.dumps({"issue_id": str(row.id), "resolved": True}))  # noqa: T201
            return 0
        if args.issues:
            with Session(engine) as db:
                rows = [billing_import.issue_view(i) for i in billing_import.unresolved_issues(db)]
                db.rollback()
            print(json.dumps({"unresolved": rows}, sort_keys=True))  # noqa: T201
            return 0
        if not args.artifact:
            print("--artifact is required", file=sys.stderr)  # noqa: T201
            return 2
        with open(args.artifact, encoding="utf-8") as f:
            doc = json.load(f)
        lx.parse(doc)
        with Session(engine) as db:
            auth_mode = billing_authority.mode(db)
            if not args.apply:
                rep = billing_import.plan_import(db, doc, authority=auth_mode).report()
                db.rollback()  # dry-run: asnjë shkrim
                print(json.dumps(rep, sort_keys=True) if args.json else _text(rep))  # noqa: T201
                return 1 if rep["blocking_total"] else 0
            batch = billing_import.apply(
                db,
                doc,
                _actor(db, args.actor_email),
                args.evidence_hash or "",
                authority=auth_mode,
                require_clean=args.require_clean,
            )
            db.commit()
            print(
                json.dumps(
                    {
                        "batch_id": str(batch.id),
                        "export_id": str(batch.export_id),
                        "summary": batch.summary,
                    },
                    sort_keys=True,
                )
            )  # noqa: T201
            return 0
    except (lx.ContractError, ValueError, OSError) as e:
        print(f"invalid artifact: {type(e).__name__}: {e}", file=sys.stderr)  # noqa: T201
        return 2
    except Conflict as e:
        print(f"conflict: {e}", file=sys.stderr)  # noqa: T201
        return 1
    except CentralError as e:
        print(f"error: {e}", file=sys.stderr)  # noqa: T201
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {type(e).__name__}", file=sys.stderr)  # noqa: T201
        return 2


def _text(rep: dict) -> str:
    lines = [
        f"export {rep['export_id']} hash {rep['content_hash']}",
        f"authority at source: {rep['authority']}",
    ]
    for t, c in sorted(rep["summary"].items()):
        lines.append(f"  {t}: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))
    lines.append(
        "sequence seeds: "
        + (", ".join(f"{y}->{n}" for y, n in rep["sequence_seeds"].items()) or "none")
    )
    for b in rep["blocking"][:50]:
        lines.append(
            f"  BLOCKING {b['table']}#{b['source_id']} {b['classification']}: {b['reason']}"
        )
    lines.append(f"blocking rows: {rep['blocking_total']}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
