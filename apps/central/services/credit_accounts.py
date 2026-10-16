"""Llogaritë tregtare të kreditit (M9-b): një për (enterprise, produkt), një monedhë (V1, pa FX),
rregullimet manuale dhe leximet. Pa commit (transaksioni i thirrësit); pa HTTP.

Monedha është e pandryshueshme (DB: FK të përbëra + trigger PG; ORM guard). Llogari e pezulluar:
bllokohen pagesat e reja/miratimi, grant-et e reja dhe rregullimet; reversal-i dhe refuzimi lejohen."""

import hashlib
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.central.core.errors import Conflict, Invalid, NotFound
from apps.central.core.timeutil import utcnow
from apps.central.models.enterprise import Enterprise
from apps.central.models.money import (
    ACCOUNT_ACTIVE,
    ACCOUNT_STATUSES,
    MANUAL_CREDIT,
    MANUAL_DEBIT,
    CommercialLedgerEntry,
    CreditAccount,
)
from apps.central.models.product import Product
from apps.central.services import audit, commercial_ledger, money_common, money_sequence

ACTION_CREATE, ACTION_STATUS = "credit_account.create", "credit_account.status_change"
ACTION_CREDIT_ADJ, ACTION_DEBIT_ADJ = "credit_adjustment.create", "debit_adjustment.create"
RESOURCE = "credit_account"


def get(db: Session, account_id, *, lock: bool = False) -> CreditAccount:
    aid = money_common.uid(account_id, "account id")
    q = select(CreditAccount).where(CreditAccount.id == aid)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(q)
    if row is None:
        raise NotFound("credit account not found")
    return row


def list_accounts(
    db: Session, *, enterprise_id=None, limit: int = 100, offset: int = 0
) -> list[CreditAccount]:
    q = select(CreditAccount)
    if enterprise_id is not None:
        q = q.where(CreditAccount.enterprise_id == money_common.uid(enterprise_id, "enterprise id"))
    q = q.order_by(CreditAccount.created_at, CreditAccount.id)
    return list(db.scalars(q.limit(max(1, min(limit, 500))).offset(max(0, offset))))


def create(
    db: Session, enterprise_id, product_id, currency, actor, *, now: datetime | None = None
) -> CreditAccount:
    """Krijon llogarinë (enterprise, produkt, monedhë). E njëjta (enterprise, produkt) me të njëjtën
    monedhë ⇒ kthen ekzistuesen (idempotent); monedhë tjetër ⇒ Conflict (V1: një monedhë)."""
    actor = money_common.admin(actor)
    actor_id = actor.id
    eid = money_common.uid(enterprise_id, "enterprise id")
    pid = money_common.uid(product_id, "product id")
    cur = money_common.currency(currency)
    if db.get(Enterprise, eid) is None:
        raise NotFound("enterprise not found")
    if db.get(Product, pid) is None:
        raise NotFound("product not found")

    def existing() -> CreditAccount | None:
        return db.scalar(select(CreditAccount).where(
            CreditAccount.enterprise_id == eid, CreditAccount.product_id == pid))  # fmt: skip

    def check(row: CreditAccount) -> CreditAccount:
        if row.currency != cur:
            raise Conflict("this enterprise/product already has an account in another currency")
        return row

    if (row := existing()) is not None:
        return check(row)
    now = now or utcnow()
    row = CreditAccount(enterprise_id=eid, product_id=pid, currency=cur, status=ACCOUNT_ACTIVE,
                        created_at=now, updated_at=now)  # fmt: skip
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:  # gara: UNIQUE(enterprise_id, product_id) vendos
        if (winner := existing()) is None:
            raise
        return check(winner)
    audit.record(db, actor, ACTION_CREATE, RESOURCE, row.id,
                 {"enterprise_id": str(eid), "product_id": str(pid), "currency": cur,
                  "actor": str(actor_id)}, now=now)  # fmt: skip
    return row


def set_status(
    db: Session, account_id, status: str, actor, reason, *, now: datetime | None = None
) -> CreditAccount:
    actor = money_common.admin(actor)
    if status not in ACCOUNT_STATUSES:
        raise Invalid(f"status must be one of {list(ACCOUNT_STATUSES)}")
    why = money_common.reason(reason)
    row = get(db, account_id, lock=True)
    if row.status == status:
        return row
    now = now or utcnow()
    before = row.status
    row.status, row.updated_at = status, now
    db.flush()
    audit.record(db, actor, ACTION_STATUS, RESOURCE, row.id,
                 {"from": before, "to": status, "reason": why}, now=now)  # fmt: skip
    return row


def totals(db: Session, account_id) -> commercial_ledger.Totals:
    return commercial_ledger.totals(db, money_common.uid(account_id, "account id"))


def available_to_grant(db: Session, account_id):
    return totals(db, account_id).available_to_grant


def require_active(account: CreditAccount) -> None:
    if account.status != ACCOUNT_ACTIVE:
        raise Conflict("credit account is suspended")


def adjust(
    db: Session,
    account_id,
    kind: str,
    amount,
    reason,
    actor,
    *,
    idempotency_key,
    now: datetime | None = None,
) -> CommercialLedgerEntry:
    """Rregullim manual: HYRJE ledger (kurrë UPDATE i një balance-i). `kind` ∈ credit|debit; admin njeri;
    arsye e detyrueshme; audit. Debit s'lejohet të rrëzojë `available_to_grant` nën 0. Idempotent
    sipas `idempotency_key` (e njëjta përmbajtje ⇒ e njëjta hyrje; ndryshe Conflict)."""
    actor = money_common.admin(actor)
    actor_id = actor.id
    if kind not in ("credit", "debit"):
        raise Invalid("kind must be 'credit' or 'debit'")
    amt = money_common.money(amount)
    why = money_common.reason(reason)
    key = money_common.idempotency_key(idempotency_key)
    aid = money_common.uid(account_id, "account id")
    entry_type = MANUAL_CREDIT if kind == "credit" else MANUAL_DEBIT
    money_sequence.lock(db)
    acct = get(db, aid, lock=True)
    source_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"central-adjustment:{aid}:{key}"))
    fingerprint = hashlib.sha256(f"{entry_type}|{amt}|{why}".encode()).hexdigest()
    prior = db.scalar(select(CommercialLedgerEntry).where(
        CommercialLedgerEntry.source_type == "adjustment",
        CommercialLedgerEntry.source_id == source_id))  # fmt: skip
    if prior is not None:
        same = (prior.entry_type, prior.amount, prior.reason) == (entry_type, amt, why)
        if not same:
            raise Conflict("idempotency_key was already used with a different adjustment")
        return prior
    require_active(acct)
    if kind == "debit" and amt > commercial_ledger.totals(db, aid).available_to_grant:
        raise Conflict("debit would make the grantable funds negative")
    now = now or utcnow()
    entry = commercial_ledger.append(
        db, acct, entry_type, amt, source_type="adjustment", source_id=source_id,
        actor_user_id=actor_id, reason=why, correlation_id=None, now=now,
    )  # fmt: skip
    audit.record(
        db, actor, ACTION_CREDIT_ADJ if kind == "credit" else ACTION_DEBIT_ADJ, RESOURCE, aid,
        {"amount": str(amt), "currency": acct.currency, "reason": why, "ledger_seq": entry.seq,
         "fingerprint": fingerprint[:16]}, now=now,
    )  # fmt: skip
    return entry
