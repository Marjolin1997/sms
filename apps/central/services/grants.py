"""Grant-et e kredisë (M9-b): alokim i pandryshueshëm i fondeve tregtare të autorizuara. VETËM Central:
asnjë sinkronizim drejt Enterprise (M9-c). Pa commit (transaksioni i thirrësit).

Emetimi është ATOMIK (një transaksion): kyç `money_sequence` → llogarinë → idempotencë → valido
(llogari aktive, fonde të alokueshme ≥ shuma) → grant → hyrje ledger `grant_issued` → ngjarje
`credit_grant.issued` me payload të ngrirë → audit. Grant s'krijon para: lëviz fonde nga "alokueshme"
te "alokuar" (formula te `commercial_ledger`). Korrigjim = reversal + grant i ri.

REVERSAL: në M9-b është NJOHJE TREGTARE (intent/event) — rikthen fondet e alokueshme në Central,
s'fshin asgjë, s'ndryshon shumën. NUK është i plotë operacionalisht: Enterprise mund t'i ketë
shpenzuar tashmë; zbatimi i sigurt në planin operacional (debit ≤ available + gjetje rakordimi)
është M9-c/d. Prandaj mbetet shërbim i brendshëm (pa API publik)."""

import hashlib
import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.core.errors import Conflict, InsufficientFunds, Invalid, NotFound
from apps.central.core.timeutil import utcnow
from apps.central.models.money import (
    APPROVED,
    EVENT_GRANT_ISSUED,
    EVENT_GRANT_REVERSED,
    GRANT_ACTIVE,
    GRANT_ISSUED,
    GRANT_REVERSAL,
    GRANT_REVERSED,
    CreditAccount,
    CreditGrant,
    MoneyEvent,
    Payment,
)
from apps.central.services import (
    audit,
    commercial_ledger,
    credit_accounts,
    money_common,
    money_sequence,
)

ACTION_CREATE, ACTION_REVERSE = "credit_grant.create", "credit_grant.reverse"
RESOURCE = "credit_grant"


def get(db: Session, grant_id, *, lock: bool = False) -> CreditGrant:
    gid = money_common.uid(grant_id, "grant id")
    q = select(CreditGrant).where(CreditGrant.id == gid)
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(q)
    if row is None:
        raise NotFound("credit grant not found")
    return row


def list_grants(
    db: Session, *, account_id=None, status: str | None = None, limit: int = 100, offset: int = 0
) -> list[CreditGrant]:
    q = select(CreditGrant)
    if account_id is not None:
        q = q.where(CreditGrant.account_id == money_common.uid(account_id, "account id"))
    if status is not None:
        q = q.where(CreditGrant.status == status)
    q = q.order_by(CreditGrant.created_at, CreditGrant.id)
    return list(db.scalars(q.limit(max(1, min(limit, 500))).offset(max(0, offset))))


def events_after(db: Session, after_seq: int = 0, limit: int = 100) -> list[MoneyEvent]:
    """Vetëm lexim, sipas `seq` (baza e feed-it të M9-c; ende pa HTTP)."""
    return list(db.scalars(
        select(MoneyEvent).where(MoneyEvent.seq > after_seq).order_by(MoneyEvent.seq)
        .limit(max(1, min(limit, 500)))
    ))  # fmt: skip


def _fmt(amount: Decimal) -> str:
    return format(amount.quantize(Decimal("0.000001")), "f")


def _payload(g: CreditGrant, event_type: str) -> dict:
    """Snapshot i NGRIRË i grantit në çastin e ngjarjes (pa arsye/shënime të brendshme)."""
    base = {
        "grant_id": str(g.id), "account_id": str(g.account_id),
        "enterprise_id": str(g.enterprise_id), "product_id": str(g.product_id),
        "amount": _fmt(g.amount), "currency": g.currency,
    }  # fmt: skip
    if event_type == EVENT_GRANT_ISSUED:
        return {**base, "status": GRANT_ACTIVE,
                "source_payment_id": str(g.source_payment_id) if g.source_payment_id else None,
                "created_at": g.created_at.isoformat()}  # fmt: skip
    return {**base, "status": GRANT_REVERSED, "reversed_at": g.reversed_at.isoformat()}


def _emit(db: Session, g: CreditGrant, event_type: str, now: datetime) -> MoneyEvent:
    ev = MoneyEvent(
        seq=money_sequence.next_seq(db), event_type=event_type, enterprise_id=g.enterprise_id,
        account_id=g.account_id, entity_type="credit_grant", entity_id=g.id,
        payload=_payload(g, event_type), created_at=now,
    )  # fmt: skip
    db.add(ev)
    db.flush()
    return ev


def _fingerprint(amount: Decimal, source_payment_id, note) -> str:
    blob = json.dumps({"amount": _fmt(amount), "source_payment_id": str(source_payment_id)
                       if source_payment_id else None, "note": note}, sort_keys=True)  # fmt: skip
    return hashlib.sha256(blob.encode()).hexdigest()


