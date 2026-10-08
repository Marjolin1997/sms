"""Gjendja dhe ndërrimi i autoritetit të faturimit në Central (M9-g4).

    python -m apps.central.tools.billing_authority status
    python -m apps.central.tools.billing_authority set --mode shadow|central|local --reason "..." --actor-email admin@example.com [--ack]

`central` kërkon `--ack`, kalim nga `shadow` dhe readiness pa FAIL; rollback nga `central` bllokohet pasi Central ka lëshuar faturë autoritare. Kodet: 0 ok · 1 refuzim (Conflict) · 2 gabim."""

import argparse
import json
import sys

from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.core.errors import CentralError, Conflict
from apps.central.services import billing_authority, users


def main(argv: list[str] | None = None, engine=None) -> int:
    ap = argparse.ArgumentParser(description="Billing authority (Central).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    st = sub.add_parser("set")
    st.add_argument("--mode", required=True, choices=("local", "shadow", "central"))
    st.add_argument("--reason", required=True)
    st.add_argument("--actor-email", required=True)
    st.add_argument("--ack", action="store_true")
    args = ap.parse_args(argv)
    try:
        engine = engine or make_engine(settings.database_url)
        with Session(engine) as db:
            if args.cmd == "status":
                s = billing_authority.get_state(db)
                print(
                    json.dumps(
                        {
                            "mode": billing_authority.mode(db),
                            "ack": bool(s and s.ack),
                            "central_invoices": billing_authority.central_invoice_count(db),
                        },
                        sort_keys=True,
                    )
                )  # noqa: T201
                return 0
            actor = users.get_by_email(db, args.actor_email)
            if actor is None:
                raise CentralError("unknown actor")
            s = billing_authority.set_mode(db, actor, args.mode, ack=args.ack, reason=args.reason)
            db.commit()
            print(json.dumps({"mode": s.mode}))  # noqa: T201
            return 0
    except Conflict as e:
        print(f"refused: {e}", file=sys.stderr)  # noqa: T201
        return 1
    except CentralError as e:
        print(f"error: {e}", file=sys.stderr)  # noqa: T201
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {type(e).__name__}", file=sys.stderr)  # noqa: T201
        return 2


if __name__ == "__main__":
    sys.exit(main())
