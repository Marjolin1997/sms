"""M9-g3: credit notes (Central). Histori financiare e pandryshueshme për korrigjim të faturave të PAGUARA; pa gjendje, pa fshirje, pa rifund automatik, pa wallet SMS.

Rregulla V1: vetëm faturë `paid`; shuma > 0; monedha = monedha e faturës; Σ credit notes të faturës ≤ `invoice.total` (faturë `open` korrigjohet me void, jo me credit note).
Numërimi `CN-{year}-{n:06d}` me rresht të kyçur `FOR UPDATE` (pa `max()+1`; sekuencë e ndarë nga faturat). Idempotent me `(invoice, idempotency_key)`:
e njëjta kërkesë ⇒ credit note ekzistues; kërkesë tjetër me të njëjtin çelës ⇒ Conflict. Kufiri kumulativ mbrohet nga kyçja e faturës + constraint trigger i shtyrë në PG."""

import hashlib
import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.central.core.errors import Conflict, NotFound
from apps.central.core.timeutil import utcnow
from apps.central.models.billing import INV_PAID
from apps.central.models.settlement import CreditNote, CreditNoteSequence
from apps.central.services import audit, billing, money_common

ACTION_ISSUE = "credit_note.issue"
RESOURCE = "credit_note"


def get(db: Session, credit_note_id) -> CreditNote:
    row = db.get(CreditNote, money_common.uid(credit_note_id, "credit note id"))
    if row is None:
        raise NotFound("credit note not found")
    return row


def list_notes(
    db: Session, *, invoice_id=None, enterprise_id=None, limit: int = 100, offset: int = 0
) -> list[CreditNote]:
    q = select(CreditNote)
    if invoice_id is not None:
        q = q.where(CreditNote.invoice_id == money_common.uid(invoice_id, "invoice id"))
    if enterprise_id is not None:
        q = q.where(CreditNote.enterprise_id == money_common.uid(enterprise_id, "enterprise id"))
    q = q.order_by(CreditNote.issued_at, CreditNote.id)
    return list(db.scalars(q.limit(max(1, min(limit, 500))).offset(max(0, offset))))


def credited_total(db: Session, invoice_id) -> Decimal:
    return Decimal(
        db.scalar(
            select(func.coalesce(func.sum(CreditNote.amount), 0)).where(
                CreditNote.invoice_id == invoice_id
            )
        )
    )


def next_number(db: Session, year: int) -> str:
    q = select(CreditNoteSequence).where(CreditNoteSequence.year == year).with_for_update()
    row = db.scalar(q)
    if row is None:
        try:
            with db.begin_nested():
                db.add(CreditNoteSequence(year=year, last_number=0))
                db.flush()
        except IntegrityError:
            pass  # një tjetër e krijoi: e kyçim më poshtë
        row = db.scalar(q.execution_options(populate_existing=True))
    row.last_number += 1
    db.flush()
    return f"CN-{year}-{row.last_number:06d}"


def _fingerprint(amount: Decimal, reason: str) -> str:
    return hashlib.sha256(
        json.dumps({"amount": str(amount), "reason": reason}, sort_keys=True).encode()
    ).hexdigest()


def issue(
    db: Session,
    actor,
    invoice_id,
    amount,
    reason,
    idempotency_key,
    *,
    currency=None,
    now: datetime | None = None,
) -> CreditNote:
    actor = money_common.admin(actor)
    actor_id = actor.id
    amt = money_common.money(amount)
    why = money_common.reason(reason)
    key = money_common.idempotency_key(idempotency_key)
    inv = billing.get_invoice(db, invoice_id, lock=True)
    if currency is not None and money_common.currency(currency) != inv.currency:
        raise Conflict("credit note currency must equal the invoice currency (no FX)")
    fp = _fingerprint(amt, why)
    prior = db.scalar(
        select(CreditNote).where(CreditNote.invoice_id == inv.id, CreditNote.idempotency_key == key)
    )
    if prior is not None:
        if prior.request_hash != fp:
            raise Conflict("idempotency_key was already used with a different credit note request")
        return prior
    if inv.status != INV_PAID:
        raise Conflict(
            f"invoice is {inv.status}: credit notes apply only to paid invoices (void an open invoice instead)"
        )
    already = credited_total(db, inv.id)
    if already + amt > Decimal(inv.total):
        raise Conflict(
            f"credit notes would exceed the invoice total (credited {already}, requested {amt}, total {inv.total})"
        )
    now = billing.utc(now or utcnow())
    note = CreditNote(number=next_number(db, now.year), enterprise_id=inv.enterprise_id, invoice_id=inv.id, currency=inv.currency, amount=amt,
                      reason=why, idempotency_key=key, request_hash=fp, issuer=dict(inv.issuer), bill_to=dict(inv.bill_to), issued_at=now,
                      created_by_id=actor_id, created_at=now)  # fmt: skip
    db.add(note)
    db.flush()
    audit.record(db, actor, ACTION_ISSUE, RESOURCE, note.id,
                 {"number": note.number, "invoice": inv.number, "amount": str(amt), "currency": inv.currency, "reason": why,
                  "credited_total": str(already + amt)}, now=now)  # fmt: skip
    return note
