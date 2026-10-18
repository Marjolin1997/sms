"""M9-g4: eksporton faturimin legacy në artifact `cp.billing.legacy_export.v1` (VETËM LEXIM; nuk ndryshon asnjë rresht).

    python -m scripts.billing_export --out /path/export.json [--force]

Shkruan skedarin (0600) në mënyrë atomike dhe printon `export_id`, `content_hash` dhe numëruesit. Hash-i përdoret te `apps.central.tools.billing_import --evidence-hash`.
Artifact-i s'përmban sekrete; përmban snapshot-et ligjore të faturave (PII minimale e domosdoshme). Kodi: 0 ok · 1 skedari ekziston/gabim · 2 gabim i brendshëm."""

import argparse
import json
import os
import sys
import tempfile

from app.core.db import engine
from app.services import billing_export


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Export legacy billing for the Central import (read-only)."
    )
    ap.add_argument("--out", required=True)
    ap.add_argument("--force", action="store_true", help="mbishkruaj skedarin ekzistues")
    args = ap.parse_args(argv)
    if os.path.exists(args.out) and not args.force:
        print(f"refusing to overwrite {args.out} (use --force)", file=sys.stderr)  # noqa: T201
        return 1
    try:
        doc = billing_export.export(engine)
    except Exception as e:  # noqa: BLE001
        print(f"export failed: {type(e).__name__}: {e}", file=sys.stderr)  # noqa: T201
        return 2
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(os.path.abspath(args.out)) or ".", prefix=".billing_export."
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, sort_keys=True, separators=(",", ":"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, args.out)
    except OSError as e:
        os.unlink(tmp)
        print(f"cannot write {args.out}: {e}", file=sys.stderr)  # noqa: T201
        return 1
    print(
        json.dumps(
            {
                "export_id": doc["export_id"],
                "content_hash": doc["content_hash"],
                "counts": doc["counts"],
            },
            sort_keys=True,
        )
    )  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
