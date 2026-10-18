"""M9-g5: mbyllja e faturimit periodik — readiness final i agreguar, verifikimi i invarianteve, pamje operacionale dhe alarme. VETËM LEXIM.

Asnjë mutacion, asnjë thirrje rrjeti, asnjë PII (vetëm UUID/numra/shuma/numërues/mosha). Nuk shton funksion faturimi: bashkon
`billing_authority.readiness` (g4), `billing_readiness.checks` (g2/g3) dhe kontrolle të reja mbi invariantët, heartbeat-in e workerit dhe
çështjet manuale. Niveli: PASS | WARN | FAIL. Alarmet nxirren nga këto kontrolle (pa integrim të rremë me sisteme jashtë)."""

import re
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.timeutil import utcnow
from apps.central.models.audit import AuditLog
from apps.central.models.billing import (
    P_NO_CHARGE,
    PROV_LEGACY,
    BillingPeriod,
    Invoice,
    InvoiceNumberSequence,
)
from apps.central.models.billing_import import BillingImportBatch, BillingImportItem
from apps.central.models.money import PURPOSE_INVOICE, CommercialLedgerEntry, CreditGrant, Payment
from apps.central.models.settlement import CreditNote, CreditNoteSequence
from apps.central.services import (
    billing,
    billing_authority,
    billing_import,
    billing_readiness,
    settlement_reports,
)

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
SAMPLE = 5000  # sasia maksimale e faturave të verifikuara për ekzekutim (më të rejat); pjesa tjetër mbulohet nga kufizimet e DB
_NUM = re.compile(r"^(INV|CN)-(\d{4})-(\d{6})$")


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: str
    reason: str


def overall(items: list[Check]) -> str:
    return (
        FAIL
        if any(c.level == FAIL for c in items)
        else (WARN if any(c.level == WARN for c in items) else PASS)
    )


def _c(name: str, bad, ok_text: str, bad_text: str, level: str = FAIL) -> Check:
    return Check(name, level if bad else PASS, bad_text if bad else ok_text)


# --- invariantet ---------------------------------------------------------------------------------------------------------------------------------


def _max_by_year(numbers, prefix: str) -> dict[int, int]:
    out: dict[int, int] = {}
    for n in numbers:
        m = _NUM.match(n)
        if m and m.group(1) == prefix:
            y, k = int(m.group(2)), int(m.group(3))
            out[y] = max(out.get(y, 0), k)
    return out