def issue(
    db: Session,
    account_id,
    amount,
    *,
    idempotency_key,
    actor=None,
    system: str | None = None,
    source_payment_id=None,
    note=None,
    now: datetime | None = None,
) -> CreditGrant:
    """Emeton një grant nga fondet e alokueshme. Idempotent sipas `(account, idempotency_key)`: e
    njëjta përmbajtje ⇒ grant-i ekzistues (pa efekt të dytë); përmbajtje tjetër ⇒ Conflict. Fonde të
    pamjaftueshme ⇒ `InsufficientFunds`; shuma e saktë e disponueshme kalon. Admin njeri OSE proces
    sistemi (`system:<emër>`)."""
    if (actor is None) == (system is None):
        raise Invalid("exactly one of actor or system is required")
    actor_id, label = None, None
    if actor is not None:
        actor = money_common.admin(actor)
        actor_id = actor.id
    else:
        label = money_common.system_label(system)
    amt = money_common.money(amount)
    key = money_common.idempotency_key(idempotency_key)
    text_note = money_common.optional_text(note, "note")
    aid = money_common.uid(account_id, "account id")
    spid = money_common.uid(source_payment_id, "payment id") if source_payment_id else None
    fp = _fingerprint(amt, spid, text_note)
    money_sequence.lock(db)
    acct: CreditAccount = credit_accounts.get(db, aid, lock=True)
    prior = db.scalar(select(CreditGrant).where(CreditGrant.account_id == aid,
                                                CreditGrant.idempotency_key == key))  # fmt: skip
    if prior is not None:
        if prior.request_hash != fp:
            raise Conflict("idempotency_key was already used with a different grant request")
        return prior
    credit_accounts.require_active(acct)
    if spid is not None:
        pay = db.get(Payment, spid)
        if pay is None or pay.account_id != aid:
            raise NotFound("payment not found for this account")
        if pay.status != APPROVED:
            raise Conflict("the referenced payment is not approved")
    available = commercial_ledger.totals(db, aid).available_to_grant
    if amt > available:
        raise InsufficientFunds(f"requested {_fmt(amt)} exceeds grantable funds {_fmt(available)}")
    now = now or utcnow()
    g = CreditGrant(
        account_id=aid, enterprise_id=acct.enterprise_id, product_id=acct.product_id,
        currency=acct.currency, amount=amt, status=GRANT_ACTIVE, idempotency_key=key,
        request_hash=fp, source_payment_id=spid, note=text_note, created_by_id=actor_id,
        created_by_label=label, created_at=now,
    )  # fmt: skip
    db.add(g)
    db.flush()
    entry = commercial_ledger.append(
        db, acct, GRANT_ISSUED, amt, source_type="credit_grant", source_id=str(g.id),
        actor_user_id=actor_id, actor_label=label, correlation_id=g.id, now=now,
    )  # fmt: skip
    ev = _emit(db, g, EVENT_GRANT_ISSUED, now)
    detail = {"grant_id": str(g.id), "amount": str(amt), "currency": acct.currency,
              "account_id": str(aid), "ledger_seq": entry.seq, "event_seq": ev.seq}  # fmt: skip
    if actor is not None:
        audit.record(db, actor, ACTION_CREATE, RESOURCE, g.id, detail, now=now)
    else:
        audit.record_system(db, label=label, action=ACTION_CREATE, resource_type=RESOURCE,
                            resource_id=g.id, detail=detail, now=now)  # fmt: skip
    return g


def reverse(db: Session, grant_id, actor, reason, *, now: datetime | None = None) -> CreditGrant:
    """active → reversed (njohje tregtare; shih docstring-un e modulit). reversed ⇒ no-op (pa fonde
    të dyfishta); arsye e detyrueshme; hyrje `grant_reversal` + ngjarje + audit. Lejohet edhe në
    llogari të pezulluar (rikthen fonde, s'i shpenzon)."""
    actor = money_common.admin(actor)
    actor_id = actor.id
    why = money_common.reason(reason)
    pre = get(db, grant_id)
    money_sequence.lock(db)
    acct = credit_accounts.get(db, pre.account_id, lock=True)
    g = get(db, pre.id, lock=True)
    if g.status == GRANT_REVERSED:
        return g
    now = now or utcnow()
    g.status, g.reversed_at, g.reversed_by_id, g.reversal_reason = (
        GRANT_REVERSED,
        now,
        actor_id,
        why,
    )
    db.flush()
    entry = commercial_ledger.append(
        db, acct, GRANT_REVERSAL, g.amount, source_type="credit_grant", source_id=str(g.id),
        actor_user_id=actor_id, reason=why, correlation_id=g.id, now=now,
    )  # fmt: skip
    ev = _emit(db, g, EVENT_GRANT_REVERSED, now)
    audit.record(db, actor, ACTION_REVERSE, RESOURCE, g.id,
                 {"grant_id": str(g.id), "amount": str(g.amount), "currency": g.currency,
                  "reason": why, "ledger_seq": entry.seq, "event_seq": ev.seq}, now=now)  # fmt: skip
    return g
