"""M9-g4: krahasimi shadow Central ↔ legacy. VETËM projeksion dhe krahasim të përhershëm (`billing_shadow_comparisons`); asnjë korrigjim automatik.

Çfarë NUK bën kurrë: s'lëshon numër fature (sekuenca e pandryshuar), s'krijon `Invoice` autoritare, s'shënon `billing_period`, s'shlyen pagesë, s'përparon kursorin
`next_period_index`. Rendi i kategorive (kryesorja): period_mismatch › currency_mismatch › plan_mismatch › tax_mismatch › insufficient_usage › pricing_mismatch ›
usage_mismatch › amount_mismatch; përndryshe `exact`. `legacy_only` / `central_only` kur vetëm njëra anë do të faturonte periudhën. Kategoritë e shpjeguara (usage/pricing/
insufficient_usage/legacy_only) janë WARN në readiness; të pashpjeguarat (amount, central_only, plan, tax, period, currency) janë FAIL."""

import re
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.core.timeutil import utcnow
from apps.central.models.billing import (
    L_EMAIL_OVERAGE,
    L_MONTHLY_FEE,
    PROV_LEGACY,
    SUB_ACTIVE,
    BillingProfile,
    BillingSubscription,
    Invoice,
    InvoiceLine,
    PlanVersion,
)
from apps.central.models.billing_import import BillingImportItem, BillingShadowComparison
from apps.central.services import billing, billing_overage
from apps.central.services.billing_import import h

_OVERAGE = re.compile(r"^Email overage \((\d+) above (\d+) included\)$")
PRIORITY = (
    "period_mismatch",
    "currency_mismatch",
    "plan_mismatch",
    "tax_mismatch",
    "insufficient_usage",
    "pricing_mismatch",
    "usage_mismatch",
    "amount_mismatch",
)


def _s(x) -> str:
    return format(Decimal(x).quantize(Decimal("0.000001")), "f")


def project(db: Session, sub: BillingSubscription, k: int) -> dict:
    """Çfarë do të lëshonte Central për periudhën k (vetëm lexim)."""
    start, end = billing.period_bounds(sub, k)
    pv = db.get(PlanVersion, sub.plan_version_id)
    prof = db.scalar(
        select(BillingProfile).where(BillingProfile.enterprise_id == sub.enterprise_id)
    )
    ev = billing_overage.evaluate(db, sub, pv, start, end, k)
    lines = []
    fee = billing.cents(pv.monthly_fee)
    if fee > 0:
        lines.append(
            {
                "type": L_MONTHLY_FEE,
                "quantity": "1",
                "unit_price": _s(pv.monthly_fee),
                "amount": _s(fee),
            }
        )
    usage = {"state": "not_metered"}
    if ev.metered:
        usage = {
            "state": "waiting" if ev.wait else ("postponed" if ev.postpone else "ok"),
            "reason": ev.wait or ev.postpone,
        }
        if not ev.wait and not ev.postpone:
            usage |= {
                "delta": ev.delta,
                "included": ev.included,
                "extra": ev.extra,
                "unit_price": _s(ev.quote.unit_price),
            }
            amt = (
                billing_overage.overage_amount(ev.extra, ev.quote.unit_price)
                if ev.extra > 0
                else Decimal(0)
            )
            if amt > 0:
                lines.append(
                    {
                        "type": L_EMAIL_OVERAGE,
                        "quantity": str(ev.extra),
                        "unit_price": _s(ev.quote.unit_price),
                        "amount": _s(amt),
                    }
                )
    elif ev.postpone:
        usage = {"state": "postponed", "reason": ev.postpone}
    subtotal = sum((Decimal(ln["amount"]) for ln in lines), Decimal(0))
    vat = Decimal(prof.vat_rate) if prof else Decimal(0)
    tax = billing.cents(subtotal * vat)
    return {"period_index": k, "start": start.isoformat(), "end": end.isoformat(), "currency": pv.currency, "monthly_fee": _s(pv.monthly_fee), "included_emails": pv.included_emails,
            "lines": lines, "subtotal": _s(subtotal), "vat_rate": _s(vat), "tax": _s(tax), "total": _s(subtotal + tax), "decision": "invoice" if lines else "no_charge", "usage": usage}  # fmt: skip


def legacy_summary(db: Session, inv: Invoice | None) -> dict:
    if inv is None:
        return {"present": False}
    lines = list(
        db.scalars(
            select(InvoiceLine)
            .where(InvoiceLine.invoice_id == inv.id)
            .order_by(InvoiceLine.line_no)
        )
    )
    fee = sum((Decimal(ln.amount) for ln in lines if ln.line_type == L_MONTHLY_FEE), Decimal(0))
    ov = next((ln for ln in lines if ln.line_type == L_EMAIL_OVERAGE), None)
    included = None
    if ov is not None and (m := _OVERAGE.match(ov.description)):
        included = int(m.group(2))
    return {"present": True, "number": inv.number, "status": inv.status, "start": billing.utc(inv.period_start).isoformat(), "end": billing.utc(inv.period_end).isoformat(),
            "currency": inv.currency, "monthly_fee": _s(fee), "overage_quantity": None if ov is None else _s(ov.quantity), "overage_unit_price": None if ov is None else _s(ov.unit_price),
            "included_emails": included, "subtotal": _s(inv.subtotal), "vat_rate": _s(inv.vat_rate), "tax": _s(inv.tax), "total": _s(inv.total)}  # fmt: skip


