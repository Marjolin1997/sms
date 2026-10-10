from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.tenant import scoped, tenant
from app.core.db import get_db
from app.core.errors import DomainError
from app.core.scope import owned
from app.core.security import Principal, require
from app.models.billing import (
    Invoice,
    Payment,
    Plan,
    Subscription,
)
from app.services import billing as svc
from app.services import invoice_render, payments
from app.services.audit import audit

router = APIRouter(prefix="/v1")
_STATUS = {
    "not_found": 404,
    "conflict": 409,
    "insufficient_funds": 402,
    "gateway_error": 502,
    "payments_disabled": 503,
    "billing_authority_frozen": 409,
}


def _run(db: Session, fn):
    try:
        out = fn()
        db.commit()
        return out
    except DomainError as e:
        db.rollback()
        raise HTTPException(_STATUS.get(e.code, 422), {"code": e.code, "message": str(e)}) from e


def _plan_out(p: Plan) -> dict:
    return {
        "id": p.id, "code": p.code, "name": p.name, "currency": p.currency,
        "monthly_fee": str(p.monthly_fee), "included_emails": p.included_emails,
        "email_overage_price": str(p.email_overage_price), "status": p.status.value,
    }  # fmt: skip


def _inv_out(i: Invoice) -> dict:
    return {
        "id": i.id, "number": i.number, "status": i.status.value, "currency": i.currency,
        "subtotal": str(i.subtotal), "vat_rate": str(i.vat_rate), "tax": str(i.tax),
        "total": str(i.total), "issued_at": i.issued_at, "due_at": i.due_at, "paid_at": i.paid_at,
        "paid_via": i.paid_via, "period_start": i.period_start, "period_end": i.period_end,
    }  # fmt: skip


def _pay_out(p: Payment) -> dict:
    return {
        "id": p.id, "purpose": p.purpose.value, "invoice_id": p.invoice_id,
        "wallet_id": p.wallet_id,
        "amount": str(p.amount), "currency": p.currency, "status": p.status.value,
        "checkout_url": p.checkout_url if p.status.value == "pending" else None,
        "failure_reason": p.failure_reason, "created_at": p.created_at,
    }  # fmt: skip


# --- Klienti ------------------------------------------------------------------------------


class ProfileIn(BaseModel):
    legal_name: str = Field(min_length=2, max_length=120)
    address: str = Field(min_length=3, max_length=300)
    country: str = Field(min_length=2, max_length=2)
    email: str = Field(max_length=254)
    tax_id: str | None = Field(default=None, max_length=40)


@router.get("/billing/profile")
def get_profile(
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("billing:read")),
):
    prof = svc.get_profile(db, tenant(db, p, owner_ref))
    if prof is None:
        return None
    return {"legal_name": prof.legal_name, "address": prof.address, "country": prof.country,
            "email": prof.email, "tax_id": prof.tax_id, "vat_rate": str(prof.vat_rate)}  # fmt: skip


@router.put("/billing/profile")
def put_profile(
    body: ProfileIn,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("billing:profile")),
):
    """Klienti përditëson vetëm të dhënat ligjore; norma e TVSH-së vendoset nga stafi."""
    owner = tenant(db, p, owner_ref, write=True)

    def go():
        prof = svc.set_profile(db, owner, **body.model_dump())
        detail = {"country": prof.country}
        audit(db, p, "billing.profile", "billing_profile", owner.owner_ref, detail)
        return prof

    prof = _run(db, go)
    return {"legal_name": prof.legal_name, "vat_rate": str(prof.vat_rate)}


