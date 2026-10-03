"""Gatishmëria për SMS_CP_SYNC_MODE=enforce (vetëm-lexim; NUK ndryshon konfigurimin).

    python -m scripts.cp_enforce_readiness [--exceptions pranuar.json]

`pranuar.json`: {"exceptions": [{"enterprise_id": "<uuid>", "channel": "sms"|"email"}, ...]} —
mospërputhje legacy↔CP që operatori i pranon eksplicit. Kodi 0 = gati nga ana e Enterprise, 1 = jo.
Portat e jashtme (M7-f dry-run real, review/conflict, periudhë shadow) nuk verifikohen këtu."""

import argparse
import json
import sys
import uuid

from app.core.db import SessionLocal
from app.services.entitlements import can_enable_enforce


def load_exceptions(path: str) -> set[tuple[uuid.UUID, str]]:
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    out = set()
    for e in doc["exceptions"]:
        if e["channel"] not in ("sms", "email"):
            raise ValueError("channel must be sms or email")
        out.add((uuid.UUID(e["enterprise_id"]), e["channel"]))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check readiness for control-plane enforce mode.")
    ap.add_argument("--exceptions", help="JSON me mospërputhjet e pranuara eksplicit")
    args = ap.parse_args(argv)
    try:
        exc = load_exceptions(args.exceptions) if args.exceptions else set()
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"invalid exceptions file: {type(e).__name__}", file=sys.stderr)  # noqa: T201
        return 2
    with SessionLocal() as db:
        rep = can_enable_enforce(db, exc)
    print(json.dumps(rep.to_dict(), indent=1, sort_keys=True))  # noqa: T201
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
