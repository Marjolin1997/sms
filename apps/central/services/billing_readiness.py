"""M9-g2: gatishmëria e faturimit periodik me përdorim email. VETËM LEXIM (asnjë mutacion, asnjë thirrje rrjeti). Pa PII.

Kontrollet: (1) raporti i fundit për çdo abonim të matur (çmim email i caktuar) është i freskët; (2) periudhat e mbyllura që presin raportin
e prerjes nuk kanë kaluar pragjet; (3) asnjë periudhë e shtyrë për arsye konfigurimi (produkt i paqartë, çmim/monedhë, baseline);
(4) aritmetika e faturave të fundit; (5) abonime të afatuara dhe të papërpunuara (billing_run i ndalur)."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.timeutil import utcnow
from apps.central.models.billing import (
    L_EMAIL_OVERAGE,
    SUB_ACTIVE,
    BillingSubscription,
    Invoice,
    InvoiceLine,
)
from apps.central.models.billing_usage import BillingUsageReport
from apps.central.services import billing, billing_overage

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
INVOICE_SAMPLE = 500


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: str
    reason: str


def _age(now: datetime, then: datetime | None) -> int | None:
    return None if then is None else max(0, int((now - billing.utc(then)).total_seconds()))


def _metered_subscriptions(db: Session, now: datetime):
    """[(sub, product_id)] pér abonimet aktive me çmim email në fuqi tani."""
    from apps.central.services import pricing

    out = []
    for sub in db.scalars(
        select(BillingSubscription).where(BillingSubscription.status == SUB_ACTIVE)
    ):
        pid, ambiguous = billing_overage.email_product(db, sub.enterprise_id)
        if ambiguous:
            out.append((sub, None))
        elif pid is not None and pricing.assignment_at(db, sub.enterprise_id, pid, now) is not None:
            out.append((sub, pid))
    return out


def summary(db: Session, now: datetime | None = None) -> dict:
    """Pamje operacionale pa PII (vetëm UUID/numërues/mosha). `waiting`/`postponed` llogariten nga vlerësimi i periudhës së radhës."""
    now = billing.utc(now or utcnow())
    subs = _metered_subscriptions(db, now)
    reports, waiting, postponed, overdue = [], [], [], []
    for sub, pid in subs:
        if pid is not None:
            top = db.scalar(select(BillingUsageReport).where(BillingUsageReport.enterprise_id == sub.enterprise_id,
                            BillingUsageReport.product_id == pid).order_by(BillingUsageReport.report_seq.desc()).limit(1))  # fmt: skip
            reports.append({"enterprise_id": str(sub.enterprise_id), "age_seconds": _age(now, top.received_at) if top else None,
                            "watermark": int(top.watermark) if top else None,
                            "cumulative_billable_count": int(top.cumulative_billable_count) if top else None})  # fmt: skip
        start, end = billing.period_bounds(sub, sub.next_period_index)
        if end > now:
            continue
        pv = db.get(billing.PlanVersion, sub.plan_version_id)
        ev = billing_overage.evaluate(db, sub, pv, start, end)
        row = {
            "enterprise_id": str(sub.enterprise_id),
            "period_index": sub.next_period_index,
            "waiting_seconds": _age(now, end),
        }
        if ev.wait:
            waiting.append({**row, "reason": ev.wait})
        elif ev.postpone:
            postponed.append({**row, "reason": ev.postpone})
        else:
            overdue.append(row)
    sample = db.scalars(
        select(Invoice).order_by(Invoice.issued_at.desc()).limit(INVOICE_SAMPLE)
    ).all()
    bad = [str(i.id) for i in sample if billing.verify_invoice(db, i)]
    overage_total = db.scalar(
        select(func.coalesce(func.sum(InvoiceLine.quantity), 0)).where(
            InvoiceLine.line_type == L_EMAIL_OVERAGE
        )
    )
    return {"metered_subscriptions": len(subs), "reports": reports, "waiting": waiting, "postponed": postponed, "due_unprocessed": overdue,
            "invoices_checked": len(sample), "invoices_with_problems": bad, "overage_emails_billed_total": str(overage_total)}  # fmt: skip


def checks(db: Session, now: datetime | None = None) -> list[Check]:
    now = billing.utc(now or utcnow())
    s = summary(db, now)
    out: list[Check] = []
    if not s["metered_subscriptions"]:
        out.append(Check("billing_usage_reports_fresh", PASS, "no metered subscription"))
    else:
        worst, why = PASS, "all fresh"
        for r in s["reports"]:
            age = r["age_seconds"]
            lvl = (
                FAIL
                if age is None or age > settings.billing_usage_stale_seconds
                else (WARN if age > settings.billing_usage_fresh_seconds else PASS)
            )
            if lvl != PASS and (worst == PASS or lvl == FAIL):
                worst, why = (
                    lvl,
                    f"enterprise {r['enterprise_id']}: "
                    + ("no report" if age is None else f"latest report {age}s old"),
                )
        out.append(Check("billing_usage_reports_fresh", worst, why))
    wmax = max((w["waiting_seconds"] or 0 for w in s["waiting"]), default=0)
    wl = (
        FAIL
        if wmax > settings.billing_wait_fail_seconds
        else (WARN if wmax > settings.billing_wait_warn_seconds else PASS)
    )
    out.append(
        Check(
            "billing_periods_not_stuck_waiting",
            wl,
            f"{len(s['waiting'])} waiting, oldest {wmax}s" if s["waiting"] else "none waiting",
        )
    )
    reasons = sorted({p["reason"] for p in s["postponed"]})
    out.append(Check("billing_no_postponed_config", FAIL if s["postponed"] else PASS,
                     f"{len(s['postponed'])} postponed: {', '.join(reasons)}" if s["postponed"] else "none"))  # fmt: skip
    out.append(Check("billing_invoice_arithmetic", FAIL if s["invoices_with_problems"] else PASS,
                     f"{len(s['invoices_with_problems'])} invoice(s) inconsistent" if s["invoices_with_problems"] else f"{s['invoices_checked']} checked"))  # fmt: skip
    omax = max((o["waiting_seconds"] or 0 for o in s["due_unprocessed"]), default=0)
    ol = (
        FAIL
        if omax > settings.billing_wait_fail_seconds
        else (WARN if omax > settings.billing_wait_warn_seconds else PASS)
    )
    out.append(Check("billing_run_not_stalled", ol, f"{len(s['due_unprocessed'])} due period(s) not processed, oldest {omax}s" if s["due_unprocessed"] else "none"))  # fmt: skip
    return out


def overall(items: list[Check]) -> str:
    return (
        FAIL
        if any(c.level == FAIL for c in items)
        else (WARN if any(c.level == WARN for c in items) else PASS)
    )
