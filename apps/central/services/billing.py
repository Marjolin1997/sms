"""M9-g1: faturimi periodik në Central — profile, abonime, përpunimi i periudhës, numërimi, void. Pa HTTP, pa commit.

Politika (e miratuar): faturim në ARREARS, PA proporcion. Periudha k = [add_months(anchor, k − base), add_months(anchor, k − base + 1))
në UTC; faturohet vetëm kur `period_end <= now`. Aktivizimi në mes të muajit nis një periudhë të plotë në çastin e aktivizimit;
ndryshimi i planit hyn nga periudha e radhës; anulimi vlen në fund të periudhës aktuale (që faturohet ende).

`process_period` është i vetmi shkrues i periudhave/faturave të automatizuara dhe bën TË GJITHA në një transaksion: kyç abonimin →
verifikon që periudha është e afatuar dhe s'është përpunuar → (nëse ka tarifë) numër i kyçur + snapshot issuer/bill-to/VAT/plan +
linja + totale të derivuara + faturë → rresht `billing_periods` (invoiced | no_charge) → `next_period_index++` (+ plani në pritje,
+ anulimi) → audit `system:billing`. Crash ⇒ asgjë. Dërgimi i email/PDF (jashtë këtij modulit) vjen pas commit-it."""

import calendar
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.errors import Conflict, Invalid, NotFound
from apps.central.core.timeutil import utcnow
from apps.central.models.billing import (
    INV_OPEN,
    INV_VOID,
    L_EMAIL_OVERAGE,
    L_MONTHLY_FEE,
    P_INVOICED,
    P_NO_CHARGE,
    SUB_ACTIVE,
    SUB_CANCELLED,
    V_ACTIVE,
    BillingPeriod,
    BillingProfile,
    BillingSubscription,
    Invoice,
    InvoiceLine,
    InvoiceNumberSequence,
    PlanVersion,
)
from apps.central.models.enterprise import Enterprise
from apps.central.services import audit, billing_overage, billing_plans, money_common

log = logging.getLogger("central.billing")
CENT = Decimal("0.01")
SYSTEM = "system:billing"
DEFAULT_ISSUER_NAME = "Your Company Ltd"
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")
MAX_CATCH_UP = 12


def cents(x) -> Decimal:
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def add_months(dt: datetime, n: int) -> datetime:
    """Shton n muaj kalendarikë; dita kufizohet në ditën e fundit të muajit (31 jan + 1 = 28/29 shk), pa drift (llogaritet nga ankora)."""
    total = dt.year * 12 + (dt.month - 1) + n
    y, m = divmod(total, 12)
    return dt.replace(year=y, month=m + 1, day=min(dt.day, calendar.monthrange(y, m + 1)[1]))


def period_bounds(sub: BillingSubscription, k: int) -> tuple[datetime, datetime]:
    if k < sub.anchor_period_index:
        raise Invalid("period index precedes the subscription anchor")
    anchor = utc(sub.anchor_started_at)
    n = k - sub.anchor_period_index
    return add_months(anchor, n), add_months(anchor, n + 1)


# --- profili ------------------------------------------------------------------------------------------------


def vat(value) -> Decimal:
    if (
        isinstance(value, bool)
        or isinstance(value, float)
        or not isinstance(value, Decimal | str | int)
    ):
        raise Invalid("vat_rate must be a Decimal, string or integer (never float)")
    if isinstance(value, str) and not re.fullmatch(r"\d(\.\d{1,4})?", value):
        raise Invalid("vat_rate must be a plain decimal (no exponent, sign or spaces)")
    try:
        d = Decimal(value)
    except Exception:  # noqa: BLE001
        raise Invalid("invalid vat_rate") from None
    q = Decimal("0.0001")
    if not d.is_finite() or d != d.quantize(q) or not Decimal(0) <= d <= Decimal(1):
        raise Invalid("vat_rate must be between 0 and 1 with at most 4 decimal places")
    return d.quantize(q)


def get_profile(db: Session, enterprise_id) -> BillingProfile | None:
    return db.scalar(
        select(BillingProfile).where(
            BillingProfile.enterprise_id == money_common.uid(enterprise_id, "enterprise id")
        )
    )


