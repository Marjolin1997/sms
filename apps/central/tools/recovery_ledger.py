"""Verifikimi i ledger-it tregtar të Central pas restore (M11-I/F-05A). VETËM-LEXIM; asnjë URL/kredencial nuk printohet.

    python -m apps.central.tools.recovery_ledger [--url-env VAR]

Kontrollon (nga ledger-i, jo nga balanca të ruajtura): `money_sequence` nuk është pas seq-it më të lartë të lëshuar (ledger + ngjarje),
asnjë llogari me fonde ose `available_to_grant` negative, dhe invariantet e faturimit (`billing_closure.invariants`, pa FAIL).
URL-ja lexohet nga konfigurimi i Central (`CENTRAL_DATABASE_URL`), ose nga ndryshorja e dhënë me `--url-env`.
Kodi: 0 në rregull · 1 gjetje integriteti · 2 gabim përdorimi/lidhjeje. E vetë-mjaftueshme: nuk varet nga `scripts/` apo `app/` të Enterprise."""

import argparse
import os
import sys

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.models.money import CommercialLedgerEntry, CreditAccount, MoneyEvent
from apps.central.services import billing_closure, commercial_ledger, money_sequence


def check(db: Session) -> tuple[list[str], int, int]:
    """→ (problemet, numri i llogarive, numri i hyrjeve të ledger-it)."""
    problems: list[str] = []
    _, last_seq = money_sequence.current(db)
    max_ledger = db.scalar(select(func.max(CommercialLedgerEntry.seq))) or 0
    max_event = db.scalar(select(func.max(MoneyEvent.seq))) or 0
    if max(max_ledger, max_event) > last_seq:
        problems.append(
            f"money_sequence ({last_seq}) is behind issued seq (ledger {max_ledger}, events {max_event})"
        )
    accounts = db.scalars(select(CreditAccount).order_by(CreditAccount.id)).all()
    entries = 0
    for a in accounts:
        t = commercial_ledger.totals(db, a.id)
        entries += t.entries
        if t.funds < 0 or t.available_to_grant < 0:
            problems.append(
                f"account {a.id}: negative funds/available_to_grant ({t.funds}/{t.available_to_grant})"
            )
    for c in billing_closure.invariants(db):
        if c.level == "FAIL":
            problems.append(f"invariant {c.name}: {c.reason}")
    return problems, len(accounts), entries


def main(argv: list[str] | None = None, engine=None) -> int:
    ap = argparse.ArgumentParser(
        description="Central ledger verification after restore (read-only)."
    )
    ap.add_argument(
        "--url-env", help="ndryshorja e mjedisit me URL-në (parazgjedhje: CENTRAL_DATABASE_URL)"
    )
    args = ap.parse_args(argv)
    try:
        if engine is None:
            url = os.environ.get(args.url_env) if args.url_env else settings.database_url
            if not url:
                print(f"ERROR: environment variable {args.url_env} is not set", file=sys.stderr)
                return 2
            engine = make_engine(url)
        with Session(engine) as db:
            problems, accounts, entries = check(db)
    except Exception as exc:  # mesazhi i drajverit mund të përfshijë URL-në: printojmë vetëm llojin
        print(f"ERROR: cannot verify the Central ledger ({type(exc).__name__})", file=sys.stderr)
        return 2
    for p in problems:
        print("PROBLEM:", p)
    print(
        f"checked {accounts} credit accounts, {entries} ledger entries: {'OK' if not problems else 'FAILED'}"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
