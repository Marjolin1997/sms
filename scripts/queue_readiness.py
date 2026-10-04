"""M9-a: readiness vetëm-lexim për rezultatet e panjohura të dërgimit (UNKNOWN / SENDING të ngecur).

    python -m scripts.queue_readiness [--warn-seconds 3600] [--fail-seconds 86400] [--strict]
                                      [--json]

Dalja: `PASS|WARN|FAIL emri: arsyeja`. Kodi: 0 nëse s'ka FAIL (me `--strict`: edhe pa WARN) ·
1 nëse ka FAIL · 2 gabim i brendshëm. Asnjë mutacion: UNKNOWN KURRË nuk lirohet/kapet nga ky skript
(vetëm stafi ose një DLR autoritativ). Para parave reale në prodhim ky kontroll hyn në gate.
"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.core.config import settings
from app.core.db import SessionLocal
from app.models.email import Email, EmailStatus
from app.models.sending import Message, MessageStatus
from app.providers import _email_registry, _registry
from app.providers.base import is_idempotent
from app.services import emails, messages

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: str
    reason: str


def evaluate(db, *, warn_s: int = 3600, fail_s: int = 86400, now: datetime | None = None):
    now = now or datetime.now(UTC)
    out: list[Check] = []
    lease = timedelta(seconds=settings.sending_lease_seconds)
    for kind, model, status, svc in (
        ("sms", Message, MessageStatus.SENDING, messages),
        ("email", Email, EmailStatus.SENDING, emails),
    ):
        stuck = db.scalar(
            select(func.count())
            .select_from(model)
            .where(model.status == status, model.updated_at <= now - 2 * lease)
        )
        out.append(Check(
            f"stuck_sending_{kind}", FAIL if stuck else PASS,
            f"{stuck} SENDING older than 2×lease: the sweeper (worker) is not running" if stuck
            else "none",
        ))  # fmt: skip
        rows = svc.list_unknown(db, now, 500)
        ages = [r["age_seconds"] for r in rows]
        if not rows:
            out.append(Check(f"unknown_{kind}", PASS, "none"))
            continue
        worst = max(ages)
        held = ""
        if kind == "sms":
            by: dict[str, float] = {}
            for r in rows:
                by[r["currency"]] = by.get(r["currency"], 0) + float(r["held_amount"])
            held = f", held={by}"
        level = FAIL if worst > fail_s else WARN if worst > warn_s else PASS
        out.append(Check(f"unknown_{kind}", level, f"{len(rows)} UNKNOWN (oldest {worst}s{held}): "
                         "resolve via /v1/admin/queue/*/resolve"))  # fmt: skip
    caps = {n: is_idempotent(p) for n, p in {**_registry, **_email_registry}.items()}
    listing = ", ".join(f"{k}={v}" for k, v in sorted(caps.items()))
    out.append(Check("provider_capabilities", PASS, "idempotent_by_reference: " + listing))
    return out


def exit_code(checks, *, strict: bool = False) -> int:
    bad = {FAIL, WARN} if strict else {FAIL}
    return 1 if any(c.level in bad for c in checks) else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="UNKNOWN / stuck SENDING readiness (read-only).")
    ap.add_argument("--warn-seconds", type=int, default=3600)
    ap.add_argument("--fail-seconds", type=int, default=86400)
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        with SessionLocal() as db:
            checks = evaluate(db, warn_s=a.warn_seconds, fail_s=a.fail_seconds)
    except Exception as e:  # noqa: BLE001
        print(f"error: {type(e).__name__}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps([asdict(c) for c in checks]))
    else:
        for c in checks:
            print(f"{c.level} {c.name}: {c.reason}")
    return exit_code(checks, strict=a.strict)


if __name__ == "__main__":
    sys.exit(main())
