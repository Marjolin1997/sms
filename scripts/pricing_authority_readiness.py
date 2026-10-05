"""M9-e: gatishmëria për SMS_PRICING_AUTHORITY=central (VETËM LEXIM; nuk ndryshon konfigurimin as DB-në).

    python -m scripts.pricing_authority_readiness [--json] [--min-samples N] [--max-mismatch-pct P]

Dalja: `PASS|WARN|FAIL emri: arsyeja`. Kodi: 0 vetëm nëse s'ka FAIL · 1 nëse ka · 2 gabim i brendshëm.
Kontrollon: sinkronizim i konfiguruar/i freskët/pa gabim, snapshot i plotë, mapim produkti, caktim + version efektiv, monedha e wallet-it,
integritet cache (hash), krahasim shadow brenda politikës, asnjë ndryshim lokal çmimesh gjatë shadow, motori i përbashkët (submit/estimate/quote),
ACK në prodhim. Pas PASS lejohet `SMS_PRICING_AUTHORITY=central` (aktivizimi është hap i veçantë nga money authority)."""

import argparse
import json
import sys
from dataclasses import asdict

from app.core.db import SessionLocal
from app.services import pricing_readiness as pr


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pricing authority readiness (read-only).")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min-samples", type=int, default=20)
    ap.add_argument("--max-mismatch-pct", type=float, default=0.0)
    a = ap.parse_args(argv)
    try:
        with SessionLocal() as db:
            checks = pr.evaluate(db, min_samples=a.min_samples, max_mismatch_pct=a.max_mismatch_pct)
            db.rollback()
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {type(e).__name__}: {e}", file=sys.stderr)  # noqa: T201
        return 2
    if a.json:
        print(json.dumps([asdict(c) for c in checks], indent=1))  # noqa: T201
    else:
        for c in checks:
            print(f"{c.level} {c.name}: {c.reason}")  # noqa: T201
    return 0 if pr.ok(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
