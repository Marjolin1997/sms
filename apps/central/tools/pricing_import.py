"""Import i tarifave ekzistuese (propozim nga Enterprise) në çmime Central — DRY-RUN si parazgjedhje.

    python -m apps.central.tools.pricing_import --proposal pricing.json [--json]                       # klasifikim, pa shkrim
    python -m apps.central.tools.pricing_import --proposal pricing.json --apply \\
        --actor-email admin@example.com --ack-proposal-hash <sha256 i printuar nga dry-run>             # zbatim (vetëm `exact`)

Klasifikimi: exact | conflict | invalid | unmapped. Zbatimi kërkon admin njeri dhe hash-in e propozimit që operatori e pa në dry-run.
Kodi: 0 sukses (edhe me conflict/invalid/unmapped të raportuar) · 1 hash/miratim i gabuar · 2 gabim i brendshëm/hyrje."""

import argparse
import json
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.models.user import CentralUser
from apps.central.services import pricing_import as pi


def main(argv: list[str] | None = None, engine=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proposal", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--actor-email")
    ap.add_argument("--ack-proposal-hash")
    ap.add_argument("--sms-product-code", default="sms")
    ap.add_argument("--email-product-code", default="email")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        with open(a.proposal, encoding="utf-8") as f:
            proposal = json.load(f)
        engine = engine or make_engine(settings.database_url)
        with Session(engine) as db:
            actor = None
            if a.apply:
                if a.ack_proposal_hash != pi.proposal_hash(proposal):
                    print(
                        "refused: --ack-proposal-hash must equal the hash printed by the dry-run",
                        file=sys.stderr,
                    )  # noqa: T201
                    return 1
                actor = db.scalar(
                    select(CentralUser).where(CentralUser.email == (a.actor_email or "").lower())
                )
                if actor is None:
                    print("refused: --actor-email must be an existing admin user", file=sys.stderr)  # noqa: T201
                    return 1
            rep = pi.classify_and_apply(db, proposal, actor=actor, apply=a.apply,
                                        sms_product_code=a.sms_product_code, email_product_code=a.email_product_code)  # fmt: skip
            if a.apply:
                db.commit()
            else:
                db.rollback()
    except Exception as e:  # noqa: BLE001
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)  # noqa: T201
        return 2
    if a.json:
        print(json.dumps(rep.to_dict(), indent=1))  # noqa: T201
    else:
        print(
            f"{'APPLIED' if a.apply else 'DRY-RUN'} proposal_hash={rep.proposal_hash} {rep.counts()}"
        )  # noqa: T201
        for i in rep.items:
            print(
                f"{i.classification.upper():9} {i.kind:10} {i.ref} {i.reason}{' [applied]' if i.applied else ''}"
            )  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
