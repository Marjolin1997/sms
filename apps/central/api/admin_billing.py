"""API admin e faturimit periodik (M9-g1). Admin = shkrim, operator = lexim. Pa DELETE; pa API klienti (faturat Central janë vetëm admin
deri në M11); pa ekzekutim faturimi përmes HTTP (punonjësi/mjeti vjen në g2). Shumat janë string dhjetorë (kurrë float)."""

import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import AfterValidator, BaseModel, Field, StrictInt, StrictStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.api.admin_common import STRICT, Currency, Reason, iso, money_str, page
from apps.central.api.admin_settlement import settlement_out
from apps.central.api.deps import get_db, require_role
from apps.central.core.errors import NotFound
from apps.central.models.billing import (
    BillingPeriod,
    BillingProfile,
    BillingSubscription,
    CommercialPlan,
    Invoice,
    PlanVersion,
)
from apps.central.models.user import CentralUser, Role
from apps.central.services import billing, billing_closure, billing_plans

router = APIRouter(prefix="/admin/billing")
READ = require_role(Role.ADMIN, Role.OPERATOR)
WRITE = require_role(Role.ADMIN)
MAX_PAGE = 200
_DEC = re.compile(r"^(0|[1-9]\d{0,13})(\.\d{1,6})?$")
_VAT = re.compile(r"^(0|1)(\.0{1,4})?$|^0\.\d{1,4}$")


def _fee(v: str) -> str:
    if not _DEC.match(v):
        raise ValueError("amount must be a plain decimal string (<= 6 decimals), never a float")
    return v


def _vat(v: str) -> str:
    if not _VAT.match(v):
        raise ValueError("vat_rate must be a plain decimal string between 0 and 1 (<= 4 decimals)")
    return v


Fee = Annotated[StrictStr, AfterValidator(_fee)]
VatRate = Annotated[StrictStr, AfterValidator(_vat)]
Included = Annotated[StrictInt, Field(ge=0, le=billing_plans.MAX_INCLUDED)]


class PlanIn(BaseModel):
    model_config = STRICT
    code: StrictStr
    name: StrictStr


class VersionIn(BaseModel):
    model_config = STRICT
    currency: Currency
    monthly_fee: Fee
    included_emails: Included = 0


class VersionPatch(BaseModel):
    model_config = STRICT
    monthly_fee: Fee | None = None
    included_emails: Included | None = None


class RetireIn(BaseModel):
    model_config = STRICT
    reason: Reason


class AssignIn(BaseModel):
    model_config = STRICT
    plan_version_id: uuid.UUID


class ProfileIn(BaseModel):
    model_config = STRICT
    legal_name: StrictStr
    address: StrictStr
    country: StrictStr
    email: StrictStr
    tax_id: StrictStr | None = None
    vat_rate: VatRate | None = None


class VoidIn(BaseModel):
    model_config = STRICT
    reason: Annotated[StrictStr, Field(min_length=3, max_length=500)]


# --- serializues --------------------------------------------------------------------------------------------------


def plan_out(p: CommercialPlan) -> dict:
    return {"id": str(p.id), "code": p.code, "name": p.name, "created_at": iso(p.created_at)}


def version_out(v: PlanVersion) -> dict:
    return {
        "id": str(v.id), "plan_id": str(v.plan_id), "version": v.version, "status": v.status, "currency": v.currency,
        "monthly_fee": money_str(v.monthly_fee), "included_emails": v.included_emails, "content_hash": v.content_hash,
        "editable": v.status == "draft", "created_at": iso(v.created_at), "activated_at": iso(v.activated_at),
        "retired_at": iso(v.retired_at), "retire_reason": v.retire_reason,
    }  # fmt: skip