@router.get("/billing/subscription")
def get_subscription(
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("billing:read")),
):
    owner = tenant(db, p, owner_ref)
    sub = db.scalar(select(Subscription).where(owned(Subscription, owner)))
    if sub is None:
        return None
    plan = db.get(Plan, sub.plan_id)
    start, end = svc.period(sub)
    used = svc.email_usage(db, owner, start, end)
    extra = max(0, used - plan.included_emails)
    return {
        "status": sub.status.value, "plan": _plan_out(plan), "auto_pay": sub.auto_pay,
        "pending_plan": (
            _plan_out(db.get(Plan, sub.pending_plan_id)) if sub.pending_plan_id else None
        ),
        "cancel_at_period_end": sub.cancel_at_period_end,
        "period_start": start, "period_end": end,
        "usage": {"emails": used, "included": plan.included_emails, "overage": extra,
                  "overage_amount": str(svc.cents(Decimal(extra) * plan.email_overage_price))},
    }  # fmt: skip


@router.get("/billing/invoices")
def list_invoices(
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("billing:read")),
):
    owner = tenant(db, p, owner_ref)
    rows = db.scalars(
        select(Invoice).where(owned(Invoice, owner)).order_by(Invoice.id.desc()).limit(200)
    )
    return [_inv_out(i) for i in rows]


def _own_invoice(db: Session, invoice_id: int, p: Principal, owner_ref: str | None) -> Invoice:
    stmt = scoped(db, p, Invoice, select(Invoice).where(Invoice.id == invoice_id))
    inv = db.scalar(stmt)
    if inv is None:
        raise HTTPException(404, {"code": "not_found", "message": "invoice not found"})
    return inv


@router.get("/billing/invoices/{invoice_id}")
def get_invoice(
    invoice_id: int,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("billing:read")),
):
    inv = _own_invoice(db, invoice_id, p, owner_ref)
    lines = [{"description": ln.description, "quantity": str(ln.quantity.normalize()),
              "unit_price": str(ln.unit_price), "amount": str(ln.amount)}
             for ln in svc.invoice_lines(db, inv.id)]  # fmt: skip
    return {**_inv_out(inv), "lines": lines}


@router.get("/billing/invoices/{invoice_id}/html", response_class=HTMLResponse)
def invoice_page(
    invoice_id: int,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("billing:read")),
):
    inv = _own_invoice(db, invoice_id, p, owner_ref)
    return HTMLResponse(
        invoice_render.invoice_html(inv, svc.invoice_lines(db, inv.id)),
        headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
            "frame-ancestors 'none'"
        },
    )


@router.post("/billing/invoices/{invoice_id}/pay-from-wallet")
def pay_from_wallet(
    invoice_id: int,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("billing:pay")),
):
    owner = tenant(db, p, owner_ref)

    def go():
        inv = svc.pay_from_wallet(db, owner, invoice_id)
        audit(db, p, "invoice.pay_wallet", "invoice", inv.number)
        return inv

    return _inv_out(_run(db, go))


class PaymentIn(BaseModel):
    purpose: str = Field(pattern="^(topup|invoice)$")
    amount: Decimal | None = Field(default=None, gt=0, max_digits=20, decimal_places=6)
    invoice_id: int | None = None
    wallet_id: int | None = None


@router.post("/billing/payments", status_code=201)
def create_payment(
    body: PaymentIn,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("billing:pay")),
):
    """Shuma e faturës merret nga serveri; klienti s'mund ta ndryshojë."""
    owner = tenant(db, p, owner_ref)

    def go():
        pay = payments.start_payment(
            db, owner, body.purpose, body.amount, body.invoice_id, body.wallet_id
        )
        audit(db, p, "payment.start", "payment", pay.id, {"purpose": body.purpose})
        return pay

    return _pay_out(_run(db, go))


@router.get("/billing/payments")
def list_payments(
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("billing:read")),
):
    owner = tenant(db, p, owner_ref)
    rows = db.scalars(
        select(Payment).where(owned(Payment, owner)).order_by(Payment.id.desc()).limit(200)
    )
    return [_pay_out(x) for x in rows]


# --- Stafi --------------------------------------------------------------------------------


class PlanIn(BaseModel):
    code: str = Field(min_length=2, max_length=32)
    name: str = Field(min_length=1, max_length=80)
    currency: str = Field(min_length=3, max_length=3)
    monthly_fee: Decimal = Field(ge=0, max_digits=20, decimal_places=6)
    included_emails: int = Field(default=0, ge=0)
    email_overage_price: Decimal = Field(default=Decimal(0), ge=0, max_digits=20, decimal_places=6)