def invariants(db: Session) -> list[Check]:
    out: list[Check] = []
    dup_periods = db.execute(
        select(BillingPeriod.subscription_id, BillingPeriod.period_index)
        .group_by(BillingPeriod.subscription_id, BillingPeriod.period_index)
        .having(func.count() > 1)
        .limit(50)
    ).all()
    out.append(_c("inv_one_period_per_subscription_index", dup_periods, "unique", f"{len(dup_periods)} duplicate (subscription, index)"))  # fmt: skip

    recent = db.scalars(
        select(Invoice).order_by(Invoice.issued_at.desc(), Invoice.id).limit(SAMPLE)
    ).all()
    bad = [str(i.id) for i in recent if billing.verify_invoice(db, i)]
    out.append(_c("inv_invoice_total_equals_lines_plus_tax", bad, f"{len(recent)} invoice(s) verified", f"{len(bad)} invoice(s) with arithmetic problems"))  # fmt: skip

    dup_inv = db.execute(
        select(Invoice.number).group_by(Invoice.number).having(func.count() > 1).limit(50)
    ).all()
    dup_cn = db.execute(
        select(CreditNote.number).group_by(CreditNote.number).having(func.count() > 1).limit(50)
    ).all()
    out.append(_c("inv_no_duplicate_document_numbers", dup_inv or dup_cn, "unique", f"duplicates: invoices={len(dup_inv)} credit_notes={len(dup_cn)}"))  # fmt: skip

    inv_max = _max_by_year(db.scalars(select(Invoice.number)), "INV")
    seq = {
        y: n
        for y, n in db.execute(
            select(InvoiceNumberSequence.year, InvoiceNumberSequence.last_number)
        )
    }
    behind = sorted(y for y, mx in inv_max.items() if seq.get(y, -1) < mx)
    out.append(_c("inv_invoice_sequence_not_behind", behind, "sequence >= every issued number", f"invoice sequence behind issued numbers for years {behind}"))  # fmt: skip
    cn_max = _max_by_year(db.scalars(select(CreditNote.number)), "CN")
    cn_seq = {
        y: n for y, n in db.execute(select(CreditNoteSequence.year, CreditNoteSequence.last_number))
    }
    cn_behind = sorted(y for y, mx in cn_max.items() if cn_seq.get(y, -1) < mx)
    out.append(_c("inv_credit_note_sequence_not_behind", cn_behind, "sequence >= every credit note number", f"credit-note sequence behind issued numbers for years {cn_behind}"))  # fmt: skip

    an = settlement_reports.anomalies(db)
    pay_bad = {k: v for k, v in an.items() if v and not k.startswith("credit_note")}
    cn_bad = {k: v for k, v in an.items() if v and k.startswith("credit_note")}
    out.append(_c("inv_settlement_one_allocation_exact_total", pay_bad, "paid invoices, payments and allocations are consistent", "; ".join(f"{k}={len(v)}" for k, v in sorted(pay_bad.items()))))  # fmt: skip
    out.append(_c("inv_credit_notes_cumulative_within_paid_total", cn_bad, "credit notes within paid invoice totals", "; ".join(f"{k}={len(v)}" for k, v in sorted(cn_bad.items()))))  # fmt: skip

    imported_inv = set(
        db.scalars(
            select(BillingImportItem.target_id).where(BillingImportItem.target_type == "invoice")
        )
    )
    orphans = [
        i
        for i in db.scalars(select(Invoice.id).where(Invoice.provenance == PROV_LEGACY))
        if i not in imported_inv
    ]
    imported_pay = set(
        db.scalars(
            select(BillingImportItem.target_id).where(BillingImportItem.target_type == "payment")
        )
    )
    pay_orphans = [
        p
        for p in db.scalars(select(Payment.id).where(Payment.source == "legacy_import"))
        if p not in imported_pay
    ]
    out.append(_c("inv_imported_rows_have_provenance_evidence", orphans or pay_orphans, "every imported invoice/payment has an import item", f"{len(orphans)} imported invoice(s) and {len(pay_orphans)} payment(s) without import evidence"))  # fmt: skip

    inv_pay = {
        str(p) for p in db.scalars(select(Payment.id).where(Payment.purpose == PURPOSE_INVOICE))
    }
    ledger = {
        s
        for s in db.scalars(
            select(CommercialLedgerEntry.source_id).where(
                CommercialLedgerEntry.source_type == "payment"
            )
        )
    }
    grants = set(
        db.scalars(
            select(CreditGrant.source_payment_id).where(CreditGrant.source_payment_id.is_not(None))
        )
    )
    coupled = (inv_pay & ledger) or {p for p in grants if str(p) in inv_pay}
    out.append(_c("inv_invoice_payments_never_touch_wallet_or_ledger", coupled, "no commercial ledger entry or grant from an invoice payment", f"{len(coupled)} invoice payment(s) coupled to the commercial ledger/grants"))  # fmt: skip

    st = billing_authority.mode(db)
    central_n = billing_authority.central_invoice_count(db)
    out.append(_c("inv_no_central_invoice_outside_central_mode", central_n and st != "central", "consistent", f"{central_n} authoritative Central invoice(s) while authority mode is '{st}' (dual issuer)"))  # fmt: skip
    return out


# --- heartbeat i workerit ------------------------------------------------------------------------------------------------------------------------


def last_run(db: Session) -> AuditLog | None:
    return db.scalar(
        select(AuditLog)
        .where(AuditLog.action == billing.RUN_AUDIT_ACTION)
        .order_by(AuditLog.created_at.desc())
        .limit(1)
    )


def _age(now: datetime, then: datetime | None) -> int | None:
    return None if then is None else max(0, int((now - billing.utc(then)).total_seconds()))