def set_profile(db: Session, actor, enterprise_id, legal_name, address, country, email, tax_id=None, vat_rate=None,
                *, now: datetime | None = None) -> BillingProfile:  # fmt: skip
    """Krijon/përditëson profilin e faturimit të një enterprise. `vat_rate` vendoset vetëm nga stafi (admin). Pa ndryshim ⇒ pa audit."""
    actor = money_common.admin(actor)
    eid = money_common.uid(enterprise_id, "enterprise id")
    if db.get(Enterprise, eid) is None:
        raise NotFound("enterprise not found")
    name = money_common.optional_text(legal_name, "legal_name")
    addr = money_common.optional_text(address, "address")
    if not name or len(name) > 120 or not addr or len(addr) > 300:
        raise Invalid("legal_name (<= 120) and address (<= 300) are required")
    if not isinstance(country, str) or not re.fullmatch(r"[A-Za-z]{2}", country):
        raise Invalid("country must be ISO alpha-2")
    if not isinstance(email, str) or len(email) > 254 or not _EMAIL.match(email.strip()):
        raise Invalid("a valid billing email is required")
    tax = money_common.optional_text(tax_id, "tax_id")
    if tax is not None and len(tax) > 40:
        raise Invalid("tax_id is too long")
    now = now or utcnow()
    prof = get_profile(db, eid)
    new = {
        "legal_name": name,
        "address": addr,
        "country": country.upper(),
        "email": email.strip(),
        "tax_id": tax,
    }
    if prof is None:
        prof = BillingProfile(
            enterprise_id=eid,
            vat_rate=vat(vat_rate if vat_rate is not None else 0),
            created_at=now,
            updated_at=now,
            **new,
        )
        db.add(prof)
        db.flush()
        audit.record(db, actor, "billing_profile.create", "billing_profile", prof.id,
                     {"enterprise_id": str(eid), "vat_rate": str(prof.vat_rate)}, now=now)  # fmt: skip
        return prof
    changed = [k for k, v in new.items() if getattr(prof, k) != v]
    detail: dict = {}
    if vat_rate is not None and (rate := vat(vat_rate)) != prof.vat_rate:
        detail["vat_rate"] = {"before": str(prof.vat_rate), "after": str(rate)}
        prof.vat_rate = rate
    if changed or detail:
        for k in changed:
            setattr(prof, k, new[k])
        prof.updated_at = now
        db.flush()
        audit.record(db, actor, "billing_profile.update", "billing_profile", prof.id,
                     {"enterprise_id": str(eid), "fields": changed, **detail}, now=now)  # fmt: skip
    return prof


# --- abonimi ---------------------------------------------------------------------------------------------------


def get_subscription(
    db: Session, enterprise_id, *, lock: bool = False
) -> BillingSubscription | None:
    q = select(BillingSubscription).where(
        BillingSubscription.enterprise_id == money_common.uid(enterprise_id, "enterprise id")
    )
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    return db.scalar(q)


def _sub_snapshot(s: BillingSubscription) -> dict:
    return {"plan_version_id": str(s.plan_version_id),
            "pending_plan_version_id": None if s.pending_plan_version_id is None else str(s.pending_plan_version_id),
            "status": s.status, "cancel_at_period_end": s.cancel_at_period_end,
            "next_period_index": s.next_period_index}  # fmt: skip


