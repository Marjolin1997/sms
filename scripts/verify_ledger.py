"""Verifikon integritetin e parave në një bazë (e mirë pas restore ose si kontroll periodik):
- për çdo wallet: balanca e ledger-it = SUM(delta) dhe asnjë balancë negative;
- numrat e faturave janë pa boshllëqe.
Dalja 0 = në rregull; 1 = gjetje. Vetëm-lexim.
    SMS_DATABASE_URL=... python -m scripts.verify_ledger"""

import sys

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.billing import Invoice
from app.models.wallet import Wallet
from app.services import wallet as wallets


def main() -> int:
    problems: list[str] = []
    with SessionLocal() as db:
        n = 0
        for w in db.scalars(select(Wallet).order_by(Wallet.id)):
            n += 1
            avail, held = wallets.balances(db, w.id)
            if avail < 0 or held < 0:
                problems.append(f"wallet {w.id}: negative balance ({avail}/{held})")
            if not wallets.verify_wallet(db, w.id):
                problems.append(f"wallet {w.id}: ledger does not add up")
        numbers = sorted(db.scalars(select(Invoice.number)))
        by_year: dict[str, list[int]] = {}
        for num in numbers:  # format: PREFIX-YYYY-NNNNNN
            head, _, seq = num.rpartition("-")
            if seq.isdigit():
                by_year.setdefault(head, []).append(int(seq))
        for head, seqs in by_year.items():
            if seqs != list(range(seqs[0], seqs[0] + len(seqs))) or seqs[0] != 1:
                problems.append(f"invoice numbering for '{head}' has gaps or does not start at 1")
    for p in problems:
        print("PROBLEM:", p)
    print(f"checked {n} wallets, {len(numbers)} invoices: {'OK' if not problems else 'FAILED'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
