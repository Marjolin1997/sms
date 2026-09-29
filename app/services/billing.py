"""Faturimi i abonimeve. Parimet:

- Faturat lëshohen të pandryshueshme (ORM + trigger DB), me numër pa boshllëqe dhe fotografi të
  profilit; çdo periudhë faturohet një herë (UNIQUE subscription+period).
- Pagesa nga wallet është hyrje ledger idempotente; online vjen nga webhook (services/payments).
- Plani i ri hyn në fuqi nga periudha tjetër; anulimi nga fundi i periudhës. Pa proporcion.
"""

import calendar
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import as_utc
from app.models.billing import (
    BillingProfile,
    Invoice,
    InvoiceCounter,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    Plan,
    PlanStatus,
    Subscription,
    SubStatus,
)
from app.models.email import Email, EmailStatus
from app.models.wallet import Wallet
from app.services import events
from app.services import wallet as wallets
from app.services.wallet import Conflict, InvalidAmount, NotFound, WalletError

log = logging.getLogger("sms.billing")
CENT = Decimal("0.01")
BILLABLE_EMAIL = (
    EmailStatus.SENT,
    EmailStatus.DELIVERED,
    EmailStatus.BOUNCED,
    EmailStatus.COMPLAINED,
)
_CODE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


class InvalidBilling(WalletError):
    code = "invalid_billing"


def cents(x: Decimal) -> Decimal:
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def add_months(dt: datetime, n: int) -> datetime:
    """Shton n muaj kalendarikë; dita kufizohet në ditën e fundit (31 jan + 1 = 28/29 shk)."""
    total = dt.year * 12 + (dt.month - 1) + n
    y, m = divmod(total, 12)
    day = min(dt.day, calendar.monthrange(y, m + 1)[1])
    return dt.replace(year=y, month=m + 1, day=day)


# --- Plane -----------------------------------------------------------------------------


def create_plan(
    db: Session, code: str, name: str, currency: str, monthly_fee, included_emails: int = 0,
    email_overage_price="0",
) -> Plan:  # fmt: skip
    if not _CODE.match(code):
        raise InvalidBilling("code must be 2-32 chars of a-z, 0-9, _ or -")
    if db.scalar(select(Plan).where(Plan.code == code)):
        raise Conflict("plan code already exists")
    if included_emails < 0:
        raise InvalidBilling("included_emails must be >= 0")
    fee = wallets.money(monthly_fee)
    price = wallets.money(email_overage_price)
    if fee < 0 or price < 0:
        raise InvalidAmount("prices must be >= 0")
    p = Plan(
        code=code, name=name, currency=currency.upper(), monthly_fee=fee,
        included_emails=included_emails, email_overage_price=price,
    )  # fmt: skip
    db.add(p)
    db.flush()
    return p


def retire_plan(db: Session, plan_id: int) -> Plan:
    p = db.get(Plan, plan_id, with_for_update=True)
    if p is None:
        raise NotFound("plan not found")
    p.status = PlanStatus.RETIRED
    db.flush()
    return p


# --- Profil ----------------------------------------------------------------------------


def set_profile(
    db: Session, owner_ref: str, legal_name: str, address: str, country: str, email: str,
    tax_id: str | None = None, vat_rate=None,
) -> BillingProfile:  # fmt: skip
    from app.services import consent

    try:
        email = consent.normalize("email", email)
    except consent.InvalidAddress as e:
        raise InvalidBilling(str(e)) from e
    if not re.fullmatch(r"[A-Za-z]{2}", country):
        raise InvalidBilling("country must be ISO alpha-2")
    prof = db.scalar(select(BillingProfile).where(BillingProfile.owner_ref == owner_ref))
    if prof is None:
        prof = BillingProfile(owner_ref=owner_ref, vat_rate=Decimal(0), legal_name="", address="",
                              country="", email="")  # fmt: skip
        db.add(prof)
    prof.legal_name, prof.address = legal_name.strip(), address.strip()
    prof.country, prof.email, prof.tax_id = country.upper(), email, (tax_id or None)
    if vat_rate is not None:
        rate = Decimal(str(vat_rate))
        if not Decimal(0) <= rate <= Decimal(1):
            raise InvalidBilling("vat_rate must be between 0 and 1")
        prof.vat_rate = rate
    prof.updated_at = datetime.now(UTC)
    db.flush()
    return prof


def get_profile(db: Session, owner_ref: str) -> BillingProfile | None:
    return db.scalar(select(BillingProfile).where(BillingProfile.owner_ref == owner_ref))


# --- Abonime ---------------------------------------------------------------------------


