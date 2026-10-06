"""API admin për paranë tregtare (M9-f). Admin = shkrim, operator = vetëm lexim; maker-checker mbetet në shërbim.
Pa DELETE, pa API klienti. Shumat janë string dhjetorë (kurrë float); çdo mutacion auditohet nga shërbimi."""

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, StrictStr
from sqlalchemy.orm import Session

from apps.central.api.admin_common import (
    STRICT,
    Amount,
    Currency,
    IdempotencyKey,
    Note,
    Reason,
    actor_of,
    iso,
    money_str,
    page,
)
from apps.central.api.deps import get_db, require_role
from apps.central.core.errors import Invalid, NotFound
from apps.central.models.money import (
    ACCOUNT_STATUSES,
    CommercialLedgerEntry,
    CreditAccount,
    CreditGrant,
    Payment,
)
from apps.central.models.usage import UsageReport
from apps.central.models.user import CentralUser, Role
from apps.central.services import (
    commercial_ledger,
    credit_accounts,
    grants,
    money_reconciliation,
    payments,
    usage_reports,
)

router = APIRouter(prefix="/admin/money")
READ = require_role(Role.ADMIN, Role.OPERATOR)
WRITE = require_role(Role.ADMIN)
MAX_PAGE = 200


# --- skema -----------------------------------------------------------------------------------------------------


class StatusIn(BaseModel):
    model_config = STRICT
    status: StrictStr
    reason: Reason


class AdjustmentIn(BaseModel):
    model_config = STRICT
    kind: StrictStr
    amount: Amount
    reason: Reason
    idempotency_key: IdempotencyKey


class PaymentIn(BaseModel):
    model_config = STRICT
    account_id: uuid.UUID
    amount: Amount
    currency: Currency | None = None
    source: StrictStr = "manual"
    external_reference: StrictStr | None = None
    note: Note | None = None


class RejectIn(BaseModel):
    model_config = STRICT
    reason: Reason


class GrantIn(BaseModel):
    """Vetëm grant `standard`; grant-et `bootstrap` krijohen nga mjeti i cutover-it, jo nga API."""

    model_config = STRICT
    account_id: uuid.UUID
    amount: Amount
    idempotency_key: IdempotencyKey
    source_payment_id: uuid.UUID | None = None
    note: Note | None = None


# --- serializues (asnjë ORM jashtë) ---------------------------------------------------------------------------------


def account_out(a: CreditAccount, with_totals: bool = False, db: Session | None = None) -> dict:
    out = {"id": str(a.id), "enterprise_id": str(a.enterprise_id), "product_id": str(a.product_id),
           "currency": a.currency, "status": a.status, "created_at": iso(a.created_at),
           "updated_at": iso(a.updated_at)}  # fmt: skip
    if with_totals and db is not None:
        out["totals"] = credit_accounts.totals(db, a.id).as_dict()
    return out


def payment_out(p: Payment) -> dict:
    return {
        "id": str(p.id), "account_id": str(p.account_id), "enterprise_id": str(p.enterprise_id),
        "currency": p.currency, "amount": money_str(p.amount), "source": p.source,
        "external_reference": p.external_reference, "note": p.note, "status": p.status,
        "created_by": actor_of(p.created_by_id, p.created_by_label), "created_at": iso(p.created_at),
        "approved_by": actor_of(p.approved_by_id, None), "approved_at": iso(p.approved_at),
        "rejected_by": actor_of(p.rejected_by_id, None), "rejected_at": iso(p.rejected_at),
        "rejection_reason": p.rejection_reason,
    }  # fmt: skip


def grant_out(g: CreditGrant) -> dict:
    return {
        "id": str(g.id), "account_id": str(g.account_id), "enterprise_id": str(g.enterprise_id),
        "product_id": str(g.product_id), "currency": g.currency, "amount": money_str(g.amount),
        "status": g.status, "purpose": g.purpose, "source_payment_id": None if g.source_payment_id is None else str(g.source_payment_id),
        "note": g.note, "created_by": actor_of(g.created_by_id, g.created_by_label), "created_at": iso(g.created_at),
        "reversed_by": actor_of(g.reversed_by_id, None), "reversed_at": iso(g.reversed_at),
        "reversal_reason": g.reversal_reason,
    }  # fmt: skip