@router.post("/admin/billing/plans", status_code=201)
def create_plan(
    body: PlanIn, db: Session = Depends(get_db), p: Principal = Depends(require("billing:admin"))
):
    def go():
        plan = svc.create_plan(db, **body.model_dump())
        audit(db, p, "plan.create", "plan", plan.code, body.model_dump())
        return plan

    return _plan_out(_run(db, go))


@router.get("/admin/billing/plans")
def list_plans(db: Session = Depends(get_db), _: Principal = Depends(require("billing:read"))):
    return [_plan_out(x) for x in db.scalars(select(Plan).order_by(Plan.id))]


@router.post("/admin/billing/plans/{plan_id}/retire")
def retire_plan(
    plan_id: int, db: Session = Depends(get_db), p: Principal = Depends(require("billing:admin"))
):
    def go():
        plan = svc.retire_plan(db, plan_id)
        audit(db, p, "plan.retire", "plan", plan.code)
        return plan

    return _plan_out(_run(db, go))


class AssignIn(BaseModel):
    plan_id: int
    auto_pay: bool = True


@router.put("/admin/billing/{owner}/subscription")
def assign(
    owner: str, body: AssignIn, db: Session = Depends(get_db),
    p: Principal = Depends(require("billing:admin")),
):  # fmt: skip
    def go():
        sub = svc.assign_plan(db, owner, body.plan_id, body.auto_pay)
        audit(db, p, "subscription.assign", "subscription", owner, body.model_dump())
        return sub

    sub = _run(db, go)
    return {
        "status": sub.status.value,
        "plan_id": sub.plan_id,
        "pending_plan_id": sub.pending_plan_id,
    }


@router.post("/admin/billing/{owner}/subscription/cancel")
def cancel(
    owner: str, db: Session = Depends(get_db), p: Principal = Depends(require("billing:admin"))
):
    def go():
        sub = svc.cancel_subscription(db, owner)
        audit(db, p, "subscription.cancel", "subscription", owner)
        return sub

    _run(db, go)
    return {"cancel_at_period_end": True}


class VatIn(ProfileIn):
    vat_rate: Decimal = Field(ge=0, le=1, max_digits=6, decimal_places=4)


@router.put("/admin/billing/{owner}/profile")
def admin_profile(
    owner: str, body: VatIn, db: Session = Depends(get_db),
    p: Principal = Depends(require("billing:admin")),
):  # fmt: skip
    def go():
        prof = svc.set_profile(db, owner, **body.model_dump())
        audit(
            db,
            p,
            "billing.profile_admin",
            "billing_profile",
            owner,
            {"vat_rate": str(prof.vat_rate)},
        )
        return prof

    prof = _run(db, go)
    return {"legal_name": prof.legal_name, "vat_rate": str(prof.vat_rate)}


@router.post("/admin/billing/run")
def run_now(db: Session = Depends(get_db), p: Principal = Depends(require("billing:admin"))):
    n = _run(db, lambda: svc.run_billing(db))  # M9-g4: central ⇒ 409
    audit(db, p, "billing.run", "billing", "manual", {"issued": n})
    db.commit()
    return {"issued": n}


class VoidIn(BaseModel):
    reason: str = Field(min_length=3, max_length=200)


@router.post("/admin/billing/invoices/{invoice_id}/void")
def void(
    invoice_id: int, body: VoidIn, db: Session = Depends(get_db),
    p: Principal = Depends(require("billing:admin")),
):  # fmt: skip
    def go():
        inv = svc.void_invoice(db, invoice_id, body.reason)
        audit(db, p, "invoice.void", "invoice", inv.number, {"reason": body.reason})
        return inv

    return _inv_out(_run(db, go))


@router.get("/admin/billing/overdue")
def overdue(db: Session = Depends(get_db), _: Principal = Depends(require("billing:read"))):
    return [{**_inv_out(i), "owner_ref": i.owner_ref} for i in svc.overdue(db)]