def assign_plan(
    db: Session, actor, enterprise_id, plan_version_id, *, now: datetime | None = None
) -> BillingSubscription:
    """Abonim i ri; ndërrim plani (hyn nga periudha e radhës, e njëjta monedhë); ose RIFILLIM pas anulimit (ankorë e re, indeksi vazhdon).
    Kërkon profil faturimi dhe version plani `active`. Pa ndryshim real ⇒ pa audit."""
    actor = money_common.admin(actor)
    eid = money_common.uid(enterprise_id, "enterprise id")
    now = utc(now or utcnow())
    if db.get(Enterprise, eid) is None:
        raise NotFound("enterprise not found")
    pv = billing_plans.get_version(db, plan_version_id)
    if pv.status != V_ACTIVE:
        raise Conflict("only an active plan version can be assigned")
    if get_profile(db, eid) is None:
        raise Conflict("a billing profile is required before subscribing")
    sub = get_subscription(db, eid, lock=True)
    if sub is None:
        try:
            with db.begin_nested():
                sub = BillingSubscription(enterprise_id=eid, plan_version_id=pv.id, status=SUB_ACTIVE, cancel_at_period_end=False,
                                          anchor_started_at=now, anchor_period_index=0, next_period_index=0, created_at=now, updated_at=now)  # fmt: skip
                db.add(sub)
                db.flush()
        except IntegrityError:  # gara: një tjetër e krijoi
            db.expire_all()
            sub = get_subscription(db, eid, lock=True)
            if sub is None:
                raise
            return assign_plan(db, actor, eid, pv.id, now=now)
        audit.record(db, actor, "billing_subscription.assign", "billing_subscription", sub.id,
                     {"enterprise_id": str(eid), "after": _sub_snapshot(sub), "anchor_started_at": now.isoformat()}, now=now)  # fmt: skip
        return sub
    before = _sub_snapshot(sub)
    if sub.status == SUB_CANCELLED:  # rifillim: ankorë e re, numërimi i periudhave vazhdon
        cur = billing_plans.get_version(db, sub.plan_version_id)
        if cur.currency != pv.currency:
            raise Conflict("cannot reactivate onto a plan in another currency (no FX)")
        sub.plan_version_id, sub.pending_plan_version_id, sub.status = pv.id, None, SUB_ACTIVE
        sub.cancel_at_period_end, sub.cancelled_at = False, None
        sub.anchor_started_at, sub.anchor_period_index, sub.updated_at = (
            now,
            sub.next_period_index,
            now,
        )
    else:
        cur = billing_plans.get_version(db, sub.plan_version_id)
        if cur.currency != pv.currency:
            raise Conflict("cannot switch to a plan in another currency (no FX)")
        pending = None if pv.id == sub.plan_version_id else pv.id
        if pending == sub.pending_plan_version_id and not sub.cancel_at_period_end:
            return sub  # no-op
        sub.pending_plan_version_id, sub.cancel_at_period_end, sub.updated_at = pending, False, now
    db.flush()
    audit.record(db, actor, "billing_subscription.assign", "billing_subscription", sub.id,
                 {"enterprise_id": str(eid), "before": before, "after": _sub_snapshot(sub)}, now=now)  # fmt: skip
    return sub


def schedule_cancel(
    db: Session, actor, enterprise_id, *, now: datetime | None = None
) -> BillingSubscription:
    """Anulim në fund të periudhës aktuale (periudha aktuale faturohet ende). Tashmë i planifikuar ⇒ no-op."""
    actor = money_common.admin(actor)
    now = utc(now or utcnow())
    sub = get_subscription(db, enterprise_id, lock=True)
    if sub is None or sub.status != SUB_ACTIVE:
        raise NotFound("no active subscription")
    if sub.cancel_at_period_end:
        return sub
    sub.cancel_at_period_end, sub.updated_at = True, now
    db.flush()
    audit.record(db, actor, "billing_subscription.schedule_cancel", "billing_subscription", sub.id,
                 {"enterprise_id": str(sub.enterprise_id), "next_period_index": sub.next_period_index}, now=now)  # fmt: skip
    return sub


def unschedule_cancel(
    db: Session, actor, enterprise_id, *, now: datetime | None = None
) -> BillingSubscription:
    actor = money_common.admin(actor)
    now = utc(now or utcnow())
    sub = get_subscription(db, enterprise_id, lock=True)
    if sub is None or sub.status != SUB_ACTIVE:
        raise NotFound("no active subscription")
    if not sub.cancel_at_period_end:
        return sub
    sub.cancel_at_period_end, sub.updated_at = False, now
    db.flush()
    audit.record(db, actor, "billing_subscription.unschedule_cancel", "billing_subscription", sub.id,
                 {"enterprise_id": str(sub.enterprise_id)}, now=now)  # fmt: skip
    return sub


# --- numërimi ---------------------------------------------------------------------------------------------------