def ledger_out(e: CommercialLedgerEntry) -> dict:
    return {"seq": e.seq, "entry_type": e.entry_type, "amount": money_str(e.amount), "currency": e.currency,
            "source_type": e.source_type, "source_id": e.source_id, "reason": e.reason,
            "actor": actor_of(e.actor_user_id, e.actor_label), "created_at": iso(e.created_at)}  # fmt: skip


def report_summary(r: UsageReport) -> dict:
    d = r.payload
    return {
        "report_id": str(r.report_id), "enterprise_id": str(r.enterprise_id), "product_id": str(r.product_id),
        "currency": r.currency, "report_seq": r.report_seq, "authority_mode": r.authority_mode,
        "generated_at": iso(r.generated_at), "received_at": iso(r.received_at), "ledger_max_id": r.ledger_max_id,
        "money_cursor_seq": r.money_cursor_seq, "available": money_str(r.available), "held": money_str(r.held),
        "gross": money_str(r.gross), "flows": d.get("flows"), "grants": len(d.get("grants", [])),
    }  # fmt: skip


# --- llogaritë -----------------------------------------------------------------------------------------------------


@router.get("/accounts")
def list_accounts(
    enterprise_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: CentralUser = Depends(READ),
):
    rows = credit_accounts.list_accounts(
        db, enterprise_id=enterprise_id, limit=limit + 1, offset=offset
    )
    return page(rows, limit, offset, lambda a: account_out(a, True, db))


@router.get("/accounts/{account_id}")
def get_account(
    account_id: uuid.UUID, db: Session = Depends(get_db), _: CentralUser = Depends(READ)
):
    return account_out(credit_accounts.get(db, account_id), True, db)


@router.post("/accounts/{account_id}/status")
def set_account_status(account_id: uuid.UUID, body: StatusIn, db: Session = Depends(get_db),
                       actor: CentralUser = Depends(WRITE)):  # fmt: skip
    if body.status not in ACCOUNT_STATUSES:
        raise Invalid(f"status must be one of {list(ACCOUNT_STATUSES)}")
    row = credit_accounts.set_status(db, account_id, body.status, actor, body.reason)
    db.commit()
    return account_out(row, True, db)


@router.post("/accounts/{account_id}/adjustments", status_code=201)
def create_adjustment(account_id: uuid.UUID, body: AdjustmentIn, db: Session = Depends(get_db),
                      actor: CentralUser = Depends(WRITE)):  # fmt: skip
    entry = credit_accounts.adjust(db, account_id, body.kind, body.amount, body.reason, actor,
                                   idempotency_key=body.idempotency_key)  # fmt: skip
    db.commit()
    return ledger_out(entry)


@router.get("/accounts/{account_id}/ledger")
def account_ledger(
    account_id: uuid.UUID,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: CentralUser = Depends(READ),
):
    """Histori vetëm-lexim, sipas `seq` rritës; vazhdo me `next_after_seq`."""
    credit_accounts.get(db, account_id)  # 404 për llogari të panjohur
    rows = commercial_ledger.history(db, account_id, after_seq=after_seq, limit=limit)
    return {"items": [ledger_out(e) for e in rows], "limit": limit,
            "next_after_seq": rows[-1].seq if len(rows) == limit else None}  # fmt: skip


# --- pagesat ----------------------------------------------------------------------------------------------------


@router.get("/payments")
def list_payments(
    account_id: uuid.UUID | None = None,
    status: str | None = Query(None, pattern="^(pending|approved|rejected)$"),
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: CentralUser = Depends(READ),
):
    rows = payments.list_payments(
        db, account_id=account_id, status=status, limit=limit + 1, offset=offset
    )
    return page(rows, limit, offset, payment_out)


@router.post("/payments", status_code=201)
def create_payment(
    body: PaymentIn, db: Session = Depends(get_db), actor: CentralUser = Depends(WRITE)
):
    p = payments.create(db, body.account_id, body.amount, actor=actor, currency=body.currency,
                        source=body.source, external_reference=body.external_reference, note=body.note)  # fmt: skip
    db.commit()
    return payment_out(p)