def assign_plan(
    db: Session, owner_ref: str, plan_id: int, auto_pay: bool = True, now: datetime | None = None
) -> Subscription:
    now = as_utc(now or datetime.now(UTC))
    plan = db.get(Plan, plan_id)
    if plan is None or plan.status != PlanStatus.ACTIVE:
        raise InvalidBilling("plan not found or retired")
    if get_profile(db, owner_ref) is None:
        raise InvalidBilling("a billing profile is required before subscribing")
    sub = db.scalar(
        select(Subscription).where(Subscription.owner_ref == owner_ref).with_for_update()
    )
    if sub is None:
        sub = Subscription(owner_ref=owner_ref, plan_id=plan.id, started_at=now, auto_pay=auto_pay)
        db.add(sub)
    elif sub.status == SubStatus.CANCELLED:  # rifillim: ankorë e re
        sub.plan_id, sub.pending_plan_id, sub.status = plan.id, None, SubStatus.ACTIVE
        sub.started_at, sub.periods_billed, sub.cancel_at_period_end = now, 0, False
        sub.auto_pay = auto_pay
    else:
        current = db.get(Plan, sub.plan_id)
        if current.currency != plan.currency:
            raise InvalidBilling("cannot switch to a plan in another currency")
        sub.pending_plan_id = None if plan.id == sub.plan_id else plan.id
        sub.cancel_at_period_end = False
        sub.auto_pay = auto_pay
    db.flush()
    return sub


def cancel_subscription(db: Session, owner_ref: str) -> Subscription:
    sub = db.scalar(
        select(Subscription).where(Subscription.owner_ref == owner_ref).with_for_update()
    )
    if sub is None or sub.status != SubStatus.ACTIVE:
        raise NotFound("no active subscription")
    sub.cancel_at_period_end = True
    db.flush()
    return sub


def period(sub: Subscription, k: int | None = None) -> tuple[datetime, datetime]:
    k = sub.periods_billed if k is None else k
    start = as_utc(sub.started_at)
    return add_months(start, k), add_months(start, k + 1)


def email_usage(db: Session, owner_ref: str, start: datetime, end: datetime) -> int:
    return db.scalar(
        select(func.count())
        .select_from(Email)
        .where(
            Email.owner_ref == owner_ref,
            Email.created_at >= start,
            Email.created_at < end,
            Email.status.in_(BILLABLE_EMAIL),
        )
    )


# --- Numërim dhe lëshim --------------------------------------------------------------------


def _next_number(db: Session, year: int) -> str:
    row = db.scalar(select(InvoiceCounter).where(InvoiceCounter.year == year).with_for_update())
    if row is None:
        try:
            with db.begin_nested():
                row = InvoiceCounter(year=year, last_number=0)
                db.add(row)
                db.flush()
        except IntegrityError:
            pass
        row = db.scalar(select(InvoiceCounter).where(InvoiceCounter.year == year).with_for_update())
    row.last_number += 1
    db.flush()
    return f"INV-{year}-{row.last_number:06d}"


def _invoice_lines(plan: Plan, usage: int) -> list[tuple[str, Decimal, Decimal, Decimal]]:
    lines = []
    if plan.monthly_fee > 0:
        lines.append(
            (f"{plan.name} - monthly fee", Decimal(1), plan.monthly_fee, cents(plan.monthly_fee))
        )
    extra = max(0, usage - plan.included_emails)
    if extra and plan.email_overage_price > 0:
        lines.append((f"Email overage ({extra} above {plan.included_emails} included)",
                      Decimal(extra), plan.email_overage_price,
                      cents(Decimal(extra) * plan.email_overage_price)))  # fmt: skip
    return lines


def generate_invoice(
    db: Session, subscription_id: int, now: datetime | None = None
) -> Invoice | None:
    """Fatura e periudhës së mbyllur, ose None (s'ka ende afat / plan falas / s'ka profil)."""
    now = as_utc(now or datetime.now(UTC))
    sub = db.get(Subscription, subscription_id, with_for_update=True)
    if sub is None or sub.status != SubStatus.ACTIVE:
        return None
    start, end = period(sub)
    if end > now:
        return None
    prof = get_profile(db, sub.owner_ref)
    if prof is None:
        log.warning("no billing profile for %s; invoice postponed", sub.owner_ref)
        return None
    plan = db.get(Plan, sub.plan_id)
    lines = _invoice_lines(plan, email_usage(db, sub.owner_ref, start, end))
    inv = None
    if lines:
        subtotal = sum((ln[3] for ln in lines), Decimal(0))
        tax = cents(subtotal * prof.vat_rate)
        inv = Invoice(
            number=_next_number(db, now.year), owner_ref=sub.owner_ref, subscription_id=sub.id,
            period_start=start, period_end=end, currency=plan.currency, subtotal=subtotal,
            vat_rate=prof.vat_rate, tax=tax, total=subtotal + tax,
            bill_to=json.dumps({
                "legal_name": prof.legal_name, "address": prof.address, "country": prof.country,
                "tax_id": prof.tax_id, "email": prof.email,
            }),
            issued_at=now, due_at=now + timedelta(days=settings.invoice_due_days),
        )  # fmt: skip
        db.add(inv)
        db.flush()
        for desc, qty, unit, amount in lines:
            db.add(InvoiceLine(invoice_id=inv.id, description=desc, quantity=qty,
                               unit_price=unit, amount=amount))  # fmt: skip
        db.flush()
    sub.periods_billed += 1
    if sub.pending_plan_id:
        sub.plan_id, sub.pending_plan_id = sub.pending_plan_id, None
    if sub.cancel_at_period_end:
        sub.status = SubStatus.CANCELLED
    db.flush()
    if inv is not None:
        events.emit(db, inv.owner_ref, "invoice.issued", "invoice", inv.number,
                    {"invoice_id": inv.number, "total": str(inv.total), "currency": inv.currency,
                     "due_at": inv.due_at.isoformat()})  # fmt: skip
        if sub.auto_pay:
            try:
                with db.begin_nested():
                    pay_from_wallet(db, inv.owner_ref, inv.id, now)
            except (wallets.InsufficientFunds, NotFound):
                pass  # mbetet e hapur; klienti e paguan pas top-up ose online
    return inv