def next_number(db: Session, year: int) -> str:
    """`INV-{year}-{n:06d}`: rreshti i vitit kyçet `FOR UPDATE` deri në commit (pa `max()+1`); rollback e kthen numrin (pa boshllëk)."""
    q = select(InvoiceNumberSequence).where(InvoiceNumberSequence.year == year).with_for_update()
    row = db.scalar(q)
    if row is None:
        try:
            with db.begin_nested():
                db.add(InvoiceNumberSequence(year=year, last_number=0))
                db.flush()
        except IntegrityError:
            pass  # një tjetër e krijoi: e kyçim më poshtë
        row = db.scalar(q.execution_options(populate_existing=True))
    row.last_number += 1
    db.flush()
    return f"INV-{year}-{row.last_number:06d}"


# --- përpunimi i periudhës ------------------------------------------------------------------------------------------


@dataclass(slots=True)
class PeriodResult:
    kind: str  # invoiced | no_charge | not_due | inactive | postponed | waiting_usage
    period: BillingPeriod | None = None
    invoice: Invoice | None = None
    reason: str | None = None
    extra: dict = field(default_factory=dict)


def issuer_snapshot() -> dict:
    return {
        "name": settings.issuer_name,
        "address": settings.issuer_address,
        "tax_id": settings.issuer_tax_id or None,
    }