def run_checks(db: Session, now: datetime) -> list[Check]:
    mode = billing_authority.mode(db)
    st = billing_authority.get_state(db)
    out: list[Check] = []
    level = {"central": PASS, "shadow": WARN, "local": FAIL}[mode]
    out.append(Check("authority_mode", level, f"billing authority = {mode}" + ("" if mode == "central" else " (Central does not issue invoices yet)")))  # fmt: skip
    ack = bool(st and st.ack)
    out.append(Check("production_ack", PASS if ack else (FAIL if settings.env == "production" and mode == "central" else WARN), "ACK recorded" if ack else "no production ACK recorded"))  # fmt: skip
    hb = last_run(db)
    age = _age(now, hb.created_at) if hb else None
    if mode != "central":
        out.append(
            Check("billing_worker_heartbeat", PASS, "not required before the central cutover")
        )
    elif hb is None:
        out.append(Check("billing_worker_heartbeat", WARN, "no billing run recorded yet"))
    elif age > settings.billing_run_stale_seconds:
        out.append(Check("billing_worker_heartbeat", WARN, f"last billing run {age}s ago (stale after {settings.billing_run_stale_seconds}s)"))  # fmt: skip
    else:
        out.append(Check("billing_worker_heartbeat", PASS, f"last billing run {age}s ago"))
    if hb is not None and (hb.detail or {}).get("failed"):
        out.append(Check("billing_last_run_clean", WARN, f"last run had {hb.detail['failed']} failed subscription(s)"))  # fmt: skip
    w = billing_import.waivers(db)
    out.append(_c("import_waivers_documented", w, "none", f"{len(w)} import issue(s) closed by documented operator waiver (objects stay outside Central)", WARN))  # fmt: skip
    return out


_PRE_CUTOVER_CAPPED = ("billing_run_not_stalled", "billing_periods_not_stuck_waiting")


def final_readiness(
    db: Session, now: datetime | None = None, *, prod_ack: bool | None = None
) -> dict:
    now = billing.utc(now or utcnow())
    items = [
        *run_checks(db, now),
        *billing_authority.readiness(db, now, prod_ack=prod_ack),
        *billing_readiness.checks(db, now),
        *invariants(db),
    ]
    mode = billing_authority.mode(db)
    flat = []
    for c in items:
        if mode != "central" and c.name in _PRE_CUTOVER_CAPPED and c.level == FAIL:
            # para cutover-it periudhat e afatuara presin nga dizajni (Central s'lëshon ende): kufizohet në WARN, jo FAIL
            c = Check(c.name, WARN, "(pre-cutover) " + c.reason)
        flat.append(Check(c.name, c.level, c.reason))
    return {
        "status": overall(flat),
        "mode": billing_authority.mode(db),
        "generated_at": now.isoformat(),
        "checks": [asdict(c) for c in flat],
    }


# --- alarme --------------------------------------------------------------------------------------------------------------------------------------

# kod → (emrat e kontrolleve, niveli kur është aktiv autoriteti `central`). Jashtë `central` këto janë kapëse cutover-i, jo incidente.
_CRITICAL_IN_CENTRAL = {
    "dual_issuer_possible": ("no_dual_issuer", "inv_no_central_invoice_outside_central_mode"),
    "sequence_collision_risk": ("sequence_seeds_safe", "inv_invoice_sequence_not_behind", "inv_credit_note_sequence_not_behind", "inv_no_duplicate_document_numbers"),
    "central_authority_with_enterprise_not_frozen": ("enterprise_billing_frozen",),
    "missing_usage_baseline_in_central": ("usage_opening_baseline",),
    "missing_pricing_in_central": ("currency_pricing_mapping_valid", "legacy_overage_has_central_price"),
    "invoice_arithmetic_invariant_failure": ("billing_invoice_arithmetic", "inv_invoice_total_equals_lines_plus_tax"),
    "settlement_invariant_failure": ("billing_settlement_integrity", "inv_settlement_one_allocation_exact_total", "inv_credit_notes_cumulative_within_paid_total", "inv_invoice_payments_never_touch_wallet_or_ledger"),
    "import_conflict_unresolved_at_cutover": ("import_conflicts_resolved", "no_unresolved_partial_or_overpayment"),
}  # fmt: skip
_WARNINGS = {
    "stale_usage_report": ("billing_usage_reports_fresh", "latest_usage_report_healthy"),
    "shadow_mismatch": ("shadow_comparison_acceptable",),
    "old_due_period": ("billing_run_not_stalled", "billing_periods_not_stuck_waiting"),
    "old_pending_invoice_payment": ("billing_invoice_payments_not_stale",),
    "unresolved_manual_review_item": ("import_conflicts_resolved",),
    "stale_worker_heartbeat": ("billing_worker_heartbeat",),
    "import_waivers_present": ("import_waivers_documented",),
    "invoices_overdue": ("billing_invoices_not_overdue",),
}  # fmt: skip