def sub_out(s: BillingSubscription, db: Session) -> dict:
    start, end = billing.period_bounds(s, s.next_period_index)
    status = "scheduled_cancel" if (s.status == "active" and s.cancel_at_period_end) else s.status
    return {
        "id": str(s.id), "enterprise_id": str(s.enterprise_id), "status": s.status, "display_status": status,
        "plan_version_id": str(s.plan_version_id),
        "pending_plan_version_id": None if s.pending_plan_version_id is None else str(s.pending_plan_version_id),
        "cancel_at_period_end": s.cancel_at_period_end, "anchor_started_at": iso(billing.utc(s.anchor_started_at)),
        "next_period_index": s.next_period_index,
        "next_period": {"start": iso(start), "end": iso(end)},
        "cancelled_at": iso(s.cancelled_at), "created_at": iso(s.created_at), "updated_at": iso(s.updated_at),
    }  # fmt: skip


def profile_out(p: BillingProfile) -> dict:
    return {"enterprise_id": str(p.enterprise_id), "legal_name": p.legal_name, "address": p.address, "country": p.country,
            "tax_id": p.tax_id, "email": p.email, "vat_rate": str(p.vat_rate), "updated_at": iso(p.updated_at)}  # fmt: skip


def invoice_out(i: Invoice, lines=None) -> dict:
    out = {
        "id": str(i.id), "number": i.number, "enterprise_id": str(i.enterprise_id), "subscription_id": str(i.subscription_id),
        "period_index": i.period_index, "period_start": iso(i.period_start), "period_end": iso(i.period_end),
        "plan_version_id": str(i.plan_version_id), "currency": i.currency, "subtotal": money_str(i.subtotal),
        "vat_rate": str(i.vat_rate), "tax": money_str(i.tax), "total": money_str(i.total), "status": i.status,
        "bill_to": i.bill_to, "issuer": i.issuer, "issued_at": iso(i.issued_at), "due_at": iso(i.due_at),
        "paid_at": iso(i.paid_at), "voided_at": iso(i.voided_at), "voided_reason": i.voided_reason,
    }  # fmt: skip
    if lines is not None:
        out["lines"] = [
            {"line_no": ln.line_no, "line_type": ln.line_type, "description": ln.description, "quantity": str(ln.quantity.normalize()),
             "unit_price": money_str(ln.unit_price), "amount": money_str(ln.amount), "currency": ln.currency,
             "period_start": iso(ln.period_start), "period_end": iso(ln.period_end),
             "plan_version_id": None if ln.plan_version_id is None else str(ln.plan_version_id),
             "pricing_source": ln.pricing_source,
             "price_book_id": None if ln.price_book_id is None else str(ln.price_book_id),
             "price_version_id": None if ln.price_version_id is None else str(ln.price_version_id),
             "price_rule_id": None if ln.price_rule_id is None else str(ln.price_rule_id)}
            for ln in lines
        ]  # fmt: skip
    return out


def period_out(p: BillingPeriod) -> dict:
    return {
        "id": str(p.id), "subscription_id": str(p.subscription_id), "enterprise_id": str(p.enterprise_id),
        "period_index": p.period_index, "period_start": iso(p.period_start), "period_end": iso(p.period_end),
        "plan_version_id": str(p.plan_version_id), "status": p.status,
        "invoice_id": None if p.invoice_id is None else str(p.invoice_id), "billed_at": iso(p.billed_at),
    }  # fmt: skip


# --- planet ---------------------------------------------------------------------------------------------------


@router.get("/plans")
def list_plans(limit: int = Query(50, ge=1, le=MAX_PAGE), offset: int = Query(0, ge=0), db: Session = Depends(get_db),
               _: CentralUser = Depends(READ)):  # fmt: skip
    rows = list(
        db.scalars(
            select(CommercialPlan)
            .order_by(CommercialPlan.created_at, CommercialPlan.id)
            .limit(limit + 1)
            .offset(offset)
        )
    )
    return page(rows, limit, offset, plan_out)


