"""Gate financiar i agreguar (M9-f). VETËM LEXIM: nuk ndryshon konfigurim, DB apo gjendje.

    python -m apps.central.tools.financial_readiness [--json] [--strict] [--no-enterprise-checks]
                                                     [--enterprise-cwd DIR] [--enterprise-timeout S]

Bashkon: (1) pjesën Central (rakordim, raporte përdorimi, reversal-e të pazgjidhura, mint i pashpjeguar, baseline/cutover,
çmime, kredenciale shërbimi) dhe (2) pjesën Enterprise, të ekzekutuar si PROCES të veçantë (Central nuk importon `app`):
`scripts.queue_readiness` (UNKNOWN/SENDING), `scripts.money_authority_readiness` (autoriteti, kursori, ACK),
`scripts.pricing_authority_readiness` (sinkron, shadow, ACK) dhe `scripts.financial_ops` (alarmet CRITICAL/WARN).
Mjedisi i Enterprise (SMS_*) duhet të jetë i disponueshëm për proceset fëmijë. Çdo kontroll që s'mund të ekzekutohet është FAIL
(`--no-enterprise-checks` jep vetëm WARN "NOT VERIFIED": kurrë PASS i rremë).

Dalja: PASS | WARN | FAIL. Kodi: 0 PASS (ose WARN pa `--strict`) · 1 FAIL (ose WARN me `--strict`) · 2 gabim i brendshëm.
Prodhimi NUK është i gatshëm pa PASS të plotë (pa asnjë FAIL; me `--strict` pa asnjë WARN)."""

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.core.timeutil import utcnow
from apps.central.services import financial_ops

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
ENTERPRISE_RUNS = (
    ("queue", "scripts.queue_readiness", ["--json"]),
    ("money", "scripts.money_authority_readiness", ["--json"]),
    ("pricing", "scripts.pricing_authority_readiness", ["--json"]),
    ("ops", "scripts.financial_ops", ["--json"]),
)  # fmt: skip
Runner = Callable[[str, list[str]], tuple[int, str]]


@dataclass(frozen=True, slots=True)
class Item:
    source: str
    name: str
    level: str
    reason: str

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "name": self.name,
            "level": self.level,
            "reason": self.reason,
        }


def subprocess_runner(cwd: str | None, timeout: int) -> Runner:
    def run(module: str, args: list[str]) -> tuple[int, str]:
        p = subprocess.run([sys.executable, "-m", module, *args], cwd=cwd, capture_output=True,
                           text=True, timeout=timeout, check=False)  # fmt: skip
        return p.returncode, p.stdout

    return run


def _level(x: str) -> str:
    return FAIL if x in (FAIL, "CRITICAL") else WARN if x == WARN else PASS


def enterprise_items(run: Runner) -> list[Item]:
    out: list[Item] = []
    for label, module, args in ENTERPRISE_RUNS:
        try:
            code, stdout = run(module, args)
            doc = json.loads(stdout)
        except subprocess.TimeoutExpired:
            out.append(Item(label, "unavailable", FAIL, f"{module} timed out"))
            continue
        except (OSError, ValueError) as e:  # JSON i prishur / s'ka proces
            out.append(
                Item(
                    label,
                    "unavailable",
                    FAIL,
                    f"{module} produced no usable JSON ({type(e).__name__})",
                )
            )
            continue
        if label == "ops":
            alerts = doc.get("alerts", []) if isinstance(doc, dict) else []
            for a in alerts:
                out.append(
                    Item(
                        label,
                        a["code"],
                        _level(a["level"]),
                        f"{a['subject']}: {a['message']}"[:300],
                    )
                )
            if not alerts:
                out.append(Item(label, "no_alerts", PASS, "no CRITICAL/WARN financial alert"))
            continue
        if not isinstance(doc, list) or not doc:
            out.append(
                Item(
                    label,
                    "unavailable",
                    FAIL,
                    f"{module} returned an unexpected document (exit {code})",
                )
            )
            continue
        for c in doc:
            out.append(Item(label, c["name"], _level(c["level"]), str(c.get("reason", ""))[:300]))
    return out


def evaluate(db: Session, run: Runner | None) -> list[Item]:
    now = utcnow()
    items = [
        Item("central", c.name, c.level, c.reason) for c in financial_ops.readiness_checks(db, now)
    ]
    if run is None:
        items.append(
            Item("enterprise", "skipped", WARN, "NOT VERIFIED: Enterprise checks were skipped")
        )
    else:
        items.extend(enterprise_items(run))
    return items


def overall(items: list[Item]) -> str:
    if any(i.level == FAIL for i in items):
        return FAIL
    return WARN if any(i.level == WARN for i in items) else PASS


def exit_code(status: str, strict: bool) -> int:
    return 1 if status == FAIL or (strict and status == WARN) else 0


def main(argv: list[str] | None = None, engine=None, run: Runner | None = None) -> int:
    ap = argparse.ArgumentParser(description="Aggregated financial readiness (read-only).")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="edhe WARN jep kod 1")
    ap.add_argument("--no-enterprise-checks", action="store_true")
    ap.add_argument("--enterprise-cwd")
    ap.add_argument("--enterprise-timeout", type=int, default=180)
    a = ap.parse_args(argv)
    try:
        engine = engine or make_engine(settings.database_url)
        runner = (
            None
            if a.no_enterprise_checks
            else (run or subprocess_runner(a.enterprise_cwd, a.enterprise_timeout))
        )
        with Session(engine) as db:
            items = evaluate(db, runner)
            db.rollback()
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)  # noqa: T201
        return 2
    status = overall(items)
    if a.json:
        print(
            json.dumps(
                {"status": status, "strict": a.strict, "checks": [i.as_dict() for i in items]},
                indent=1,
            )
        )  # noqa: T201
    else:
        for i in items:
            print(f"{i.level} {i.source}:{i.name}: {i.reason}")  # noqa: T201
        print(f"FINANCIAL READINESS: {status}")  # noqa: T201
    return exit_code(status, a.strict)


if __name__ == "__main__":
    sys.exit(main())
