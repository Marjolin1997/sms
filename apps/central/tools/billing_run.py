"""Ekzekutimi i faturimit periodik (M9-g1/g2): faturon periudhat e afatuara, idempotent, i kufizuar, i sigurt në paralel.

    python -m apps.central.tools.billing_run [--limit N] [--max-periods N] [--subscription-id UUID] [--json]

Çdo periudhë ka transaksionin e vet (shih `billing.process_period`): kyç abonimin `FOR UPDATE`, rirunimi/ekzekutimi paralel s'krijon
periudhë/faturë të dytë. Periudhë që pret raportin e përdorimit të email-it NUK vlerësohet kurrë: raportohet `waiting_usage` dhe
rishikohet në ekzekutimin e radhës. Asnjë thirrje rrjeti drejt Enterprise.
Dalja: due / invoiced / no_charge / waiting_usage / postponed / failed. Kodi: 0 (failed = 0) · 1 (failed > 0) · 2 gabim i brendshëm · 3 refuzuar (autoriteti i faturimit nuk është `central`)."""

import argparse
import json
import sys

from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.core.errors import Conflict
from apps.central.services import billing, billing_authority


def main(argv: list[str] | None = None, engine=None) -> int:
    ap = argparse.ArgumentParser(description="Run due subscription billing.")
    ap.add_argument(
        "--limit", type=int, default=500, help="maksimumi i abonimeve për ekzekutim (default 500)"
    )
    ap.add_argument(
        "--max-periods",
        type=int,
        default=billing.MAX_CATCH_UP,
        help="periudha maksimale per abonim",
    )
    ap.add_argument("--subscription-id")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.limit < 1 or args.max_periods < 1:
        print("invalid argument: --limit and --max-periods must be >= 1", file=sys.stderr)  # noqa: T201
        return 2
    try:
        engine = engine or make_engine(settings.database_url)
        with (
            Session(engine) as db
        ):  # M9-g4: vetëm autoriteti `central` lëshon (local/shadow ⇒ Enterprise është lëshuesi)
            billing_authority.require_central(db)
        out = billing.run_due(
            engine,
            max_periods=args.max_periods,
            limit=args.limit,
            subscription_id=args.subscription_id,
        )
    except ValueError as e:
        print(f"invalid argument: {e}", file=sys.stderr)  # noqa: T201
        return 2
    except Conflict as e:
        print(f"refused: {e}", file=sys.stderr)  # noqa: T201
        return 3
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {type(e).__name__}", file=sys.stderr)  # noqa: T201
        return 2
    d = out.as_dict()
    if args.json:
        print(json.dumps(d, sort_keys=True))  # noqa: T201
    else:
        print(
            "  ".join(
                f"{k}={d[k]}"
                for k in ("due", "invoiced", "no_charge", "waiting_usage", "postponed", "failed")
            )
        )  # noqa: T201
        for k, v in sorted({**d["postponed_reasons"], **d["waiting_reasons"]}.items()):
            print(f"  reason {k}: {v}")  # noqa: T201
    return 1 if out.failed else 0


if __name__ == "__main__":
    sys.exit(main())