def process_period(db: Session, subscription_id, now: datetime | None = None) -> PeriodResult:
    """Përpunon periudhën e radhës të një abonimi (një periudhë per thirrje), ATOMIKISHT në transaksionin e thirrësit (që bën commit)."""
    now = utc(now or utcnow())
    sid = money_common.uid(subscription_id, "subscription id")
    sub = db.scalar(
        select(BillingSubscription)
        .where(BillingSubscription.id == sid)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if sub is None:
        raise NotFound("subscription not found")
    if sub.status != SUB_ACTIVE:
        return PeriodResult("inactive")
    k = sub.next_period_index
    start, end = period_bounds(sub, k)
    if end > now:
        return PeriodResult("not_due", reason="period has not ended")
    if db.scalar(
        select(BillingPeriod.id).where(
            BillingPeriod.subscription_id == sub.id, BillingPeriod.period_index == k
        )
    ):
        raise Conflict("billing period already processed but next_period_index did not advance")
    prof = get_profile(db, sub.enterprise_id)
    if prof is None:
        log.warning(
            "no billing profile for enterprise %s; period %s postponed", sub.enterprise_id, k
        )
        return PeriodResult("postponed", reason="billing_profile_missing")
    pv = db.get(PlanVersion, sub.plan_version_id)
    fee_amount = cents(pv.monthly_fee)
    specs = []
    if fee_amount > 0:
        plan = billing_plans.get_plan(db, pv.plan_id)
        specs.append({"line_type": L_MONTHLY_FEE, "description": f"{plan.name} - monthly fee", "quantity": Decimal(1),
                      "unit_price": pv.monthly_fee, "amount": fee_amount, "plan_version_id": pv.id})  # fmt: skip
    ev = billing_overage.evaluate(db, sub, pv, start, end)  # M9-g2: vetëm lexim DB; kurrë rrjet/estimim
    if ev.wait:
        return PeriodResult("waiting_usage", reason=ev.wait)
    if ev.postpone:
        log.warning("email usage for enterprise %s period %s postponed: %s", sub.enterprise_id, k, ev.postpone)
        return PeriodResult("postponed", reason=ev.postpone)
    if ev.extra > 0:
        amount = billing_overage.overage_amount(ev.extra, ev.quote.unit_price)
        if amount > 0:  # sasi nën-cent (p.sh. 3 × 0.000001) rrumbullakoset në 0.00: s'krijohet linjë me vlerë zero
            specs.append({"line_type": L_EMAIL_OVERAGE, "quantity": Decimal(ev.extra), "unit_price": ev.quote.unit_price,
                          "amount": amount, "plan_version_id": pv.id, "pricing_source": "central", "price_book_id": ev.quote.book_id,
                          "price_version_id": ev.quote.version_id, "price_rule_id": ev.quote.rule_id,
                          "description": f"Email overage - {ev.extra} above {ev.included} included"})  # fmt: skip
    invoice = None
    issued = []
    if specs:
        if settings.env == "production" and settings.issuer_name == DEFAULT_ISSUER_NAME:
            log.error(
                "CENTRAL_ISSUER_NAME is not configured; invoice for %s postponed", sub.enterprise_id
            )
            return PeriodResult("postponed", reason="issuer_not_configured")
        subtotal = sum((s["amount"] for s in specs), Decimal(0))
        tax = cents(subtotal * prof.vat_rate)
        invoice = Invoice(
            number=next_number(db, now.year), enterprise_id=sub.enterprise_id, subscription_id=sub.id, period_index=k,
            period_start=start, period_end=end, plan_version_id=pv.id, currency=pv.currency, subtotal=subtotal,
            vat_rate=prof.vat_rate, tax=tax, total=subtotal + tax, status=INV_OPEN,
            bill_to={"legal_name": prof.legal_name, "address": prof.address, "country": prof.country,
                     "tax_id": prof.tax_id, "email": prof.email},
            issuer=issuer_snapshot(), issued_at=now, due_at=now + timedelta(days=settings.invoice_due_days), created_at=now,
        )  # fmt: skip
        db.add(invoice)
        db.flush()
        for i, s in enumerate(specs, 1):
            db.add(
                InvoiceLine(
                    invoice_id=invoice.id,
                    currency=pv.currency,
                    line_no=i,
                    period_start=start,
                    period_end=end,
                    **s,
                )
            )
        db.flush()
        issued.append(invoice)
    period = BillingPeriod(
        subscription_id=sub.id, enterprise_id=sub.enterprise_id, period_index=k, period_start=start, period_end=end,
        plan_version_id=pv.id, status=P_INVOICED if invoice else P_NO_CHARGE, invoice_id=invoice.id if invoice else None,
        billed_at=now, created_at=now,
    )  # fmt: skip
    if ev.metered:
        period.usage_from, period.usage_to = int(ev.base.cumulative_billable_count), int(ev.cut.cumulative_billable_count)
        period.usage_from_report_id, period.usage_to_report_id = ev.base.report_id, ev.cut.report_id
    db.add(period)
    db.flush()
    sub.next_period_index = k + 1
    sub.updated_at = now
    if sub.pending_plan_version_id is not None:
        sub.plan_version_id, sub.pending_plan_version_id = sub.pending_plan_version_id, None
    if sub.cancel_at_period_end:
        sub.status, sub.cancelled_at = SUB_CANCELLED, now
    db.flush()
    audit.record_system(db, label=SYSTEM, action="billing.period_processed", resource_type="billing_period", resource_id=period.id,
                        detail={"enterprise_id": str(sub.enterprise_id), "subscription_id": str(sub.id), "period_index": k,
                                "status": period.status, "invoice": invoice.number if invoice else None,
                                **({"usage": {"from": period.usage_from, "to": period.usage_to, "included": ev.included, "extra": ev.extra,
                                              "from_report": str(ev.base.report_id), "to_report": str(ev.cut.report_id)}} if ev.metered else {})}, now=now)  # fmt: skip
    if invoice is not None:
        audit.record_system(db, label=SYSTEM, action="invoice.issued", resource_type="invoice", resource_id=invoice.id,
                            detail={"number": invoice.number, "total": str(invoice.total), "currency": invoice.currency,
                                    "period_index": k}, now=now)  # fmt: skip
    return PeriodResult(P_INVOICED if invoice else P_NO_CHARGE, period, invoice)


@dataclass(slots=True)
class RunSummary:
    invoiced: int = 0
    no_charge: int = 0
    postponed: int = 0
    errors: int = 0
    postponed_reasons: dict = field(default_factory=dict)
    due: int = 0  # M9-g2: abonime me të paktën një periudhë të afatuar (e përpunuar, e pritur ose e shtyrë)
    waiting_usage: int = 0  # periudha që presin raportin e përdorimit të email-it (kurrë estimim)
    waiting_reasons: dict = field(default_factory=dict)

    @property
    def failed(self) -> int:
        return self.errors

    def as_dict(self) -> dict:
        return {"due": self.due, "invoiced": self.invoiced, "no_charge": self.no_charge, "waiting_usage": self.waiting_usage,
                "postponed": self.postponed, "failed": self.failed, "postponed_reasons": dict(self.postponed_reasons),
                "waiting_reasons": dict(self.waiting_reasons)}  # fmt: skip


def run_due(engine, now: datetime | None = None, *, max_periods: int = MAX_CATCH_UP, limit: int | None = None,
            subscription_id=None) -> RunSummary:  # fmt: skip
    """Punonjësi (jo HTTP): çdo periudhë në transaksionin e vet; rikuperim i kufizuar i periudhave të humbura; periudhë e shtyrë ⇒ ndalon
    (pa iteracione të kota). Idempotent: rinisja s'krijon periudhë/faturë të dytë."""
    now = utc(now or utcnow())
    out = RunSummary()
    with Session(engine) as db:
        q = select(BillingSubscription.id).where(BillingSubscription.status == SUB_ACTIVE)
        if subscription_id is not None:
            q = q.where(BillingSubscription.id == money_common.uid(subscription_id, "subscription id"))
        q = q.order_by(BillingSubscription.created_at, BillingSubscription.id)
        if limit is not None:
            q = q.limit(max(1, int(limit)))
        ids = list(db.scalars(q))
    for sid in ids:
        counted = False
        for _ in range(max_periods):
            with Session(engine, expire_on_commit=False) as db:
                try:
                    r = process_period(db, sid, now)
                    db.commit()
                except Exception:  # noqa: BLE001
                    db.rollback()
                    log.exception("billing failed for subscription %s", sid)
                    out.errors += 1
                    break
            if r.kind not in ("not_due", "inactive") and not counted:
                counted = True
                out.due += 1
            if r.kind == P_INVOICED:
                out.invoiced += 1
            elif r.kind == P_NO_CHARGE:
                out.no_charge += 1
            else:
                if r.kind == "waiting_usage":
                    out.waiting_usage += 1
                    out.waiting_reasons[r.reason] = out.waiting_reasons.get(r.reason, 0) + 1
                if r.kind == "postponed":
                    out.postponed += 1
                    out.postponed_reasons[r.reason] = out.postponed_reasons.get(r.reason, 0) + 1
                break
    return out


# --- faturat ---------------------------------------------------------------------------------------------------------


def get_invoice(db: Session, invoice_id, *, lock: bool = False) -> Invoice:
    q = select(Invoice).where(Invoice.id == money_common.uid(invoice_id, "invoice id"))
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(q)
    if row is None:
        raise NotFound("invoice not found")
    return row


def lines_of(db: Session, invoice_id) -> list[InvoiceLine]:
    return list(
        db.scalars(
            select(InvoiceLine)
            .where(InvoiceLine.invoice_id == invoice_id)
            .order_by(InvoiceLine.line_no)
        )
    )


def verify_invoice(db: Session, inv: Invoice) -> list[str]:
    """Kontroll vetëm-lexim i aritmetikës (përdoret nga readiness-i i mëvonshëm): [] kur gjithçka përputhet."""
    problems = []
    ls = lines_of(db, inv.id)
    if not ls:
        problems.append("no lines")
    if sum((ln.amount for ln in ls), Decimal(0)) != inv.subtotal:
        problems.append("subtotal != sum(lines)")
    if any(ln.amount != cents(ln.quantity * ln.unit_price) for ln in ls):
        problems.append("line amount != cents(quantity*unit_price)")
    if inv.tax != cents(inv.subtotal * inv.vat_rate) or inv.total != inv.subtotal + inv.tax:
        problems.append("tax/total do not follow from subtotal and vat_rate")
    return problems


def void_invoice(db: Session, actor, invoice_id, reason, *, now: datetime | None = None) -> Invoice:
    """Vetëm faturë OPEN; arsye e detyrueshme; terminale. Tashmë void ⇒ no-op (pa audit). Faturë e paguar ⇒ Conflict (credit note, g3)."""
    actor = money_common.admin(actor)
    why = money_common.reason(reason)
    if len(why) < 3:
        raise Invalid("a reason of at least 3 characters is required")
    inv = get_invoice(db, invoice_id, lock=True)
    if inv.status == INV_VOID:
        return inv
    if inv.status != INV_OPEN:
        raise Conflict(
            f"invoice is {inv.status}: only open invoices can be voided (paid invoices need a credit note)"
        )
    now = utc(now or utcnow())
    inv.status, inv.voided_at, inv.voided_by_id, inv.voided_reason = INV_VOID, now, actor.id, why
    db.flush()
    audit.record(
        db,
        actor,
        "invoice.void",
        "invoice",
        inv.id,
        {"number": inv.number, "reason": why, "total": str(inv.total)},
        now=now,
    )
    return inv
