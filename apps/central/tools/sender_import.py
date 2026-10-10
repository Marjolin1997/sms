"""Bootstrap i senderave ekzistues (artefakt nga Enterprise) në regjistrin Central — DRY-RUN si parazgjedhje.

    python -m apps.central.tools.sender_import --artifact senders.json [--json]                                   # klasifikim, pa shkrim
    python -m apps.central.tools.sender_import --artifact senders.json --apply --actor-email admin@example.com \\
        --ack-artifact-hash <sha256 i printuar nga dry-run> [--batch-size 100] [--enterprise-id UUID]             # zbatim në batch-e

Importon vetëm `missing_in_central` (i miratuar lokalisht, politika e lejon, pa konflikt) dhe `local_pending`; çdo konflikt raportohet pa shkrim. Zbatimi kërkon admin njeri dhe hash-in e artefaktit.
Kodi: 0 sukses (edhe me raportim konfliktesh) · 1 hash/miratim i gabuar · 2 gabim i brendshëm/hyrje."""

import argparse
import json
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.models.user import CentralUser
from apps.central.services import sender_bootstrap as sb


def main(argv: list[str] | None = None, engine=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--actor-email")
    ap.add_argument("--ack-artifact-hash")
    ap.add_argument("--batch-size", type=int, default=100)
    ap.add_argument("--enterprise-id")
    ap.add_argument("--source-revision")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        with open(a.artifact, encoding="utf-8") as f:
            art = json.load(f)
        engine = engine or make_engine(settings.database_url)
        with Session(engine) as db:
            actor = None
            if a.apply:
                if a.ack_artifact_hash != sb.artifact_hash(art):
                    print(
                        "refused: --ack-artifact-hash must equal the hash printed by the dry-run",
                        file=sys.stderr,
                    )  # noqa: T201
                    return 1
                actor = db.scalar(
                    select(CentralUser).where(CentralUser.email == (a.actor_email or "").lower())
                )
                if actor is None:
                    print("refused: --actor-email must be an existing admin user", file=sys.stderr)  # noqa: T201
                    return 1
            rep = sb.run(
                db, art, apply=a.apply, actor_id=None if actor is None else actor.id, batch_size=a.batch_size,
                enterprise_id=uuid.UUID(a.enterprise_id) if a.enterprise_id else None, source_revision=a.source_revision,
            )  # fmt: skip
            if not a.apply:
                db.rollback()
    except Exception as e:  # noqa: BLE001
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)  # noqa: T201
        return 2
    if a.json:
        print(json.dumps(rep, sort_keys=True))  # noqa: T201
    else:
        print(
            f"mode={rep['mode']} senders={rep['senders']} tenants={rep['tenants']} unresolved={rep['unresolved']} imported={rep['imported']}"
        )  # noqa: T201
        for k, v in rep["summary"].items():
            print(f"  {k}: {v}")  # noqa: T201
        print(f"artifact_hash={rep['artifact_hash']}\nreport_hash={rep['report_hash']}")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