def classify(c: dict, lg: dict) -> tuple[str, list[str]]:
    if not lg["present"]:
        return ("central_only", ["central_only"]) if c["decision"] == "invoice" else ("exact", [])
    if c["decision"] == "no_charge" and Decimal(lg["total"]) > 0:
        return "legacy_only", ["legacy_only"]
    cats: list[str] = []
    if (lg["start"], lg["end"]) != (c["start"], c["end"]):
        cats.append("period_mismatch")
    if lg["currency"] != c["currency"]:
        cats.append("currency_mismatch")
    if Decimal(lg["monthly_fee"]) != Decimal(c["monthly_fee"]) or (
        lg["included_emails"] is not None and lg["included_emails"] != c["included_emails"]
    ):
        cats.append("plan_mismatch")
    if Decimal(lg["vat_rate"]) != Decimal(c["vat_rate"]):
        cats.append("tax_mismatch")
    u = c["usage"]
    c_ov = next((ln for ln in c["lines"] if ln["type"] == L_EMAIL_OVERAGE), None)
    l_has = lg["overage_quantity"] is not None
    if u["state"] in ("waiting", "postponed"):
        cats.append("insufficient_usage")
    elif l_has and c_ov is not None:
        if Decimal(c_ov["unit_price"]) != Decimal(lg["overage_unit_price"]):
            cats.append("pricing_mismatch")
        if Decimal(c_ov["quantity"]) != Decimal(lg["overage_quantity"]):
            cats.append("usage_mismatch")
    elif l_has:  # legacy faturoi overage, Central jo
        cats.append("pricing_mismatch" if u["state"] == "not_metered" else "usage_mismatch")
    elif c_ov is not None:  # Central do të faturonte overage, legacy jo
        cats.append("usage_mismatch")
    if not cats and (
        Decimal(lg["total"]) != Decimal(c["total"])
        or Decimal(lg["subtotal"]) != Decimal(c["subtotal"])
    ):
        cats.append("amount_mismatch")
    if (
        Decimal(lg["subtotal"]) == Decimal(c["subtotal"])
        and Decimal(lg["tax"]) != Decimal(c["tax"])
        and "tax_mismatch" not in cats
    ):
        cats.append("tax_mismatch")
    if not cats:
        return "exact", []
    return next(p for p in PRIORITY if p in cats), cats


def compare_period(
    db: Session, sub: BillingSubscription, k: int, now: datetime
) -> BillingShadowComparison | None:
    c = project(db, sub, k)
    inv = db.scalar(
        select(Invoice).where(
            Invoice.subscription_id == sub.id,
            Invoice.period_index == k,
            Invoice.provenance == PROV_LEGACY,
        )
    )
    lg = legacy_summary(db, inv)
    cat, cats = classify(c, lg)
    hsh = h({"c": c, "l": lg, "cat": cat})
    last = db.scalar(select(BillingShadowComparison).where(BillingShadowComparison.subscription_id == sub.id, BillingShadowComparison.period_index == k)
                     .order_by(BillingShadowComparison.computed_at.desc(), BillingShadowComparison.id.desc()).limit(1))  # fmt: skip
    if last is not None and last.comparison_hash == hsh:
        return None
    row = BillingShadowComparison(subscription_id=sub.id, enterprise_id=sub.enterprise_id, period_index=k, legacy_invoice_id=None if inv is None else inv.id, category=cat,
                                  categories=cats, central=c, legacy=lg, comparison_hash=hsh, computed_at=now)  # fmt: skip
    db.add(row)
    db.flush()
    return row


def run(
    db: Session, now: datetime | None = None, *, recent: int = 3, subscription_id=None
) -> list[BillingShadowComparison]:
    """Krahason `recent` periudhat e fundit të përpunuara nga legacy (indekse < next_period_index) për abonimet aktive të importuara. Vetëm komparime të reja shkruhen."""
    now = billing.utc(now or utcnow())
    imported = {
        i.target_id
        for i in db.scalars(
            select(BillingImportItem).where(BillingImportItem.source_table == "subscriptions")
        )
    }
    q = select(BillingSubscription).where(BillingSubscription.status == SUB_ACTIVE)
    if subscription_id is not None:
        q = q.where(BillingSubscription.id == subscription_id)
    out = []
    for sub in db.scalars(q.order_by(BillingSubscription.created_at, BillingSubscription.id)):
        if sub.id not in imported:
            continue
        for k in range(max(0, sub.next_period_index - recent), sub.next_period_index):
            row = compare_period(db, sub, k, now)
            if row is not None:
                out.append(row)
    return out