def run_billing(db: Session, now: datetime | None = None) -> int:
    """Worker: lëshon faturat e afatuara. Çdo abonim në transaksionin e vet."""
    now = as_utc(now or datetime.now(UTC))
    ids = list(db.scalars(select(Subscription.id).where(Subscription.status == SubStatus.ACTIVE)))
    db.rollback()
    issued = 0
    for sid in ids:
        for _ in range(12):  # rikuperim i periudhave të humbura, i kufizuar
            try:
                inv = generate_invoice(db, sid, now)
                db.commit()
            except Exception:
                db.rollback()
                log.exception("billing failed for subscription %s", sid)
                break
            if inv is None:
                sub = db.get(Subscription, sid)
                if sub is None or sub.status != SubStatus.ACTIVE or period(sub)[1] > now:
                    break
                continue  # periudhë pa faturë (plan falas): vazhdo te tjetra
            issued += 1
    return issued


# --- Pagesa dhe anulim --------------------------------------------------------------------


def _get_invoice(
    db: Session, owner_ref: str | None, invoice_id: int, lock: bool = False
) -> Invoice:
    q = select(Invoice).where(Invoice.id == invoice_id)
    if owner_ref is not None:
        q = q.where(Invoice.owner_ref == owner_ref)
    inv = db.scalar(q.with_for_update() if lock else q)
    if inv is None:
        raise NotFound("invoice not found")
    return inv


def _mark_paid(db: Session, inv: Invoice, via: str, now: datetime) -> None:
    inv.status, inv.paid_at, inv.paid_via = InvoiceStatus.PAID, now, via
    db.flush()
    events.emit(db, inv.owner_ref, "invoice.paid", "invoice", inv.number,
                {"invoice_id": inv.number, "total": str(inv.total), "via": via})  # fmt: skip


def pay_from_wallet(
    db: Session, owner_ref: str, invoice_id: int, now: datetime | None = None
) -> Invoice:
    now = as_utc(now or datetime.now(UTC))
    inv = _get_invoice(db, owner_ref, invoice_id, lock=True)
    if inv.status == InvoiceStatus.PAID:
        return inv
    if inv.status != InvoiceStatus.OPEN:
        raise Conflict(f"invoice is {inv.status.value}")
    w = db.scalar(
        select(Wallet).where(Wallet.owner_ref == owner_ref, Wallet.currency == inv.currency)
    )
    if w is None:
        raise NotFound(f"no {inv.currency} wallet")
    wallets.charge(db, w.id, inv.total, f"invoice:{inv.id}", "invoice", inv.number, inv.number)
    _mark_paid(db, inv, "wallet", now)
    return inv


def mark_paid_online(db: Session, inv: Invoice, now: datetime) -> None:
    _mark_paid(db, inv, "online", now)


def void_invoice(db: Session, invoice_id: int, reason: str) -> Invoice:
    if not reason or len(reason.strip()) < 3:
        raise InvalidBilling("a reason is required to void an invoice")
    inv = _get_invoice(db, None, invoice_id, lock=True)
    if inv.status != InvoiceStatus.OPEN:
        raise Conflict("only open invoices can be voided (paid invoices need a credit note)")
    inv.status, inv.voided_reason = InvoiceStatus.VOID, reason.strip()[:200]
    for p in db.scalars(
        select(Payment).where(Payment.invoice_id == inv.id, Payment.status == PaymentStatus.PENDING)
    ):
        p.status, p.failure_reason = PaymentStatus.EXPIRED, "invoice_voided"
    db.flush()
    return inv


def overdue(db: Session, now: datetime | None = None) -> list[Invoice]:
    now = as_utc(now or datetime.now(UTC))
    return list(
        db.scalars(
            select(Invoice)
            .where(Invoice.status == InvoiceStatus.OPEN, Invoice.due_at < now)
            .order_by(Invoice.due_at)
        )
    )


def invoice_lines(db: Session, invoice_id: int) -> list[InvoiceLine]:
    return list(
        db.scalars(
            select(InvoiceLine).where(InvoiceLine.invoice_id == invoice_id).order_by(InvoiceLine.id)
        )
    )