def alerts(checks: list[dict], mode: str) -> list[dict]:
    """Alarme të veprueshme nga rezultati i `final_readiness`. CRITICAL vetëm kur autoriteti është `central` (para tij janë kapëse cutover-i)."""
    by = {c["name"]: c for c in checks}
    out: list[dict] = []
    for code, names in _CRITICAL_IN_CENTRAL.items():
        hit = [by[n] for n in names if n in by and by[n]["level"] == FAIL]
        if hit and (
            mode == "central"
            or code
            in (
                "dual_issuer_possible",
                "sequence_collision_risk",
                "invoice_arithmetic_invariant_failure",
                "settlement_invariant_failure",
            )
        ):
            out.append({"severity": "CRITICAL", "code": code, "checks": [h["name"] for h in hit], "message": hit[0]["reason"]})  # fmt: skip
    for code, names in _WARNINGS.items():
        hit = [by[n] for n in names if n in by and by[n]["level"] in (WARN, FAIL)]
        if hit and not (
            code == "unresolved_manual_review_item"
            and any(a["code"] == "import_conflict_unresolved_at_cutover" for a in out)
        ):
            out.append({"severity": "WARN", "code": code, "checks": [h["name"] for h in hit], "message": hit[0]["reason"]})  # fmt: skip
    return out


# --- pamja operacionale (pa PII) -----------------------------------------------------------------------------------------------------------------


def observability(db: Session, now: datetime | None = None) -> dict:
    now = billing.utc(now or utcnow())
    s = billing_readiness.summary(db, now)
    st = billing_authority.get_state(db)
    batch = db.scalar(
        select(BillingImportBatch)
        .order_by(BillingImportBatch.applied_at.desc(), BillingImportBatch.id)
        .limit(1)
    )
    issues = billing_import.unresolved_issues(db)
    by_cat: dict[str, int] = {}
    for i in issues:
        c = billing_import.categorize(i.reason)
        by_cat[c] = by_cat.get(c, 0) + 1
    latest = billing_authority.latest_shadow(db)
    mism: dict[str, int] = {}
    for r in latest.values():
        mism[r.category] = mism.get(r.category, 0) + 1
    last_cmp = max((r.computed_at for r in latest.values()), default=None)
    ages = [r["age_seconds"] for r in s["reports"] if r["age_seconds"] is not None]
    hb = last_run(db)
    ss = settlement_reports.summary(db, now, settings.payment_pending_stale_seconds)
    no_charge = (
        db.scalar(
            select(func.count())
            .select_from(BillingPeriod)
            .where(BillingPeriod.status == P_NO_CHARGE)
        )
        or 0
    )
    return {
        "generated_at": now.isoformat(),
        "authority": {"mode": billing_authority.mode(db), "ack": bool(st and st.ack), "changed_at": st.changed_at.isoformat() if st and st.changed_at else None,
                      "central_invoices": billing_authority.central_invoice_count(db),
                      "legacy_worker_frozen_attested": bool(batch and batch.attestation.get("mode") == "central")},
        "import": {"last_batch_export_id": str(batch.export_id) if batch else None, "last_batch_applied_at": batch.applied_at.isoformat() if batch else None,
                   "unresolved_issues": len(issues), "unresolved_by_category": dict(sorted(by_cat.items())), "documented_waivers": len(billing_import.waivers(db))},
        "shadow": {"last_comparison_at": last_cmp.isoformat() if last_cmp else None, "mismatch_counts": dict(sorted(mism.items()))},
        "usage": {"metered_subscriptions": s["metered_subscriptions"], "oldest_report_age_seconds": max(ages, default=None),
                  "newest_report_age_seconds": min(ages, default=None), "periods_waiting_for_usage": len(s["waiting"]),
                  "periods_postponed": len(s["postponed"])},
        "periods": {"due_unprocessed": len(s["due_unprocessed"]), "no_charge_total": int(no_charge)},
        "worker": {"last_run_at": hb.created_at.isoformat() if hb else None, "last_run_age_seconds": _age(now, hb.created_at) if hb else None,
                   "invoices_issued_last_run": (hb.detail or {}).get("invoiced") if hb else None, "last_run_failed": (hb.detail or {}).get("failed") if hb else None},
        "settlement": {"invoice_payments_pending": ss["invoice_payments"]["pending"], "invoice_payments_stale_pending": ss["invoice_payments"]["stale_pending"],
                       "invoices_overdue_open": ss["invoices"]["overdue_open"], "anomalies": {k: len(v) for k, v in ss["anomalies"].items() if v}},
    }  # fmt: skip