@router.post("/plans", status_code=201)
def create_plan(body: PlanIn, db: Session = Depends(get_db), actor: CentralUser = Depends(WRITE)):
    p = billing_plans.create_plan(db, actor, body.code, body.name)
    db.commit()
    return plan_out(p)


@router.get("/plans/{plan_id}")
def get_plan(plan_id: uuid.UUID, db: Session = Depends(get_db), _: CentralUser = Depends(READ)):
    p = billing_plans.get_plan(db, plan_id)
    return {
        **plan_out(p),
        "versions": [version_out(v) for v in billing_plans.versions_of(db, p.id)],
    }


@router.post("/plans/{plan_id}/versions", status_code=201)
def create_version(
    plan_id: uuid.UUID,
    body: VersionIn,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    v = billing_plans.new_version(
        db, actor, plan_id, body.currency, body.monthly_fee, body.included_emails
    )
    db.commit()
    return version_out(v)


@router.get("/plan-versions/{version_id}")
def get_version(
    version_id: uuid.UUID, db: Session = Depends(get_db), _: CentralUser = Depends(READ)
):
    return version_out(billing_plans.get_version(db, version_id))


@router.post("/plan-versions/{version_id}/update")
def update_version(
    version_id: uuid.UUID,
    body: VersionPatch,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    v = billing_plans.update_draft(
        db, actor, version_id, monthly_fee=body.monthly_fee, included_emails=body.included_emails
    )
    db.commit()
    return version_out(v)


@router.post("/plan-versions/{version_id}/activate")
def activate_version(
    version_id: uuid.UUID, db: Session = Depends(get_db), actor: CentralUser = Depends(WRITE)
):
    v = billing_plans.activate(db, actor, version_id)
    db.commit()
    return version_out(v)


@router.post("/plan-versions/{version_id}/retire")
def retire_version(
    version_id: uuid.UUID,
    body: RetireIn,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    v = billing_plans.retire(db, actor, version_id, body.reason)
    db.commit()
    return version_out(v)


# --- profilet ---------------------------------------------------------------------------------------------------


@router.get("/profiles/{enterprise_id}")
def get_profile(
    enterprise_id: uuid.UUID, db: Session = Depends(get_db), _: CentralUser = Depends(READ)
):
    p = billing.get_profile(db, enterprise_id)
    if p is None:
        raise NotFound("billing profile not found")
    return profile_out(p)


@router.post("/profiles/{enterprise_id}")
def put_profile(
    enterprise_id: uuid.UUID,
    body: ProfileIn,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    p = billing.set_profile(
        db,
        actor,
        enterprise_id,
        body.legal_name,
        body.address,
        body.country,
        body.email,
        body.tax_id,
        body.vat_rate,
    )
    db.commit()
    return profile_out(p)


# --- abonimet ---------------------------------------------------------------------------------------------------


@router.get("/subscriptions")
def list_subscriptions(status: str | None = Query(None, pattern="^(active|cancelled)$"), limit: int = Query(50, ge=1, le=MAX_PAGE),
                       offset: int = Query(0, ge=0), db: Session = Depends(get_db), _: CentralUser = Depends(READ)):  # fmt: skip
    q = select(BillingSubscription)
    if status:
        q = q.where(BillingSubscription.status == status)
    rows = list(
        db.scalars(
            q.order_by(BillingSubscription.created_at, BillingSubscription.id)
            .limit(limit + 1)
            .offset(offset)
        )
    )
    return page(rows, limit, offset, lambda s: sub_out(s, db))


@router.get("/subscriptions/{enterprise_id}")
def get_subscription(
    enterprise_id: uuid.UUID, db: Session = Depends(get_db), _: CentralUser = Depends(READ)
):
    s = billing.get_subscription(db, enterprise_id)
    if s is None:
        raise NotFound("subscription not found")
    return sub_out(s, db)


@router.post("/subscriptions/{enterprise_id}/assign")
def assign(
    enterprise_id: uuid.UUID,
    body: AssignIn,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    s = billing.assign_plan(db, actor, enterprise_id, body.plan_version_id)
    db.commit()
    return sub_out(s, db)


@router.post("/subscriptions/{enterprise_id}/cancel")
def schedule_cancel(
    enterprise_id: uuid.UUID, db: Session = Depends(get_db), actor: CentralUser = Depends(WRITE)
):
    """Anulim në fund të periudhës aktuale (që faturohet ende)."""
    s = billing.schedule_cancel(db, actor, enterprise_id)
    db.commit()
    return sub_out(s, db)


@router.post("/subscriptions/{enterprise_id}/resume")
def resume(
    enterprise_id: uuid.UUID, db: Session = Depends(get_db), actor: CentralUser = Depends(WRITE)
):
    s = billing.unschedule_cancel(db, actor, enterprise_id)
    db.commit()
    return sub_out(s, db)


# --- periudhat dhe faturat ---------------------------------------------------------------------------------------


@router.get("/periods")
def list_periods(enterprise_id: uuid.UUID | None = None, subscription_id: uuid.UUID | None = None,
                 status: str | None = Query(None, pattern="^(invoiced|no_charge)$"), limit: int = Query(50, ge=1, le=MAX_PAGE),
                 offset: int = Query(0, ge=0), db: Session = Depends(get_db), _: CentralUser = Depends(READ)):  # fmt: skip
    q = select(BillingPeriod)
    if enterprise_id:
        q = q.where(BillingPeriod.enterprise_id == enterprise_id)
    if subscription_id:
        q = q.where(BillingPeriod.subscription_id == subscription_id)
    if status:
        q = q.where(BillingPeriod.status == status)
    rows = list(
        db.scalars(
            q.order_by(BillingPeriod.enterprise_id, BillingPeriod.period_index)
            .limit(limit + 1)
            .offset(offset)
        )
    )
    return page(rows, limit, offset, period_out)


@router.get("/invoices")
def list_invoices(enterprise_id: uuid.UUID | None = None, status: str | None = Query(None, pattern="^(open|paid|void)$"),
                  limit: int = Query(50, ge=1, le=MAX_PAGE), offset: int = Query(0, ge=0), db: Session = Depends(get_db),
                  _: CentralUser = Depends(READ)):  # fmt: skip
    q = select(Invoice)
    if enterprise_id:
        q = q.where(Invoice.enterprise_id == enterprise_id)
    if status:
        q = q.where(Invoice.status == status)
    rows = list(
        db.scalars(q.order_by(Invoice.issued_at.desc(), Invoice.id).limit(limit + 1).offset(offset))
    )
    return page(rows, limit, offset, invoice_out)


@router.get("/invoices/{invoice_id}")
def get_invoice(
    invoice_id: uuid.UUID, db: Session = Depends(get_db), _: CentralUser = Depends(READ)
):
    inv = billing.get_invoice(db, invoice_id)
    return {
        **invoice_out(inv, billing.lines_of(db, inv.id)),
        "settlement": settlement_out(db, inv),
    }  # M9-g3


@router.post("/invoices/{invoice_id}/void")
def void_invoice(
    invoice_id: uuid.UUID,
    body: VoidIn,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    inv = billing.void_invoice(db, actor, invoice_id, body.reason)
    db.commit()
    return invoice_out(inv, billing.lines_of(db, inv.id))


# --- M9-g5: gatishmëria finale dhe pamja operacionale (VETËM LEXIM, pa PII, admin|operator) ----------------------------------------------------


@router.get("/final-readiness")
def final_readiness(db: Session = Depends(get_db), _: CentralUser = Depends(READ)):
    doc = billing_closure.final_readiness(db)
    doc["alerts"] = billing_closure.alerts(doc["checks"], doc["mode"])
    db.rollback()
    return doc


@router.get("/ops")
def ops_overview(db: Session = Depends(get_db), _: CentralUser = Depends(READ)):
    doc = billing_closure.observability(db)
    db.rollback()
    return doc