@router.get("/payments/{payment_id}")
def get_payment(
    payment_id: uuid.UUID, db: Session = Depends(get_db), _: CentralUser = Depends(READ)
):
    return payment_out(payments.get(db, payment_id))


@router.post("/payments/{payment_id}/approve")
def approve_payment(
    payment_id: uuid.UUID, db: Session = Depends(get_db), actor: CentralUser = Depends(WRITE)
):
    p = payments.approve(db, payment_id, actor)
    db.commit()
    return payment_out(p)


@router.post("/payments/{payment_id}/reject")
def reject_payment(payment_id: uuid.UUID, body: RejectIn, db: Session = Depends(get_db),
                   actor: CentralUser = Depends(WRITE)):  # fmt: skip
    p = payments.reject(db, payment_id, actor, body.reason)
    db.commit()
    return payment_out(p)


# --- grant-et ---------------------------------------------------------------------------------------------------


@router.get("/grants")
def list_grants(
    account_id: uuid.UUID | None = None,
    status: str | None = Query(None, pattern="^(active|reversed)$"),
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: CentralUser = Depends(READ),
):
    rows = grants.list_grants(
        db, account_id=account_id, status=status, limit=limit + 1, offset=offset
    )
    return page(rows, limit, offset, grant_out)


@router.post("/grants", status_code=201)
def create_grant(body: GrantIn, db: Session = Depends(get_db), actor: CentralUser = Depends(WRITE)):
    g = grants.issue(db, body.account_id, body.amount, idempotency_key=body.idempotency_key, actor=actor,
                     source_payment_id=body.source_payment_id, note=body.note)  # fmt: skip
    db.commit()
    return grant_out(g)


@router.get("/grants/{grant_id}")
def get_grant(grant_id: uuid.UUID, db: Session = Depends(get_db), _: CentralUser = Depends(READ)):
    return grant_out(grants.get(db, grant_id))


@router.post("/grants/{grant_id}/reverse")
def reverse_grant(grant_id: uuid.UUID, body: RejectIn, db: Session = Depends(get_db),
                  actor: CentralUser = Depends(WRITE)):  # fmt: skip
    g = grants.reverse(db, grant_id, actor, body.reason)
    db.commit()
    return grant_out(g)


# --- rakordimi dhe raportet e përdorimit (vetëm lexim) ---------------------------------------------------------------


@router.get("/reconciliation")
def reconciliation(
    enterprise_id: uuid.UUID | None = None,
    min_severity: str = Query("WARN", pattern="^(INFO|WARN|FAIL|CRITICAL)$"),
    db: Session = Depends(get_db),
    _: CentralUser = Depends(READ),
):
    """Verdikti aktual (llogaritur mbi raportet më të fundit; pa shkrim) + diskrepancat ≥ `min_severity`."""
    res = money_reconciliation.reconcile(db, enterprise_id=enterprise_id)
    rank = money_reconciliation._RANK
    doc = res.to_dict()
    doc["discrepancies"] = [
        d for d in doc["discrepancies"] if rank[d["severity"]] >= rank[min_severity]
    ]
    doc["min_severity"] = min_severity
    db.rollback()
    return doc


@router.get("/usage-reports")
def latest_usage_reports(enterprise_id: uuid.UUID | None = None, db: Session = Depends(get_db),
                         _: CentralUser = Depends(READ)):  # fmt: skip
    """Raporti aktual per (enterprise, product, currency)."""
    return {"items": [report_summary(r) for r in usage_reports.latest_per_key(db, enterprise_id)]}


@router.get("/usage-reports/history")
def usage_report_history(
    enterprise_id: uuid.UUID,
    product_id: uuid.UUID,
    currency: str = Query(pattern="^[A-Z]{3}$"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _: CentralUser = Depends(READ),
):
    return {
        "items": [
            report_summary(r)
            for r in usage_reports.history(db, enterprise_id, product_id, currency, limit)
        ]
    }


@router.get("/usage-reports/{report_id}")
def usage_report_detail(
    report_id: uuid.UUID, db: Session = Depends(get_db), _: CentralUser = Depends(READ)
):
    r = db.get(UsageReport, report_id)
    if r is None:
        raise NotFound("usage report not found")
    return {**report_summary(r), "payload": r.payload}
