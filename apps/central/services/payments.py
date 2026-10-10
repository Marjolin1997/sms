"""Pagesat tregtare (M9-b): pending → approved | rejected, maker-checker, një kredit ledger për pagesë.
Pa gateway, pa HTTP, pa commit (transaksioni i thirrësit).

Miratimi është ATOMIK: kyç `money_sequence` → llogarinë → pagesën; valido pending + maker-checker +
llogari aktive; vendos approved; shto saktësisht NJË `payment_credit` në ledger (UNIQUE(entry_type,
source) ndalon të dytin edhe me SQL); audit njeriu. Çdo hap që dështon rikthen gjithçka. `approved`
nuk refuzohet/fshihet kurrë (rikthimi kërkon rrjedhë reversal të ardhshme, jo rishkrim statusi).
Krijuesi është njeri admin ose proces sistemi me etiketë `system:<emër>` (kurrë përdorues i rremë);
miratuesi është gjithmonë admin njeri dhe ≠ krijuesi njeri."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.central.core.errors import Conflict, Invalid, NotFound
from apps.central.core.timeutil import utcnow
from apps.central.models.money import (
    APPROVED,
    PAYMENT_CREDIT,
    PENDING,
    PURPOSE_CREDIT,
    REJECTED,
    Payment,
)
from apps.central.services import (
    audit,
    commercial_ledger,
    credit_accounts,
    money_common,
    money_sequence,
)

ACTION_CREATE, ACTION_APPROVE, ACTION_REJECT = "payment.create", "payment.approve", "payment.reject"
RESOURCE = "payment"


def get(db: Session, payment_id, *, lock: bool = False) -> Payment:
    pid = money_common.uid(payment_id, "payment id")
    q = select(Payment).where(Payment.id == pid)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(q)
    if row is None:
        raise NotFound("payment not found")
    return row


def list_payments(
    db: Session, *, account_id=None, status: str | None = None, limit: int = 100, offset: int = 0
) -> list[Payment]:
    """M9-g3: vetëm pagesat `purpose=credit` (pagesat e faturave shihen te `invoice_payments`)."""
    q = select(Payment).where(Payment.purpose == PURPOSE_CREDIT)
    if account_id is not None:
        q = q.where(Payment.account_id == money_common.uid(account_id, "account id"))
    if status is not None:
        q = q.where(Payment.status == status)
    q = q.order_by(Payment.created_at, Payment.id)
    return list(db.scalars(q.limit(max(1, min(limit, 500))).offset(max(0, offset))))


def create(
    db: Session,
    account_id,
    amount,
    *,
    actor=None,
    system: str | None = None,
    currency=None,
    source: str = "manual",
    external_reference=None,
    note=None,
    now: datetime | None = None,
) -> Payment:
    """Pagesë `pending`. Saktësisht një nga `actor` (admin njeri) ose `system` (etiketë). Monedha
    vjen nga llogaria; nëse jepet duhet të përputhet (V1). `(source, external_reference)` i skopuar:
    e njëjta përmbajtje ⇒ pagesa ekzistuese; përmbajtje tjetër ⇒ Conflict."""
    if (actor is None) == (system is None):
        raise Invalid("exactly one of actor or system is required")
    actor_id = None
    label = None
    if actor is not None:
        actor = money_common.admin(actor)
        actor_id = actor.id
    else:
        label = money_common.system_label(system)
    amt = money_common.money(amount)
    src = money_common.source_name(source)
    ref = money_common.external_reference(external_reference)
    text_note = money_common.optional_text(note, "note")
    acct = credit_accounts.get(db, account_id, lock=True)
    if currency is not None and money_common.currency(currency) != acct.currency:
        raise Conflict("payment currency must equal the account currency (V1: one currency)")
    credit_accounts.require_active(acct)

    def existing() -> Payment | None:
        if ref is None:
            return None
        return db.scalar(select(Payment).where(Payment.source == src,
                                               Payment.external_reference == ref))  # fmt: skip

    def check(p: Payment) -> Payment:
        if (p.account_id, p.amount) != (acct.id, amt):
            raise Conflict("external_reference was already used for a different payment")
        return p

    if (p := existing()) is not None:
        return check(p)
    now = now or utcnow()
    row = Payment(
        enterprise_id=acct.enterprise_id, account_id=acct.id, currency=acct.currency, amount=amt,
        source=src, external_reference=ref, note=text_note, status=PENDING,
        created_by_id=actor_id, created_by_label=label, created_at=now, updated_at=now,
    )  # fmt: skip
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:  # gara mbi (source, external_reference)
        if (winner := existing()) is None:
            raise
        return check(winner)
    detail = {"amount": str(amt), "currency": acct.currency, "account_id": str(acct.id),
              "source": src, "external_reference": ref}  # fmt: skip
    if actor is not None:
        audit.record(db, actor, ACTION_CREATE, RESOURCE, row.id, detail, now=now)
    else:
        audit.record_system(db, label=label, action=ACTION_CREATE, resource_type=RESOURCE,
                            resource_id=row.id, detail=detail, now=now)  # fmt: skip
    return row


def approve(db: Session, payment_id, actor, *, now: datetime | None = None) -> Payment:
    """pending → approved + një `payment_credit`. approved ⇒ no-op (pa audit/kredit të dytë);
    rejected ⇒ Conflict; krijuesi njeri s'e miraton pagesën e vet (maker-checker)."""
    actor = money_common.admin(actor)
    actor_id = actor.id  # lexo para ndryshimeve (actor i skaduar do shkaktonte autoflush)
    pre = get(db, payment_id)
    if (
        pre.purpose != PURPOSE_CREDIT
    ):  # M9-g3: pagesat e faturave miratohen vetëm përmes `invoice_payments.approve`
        raise NotFound("payment not found")
    money_sequence.lock(db)
    acct = credit_accounts.get(db, pre.account_id, lock=True)
    p = get(db, pre.id, lock=True)
    if p.status == APPROVED:
        return p
    if p.status == REJECTED:
        raise Conflict("payment was rejected")
    if p.created_by_id is not None and p.created_by_id == actor_id:
        raise Conflict("maker-checker: the creator cannot approve their own payment")
    credit_accounts.require_active(acct)
    now = now or utcnow()
    p.status, p.approved_at, p.approved_by_id, p.updated_at = APPROVED, now, actor_id, now
    db.flush()
    entry = commercial_ledger.append(
        db, acct, PAYMENT_CREDIT, p.amount, source_type="payment", source_id=str(p.id),
        actor_user_id=actor_id, correlation_id=p.id, now=now,
    )  # fmt: skip
    audit.record(db, actor, ACTION_APPROVE, RESOURCE, p.id,
                 {"amount": str(p.amount), "currency": p.currency, "account_id": str(acct.id),
                  "ledger_seq": entry.seq}, now=now)  # fmt: skip
    return p


def reject(
    db: Session,
    payment_id,
    actor,
    reason,
    *,
    now: datetime | None = None,
    purpose: str = PURPOSE_CREDIT,
) -> Payment:
    """pending → rejected (pa kredit). rejected ⇒ no-op; approved ⇒ Conflict (paraja s'fshihet)."""
    actor = money_common.admin(actor)
    actor_id = actor.id
    why = money_common.reason(reason)
    p = get(db, payment_id, lock=True)
    if p.purpose != purpose:
        raise NotFound("payment not found")
    if p.status == REJECTED:
        return p
    if p.status == APPROVED:
        raise Conflict("an approved payment cannot be rejected; use an explicit reversal")
    now = now or utcnow()
    p.status, p.rejected_at, p.rejected_by_id = REJECTED, now, actor_id
    p.rejection_reason, p.updated_at = why, now
    db.flush()
    audit.record(db, actor, ACTION_REJECT, RESOURCE, p.id,
                 {"amount": str(p.amount), "currency": p.currency, "reason": why}, now=now)  # fmt: skip
    return p
