"""Ledger-i tregtar i pandryshueshëm (M9-b) + formula kanonike e totaleve.

    funds              = Σ payment_credit + Σ manual_credit_adjustment − Σ manual_debit_adjustment
    outstanding_grants = Σ grant_issued − Σ grant_reversal
    available_to_grant = funds − outstanding_grants                  (invariant: ≥ 0)

Shumat janë pozitive; drejtimi vjen nga `entry_type`. Pagesa krijon fonde një herë; granti vetëm i
alokon (s'krijon para). Ky është i VETMI vend ku llogariten totalet. Çdo shkrim thirret me
`money_sequence` të kyçur dhe llogarinë të kyçur (shih `money_sequence`). Pa commit, pa audit këtu.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.central.core.timeutil import utcnow
from apps.central.models.money import (
    ENTRY_TYPES,
    FUNDS_DOWN,
    FUNDS_UP,
    GRANT_ISSUED,
    GRANT_REVERSAL,
    GRANTED_DOWN,
    GRANTED_UP,
    MANUAL_CREDIT,
    MANUAL_DEBIT,
    PAYMENT_CREDIT,
    CommercialLedgerEntry,
    CreditAccount,
)
from apps.central.services import money_sequence

ZERO = Decimal("0.000000")
_Q = Decimal("0.000001")


def _q(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(_Q)


@dataclass(frozen=True, slots=True)
class Totals:
    payment_credits: Decimal
    credit_adjustments: Decimal
    debit_adjustments: Decimal
    grants_issued: Decimal
    grants_reversed: Decimal
    entries: int

    @property
    def funds(self) -> Decimal:
        return self.payment_credits + self.credit_adjustments - self.debit_adjustments

    @property
    def outstanding_grants(self) -> Decimal:
        return self.grants_issued - self.grants_reversed

    @property
    def available_to_grant(self) -> Decimal:
        return self.funds - self.outstanding_grants

    def as_dict(self) -> dict:
        return {"funds": str(self.funds), "outstanding_grants": str(self.outstanding_grants),
                "available_to_grant": str(self.available_to_grant),
                "payment_credits": str(self.payment_credits),
                "credit_adjustments": str(self.credit_adjustments),
                "debit_adjustments": str(self.debit_adjustments),
                "grants_issued": str(self.grants_issued),
                "grants_reversed": str(self.grants_reversed), "entries": self.entries}  # fmt: skip


def totals(db: Session, account_id: uuid.UUID) -> Totals:
    """Totalet nga ledger-i (asnjë balance i ruajtur). Burimi i vetëm i së vërtetës."""
    rows = db.execute(
        select(CommercialLedgerEntry.entry_type, func.sum(CommercialLedgerEntry.amount),
               func.count())
        .where(CommercialLedgerEntry.account_id == account_id)
        .group_by(CommercialLedgerEntry.entry_type)
    ).all()  # fmt: skip
    s = {t: ZERO for t in ENTRY_TYPES}
    n = 0
    for t, total, c in rows:
        s[t] = _q(total)
        n += int(c)
    assert set(FUNDS_UP + FUNDS_DOWN + GRANTED_UP + GRANTED_DOWN) == set(ENTRY_TYPES)
    return Totals(s[PAYMENT_CREDIT], s[MANUAL_CREDIT], s[MANUAL_DEBIT], s[GRANT_ISSUED],
                  s[GRANT_REVERSAL], n)  # fmt: skip


def append(
    db: Session,
    account: CreditAccount,
    entry_type: str,
    amount: Decimal,
    *,
    source_type: str,
    source_id: str,
    actor_user_id=None,
    actor_label: str | None = None,
    reason: str | None = None,
    correlation_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> CommercialLedgerEntry:
    """Shton një hyrje të pandryshueshme me `seq` nga numëruesi transaksional. Thirrësi mban
    `money_sequence` + llogarinë të kyçura. Monedha vjen nga llogaria (FK i përbërë e imponon)."""
    if entry_type not in ENTRY_TYPES:
        raise ValueError(f"unknown entry type {entry_type!r}")
    row = CommercialLedgerEntry(
        seq=money_sequence.next_seq(db), account_id=account.id, currency=account.currency,
        entry_type=entry_type, amount=amount, source_type=source_type, source_id=source_id,
        correlation_id=correlation_id, reason=reason, actor_user_id=actor_user_id,
        actor_label=actor_label, created_at=now or utcnow(),
    )  # fmt: skip
    db.add(row)
    db.flush()
    return row


def history(
    db: Session, account_id: uuid.UUID, *, after_seq: int = 0, limit: int = 100
) -> list[CommercialLedgerEntry]:
    """Vetëm lexim, sipas `seq` rritës."""
    return list(db.scalars(
        select(CommercialLedgerEntry)
        .where(CommercialLedgerEntry.account_id == account_id,
               CommercialLedgerEntry.seq > after_seq)
        .order_by(CommercialLedgerEntry.seq)
        .limit(max(1, min(limit, 500)))
    ))  # fmt: skip
