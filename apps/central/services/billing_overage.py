"""M9-g2: vlerësimi i përdorimit të email-it për një periudhë faturimi (vetëm lexim; pa rrjet, pa commit, kurrë vlerësim/estimim).

Hapat (fail-closed; çdo rezultat jo-i-plotë lë periudhën e papërpunuar dhe s'ndryshon asnjë gjendje):
 1. produkti email i enterprise-it (kanal `email`): asnjë ⇒ nuk matet; më shumë se një ⇒ `email_product_ambiguous`;
 2. caktimi i çmimit Central (M9-e) në `period_end`: asnjë ⇒ nuk matet (s'ka çmim overage ⇒ s'ka overage as kërkesë raporti);
    ka caktim por s'ka version/rregull efektiv ⇒ `email_price_unavailable`; monedhë ≠ monedha e planit ⇒ `currency_mismatch` (pa FX);
 3. raporti i prerjes = i pari (seq) me `generated_at >= period_end`: s'ka ⇒ PRIT (`usage_report_missing`);
 4. baseline = `usage_to` i periudhës së mëparshme (zinxhir) ose raporti më i fundit me `generated_at <= period_start`: s'ka ⇒ `usage_baseline_missing`;
 5. delta = cumulative_to − cumulative_from (≥ 0); extra = max(0, delta − included_emails i versionit të ngrirë të planit).
Çmimi vlerësohet në `period_end` dhe ngrihet në linjën e faturës (version/rregull)."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.models.billing import BillingPeriod, PlanVersion
from apps.central.models.billing_usage import BillingUsageReport
from apps.central.models.enterprise_product import EnterpriseProduct
from apps.central.models.product import Product
from apps.central.services import billing_usage, pricing

WAIT_REPORT = "usage_report_missing"
BASELINE_MISSING = "usage_baseline_missing"
PRODUCT_AMBIGUOUS = "email_product_ambiguous"
PRICE_UNAVAILABLE = "email_price_unavailable"
CURRENCY_MISMATCH = "currency_mismatch"
REGRESSION = "usage_regression"


@dataclass(slots=True)
class Evaluation:
    metered: bool = False
    wait: str | None = None  # PRIT raportin (zgjidhet vetë me kohën)
    postpone: str | None = None  # kërkon ndërhyrje (konfigurim/të dhëna)
    product_id: object = None
    base: BillingUsageReport | None = None
    cut: BillingUsageReport | None = None
    delta: int = 0
    included: int = 0
    extra: int = 0
    quote: pricing.PriceQuote | None = None


def email_product(db: Session, enterprise_id) -> tuple[object | None, bool]:
    """(product_id, ambiguous). Një enterprise pa produkt email ⇒ (None, False)."""
    ids = list(
        db.scalars(
            select(EnterpriseProduct.product_id)
            .join(Product, Product.id == EnterpriseProduct.product_id)
            .where(EnterpriseProduct.enterprise_id == enterprise_id, Product.channel == "email")
        )
    )
    if len(ids) > 1:
        return None, True
    return (ids[0] if ids else None), False


def evaluate(db: Session, sub, pv: PlanVersion, start: datetime, end: datetime) -> Evaluation:
    ev = Evaluation(included=int(pv.included_emails))
    product_id, ambiguous = email_product(db, sub.enterprise_id)
    if ambiguous:
        ev.postpone = PRODUCT_AMBIGUOUS
        return ev
    if product_id is None:
        return ev
    assignment = pricing.assignment_at(db, sub.enterprise_id, product_id, end)
    if assignment is None:
        return ev
    ev.product_id = product_id
    try:
        ev.quote = pricing.lookup(db, assignment.price_book_id, "email", "", end)
    except pricing.NoPrice:
        ev.postpone = PRICE_UNAVAILABLE
        return ev
    if ev.quote.currency != pv.currency:
        ev.postpone = CURRENCY_MISMATCH
        return ev
    ev.metered = True
    ev.cut = billing_usage.cutoff_report(db, sub.enterprise_id, product_id, end)
    if ev.cut is None:
        ev.wait = WAIT_REPORT
        return ev
    ev.base = _baseline(db, sub, product_id, start)
    if ev.base is None:
        ev.postpone = BASELINE_MISSING
        return ev
    ev.delta = int(ev.cut.cumulative_billable_count) - int(ev.base.cumulative_billable_count)
    if ev.delta < 0:
        ev.postpone = REGRESSION
        return ev
    ev.extra = max(0, ev.delta - ev.included)
    return ev


def _baseline(db: Session, sub, product_id, start: datetime) -> BillingUsageReport | None:
    prev = db.scalar(
        select(BillingPeriod).where(
            BillingPeriod.subscription_id == sub.id,
            BillingPeriod.period_index == sub.next_period_index - 1,
        )
    )
    if prev is not None and prev.usage_to_report_id is not None:
        rep = db.get(BillingUsageReport, prev.usage_to_report_id)
        if rep is not None and rep.product_id == product_id:
            return rep
    return billing_usage.baseline_before(db, sub.enterprise_id, product_id, start)


def overage_amount(extra: int, unit_price: Decimal) -> Decimal:
    from apps.central.services.billing import cents

    return cents(Decimal(extra) * unit_price)
