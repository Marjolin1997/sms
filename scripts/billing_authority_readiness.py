"""M9-g4: gatishmëria Enterprise për SMS_BILLING_AUTHORITY≠local (VETËM LEXIM; nuk ndryshon konfigurimin as DB-në).

    python -m scripts.billing_authority_readiness [--json] [--target shadow|central]

Kontrollon anën Enterprise: modaliteti/ACK, raportimi i përdorimit të email-it, periudha të afatuara të pafaturuara (nuk duhet të ketë para cutover), pagesa të sukseshme fature
që s'përputhen me totalin (pjesëtim/mbipagesë: kërkon rakordim manual), seanca online pending mbi fatura të hapura (do të refuzohen pas cutover), kapja e provave të përdorimit.
Dalja: `PASS|WARN|FAIL emri: arsyeja`. Kodi: 0 nëse s'ka FAIL · 1 nëse ka · 2 gabim i brendshëm. Pjesa Central: `apps.central.tools.billing_authority_readiness`."""

import argparse
import json
import sys
from dataclasses import asdict, dataclass

from sqlalchemy import func, select

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.timeutil import utcnow
from app.models.billing import (
    Invoice,
    InvoiceStatus,
    Payment,
    PaymentPurpose,
    PaymentStatus,
    Subscription,
    SubStatus,
)
from app.models.billing_usage import BillingUsageReport, EmailBillableEvent
from app.services import billing

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: str
    reason: str


def checks(db, target: str = "central") -> list[Check]:
    now = utcnow()
    out: list[Check] = []
    out.append(
        Check(
            "authority_mode",
            PASS if settings.billing_authority in ("local", "shadow", "central") else FAIL,
            f"SMS_BILLING_AUTHORITY={settings.billing_authority}",
        )
    )
    if target == "central" and settings.env == "production":
        out.append(
            Check(
                "production_ack",
                PASS if settings.billing_authority_ack else FAIL,
                "SMS_BILLING_AUTHORITY_ACK "
                + ("set" if settings.billing_authority_ack else "missing"),
            )
        )
    out.append(
        Check(
            "usage_reporting_enabled",
            PASS if settings.billing_usage_reporting else FAIL,
            "SMS_BILLING_USAGE_REPORTING=" + str(settings.billing_usage_reporting).lower(),
        )
    )
    subs = list(db.scalars(select(Subscription).where(Subscription.status == SubStatus.ACTIVE)))
    due = [s.id for s in subs if billing.period(s)[1] <= now]
    out.append(
        Check(
            "no_due_unbilled_periods",
            FAIL if due and target == "central" else (WARN if due else PASS),
            f"{len(due)} subscription(s) have a due, unbilled period" if due else "none",
        )
    )
    no_ent = (
        db.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.status == SubStatus.ACTIVE, Subscription.enterprise_id.is_(None))
        )
        or 0
    )
    out.append(
        Check(
            "subscriptions_have_enterprise_id",
            FAIL if no_ent else PASS,
            f"{no_ent} active subscription(s) without enterprise_id" if no_ent else "all mapped",
        )
    )
    bad = 0
    for p in db.scalars(
        select(Payment).where(
            Payment.purpose == PaymentPurpose.INVOICE, Payment.status == PaymentStatus.SUCCEEDED
        )
    ):
        inv = db.get(Invoice, p.invoice_id) if p.invoice_id else None
        if inv is None or inv.total != p.amount or inv.currency != p.currency:
            bad += 1
    out.append(
        Check(
            "invoice_payments_exact",
            WARN if bad else PASS,
            f"{bad} succeeded invoice payment(s) do not match the invoice total (manual reconciliation; V1 imports exact payments only)"
            if bad
            else "all exact",
        )
    )
    pend = db.scalar(select(func.count()).select_from(Payment).join(Invoice, Invoice.id == Payment.invoice_id).where(
        Payment.purpose == PaymentPurpose.INVOICE, Payment.status == PaymentStatus.PENDING, Invoice.status == InvoiceStatus.OPEN)) or 0  # fmt: skip
    out.append(
        Check(
            "no_pending_online_checkouts",
            WARN if pend else PASS,
            f"{pend} pending online checkout(s) on open invoices (late webhooks will be refused after cutover)"
            if pend
            else "none",
        )
    )
    ev = db.scalar(select(func.count()).select_from(EmailBillableEvent)) or 0
    rep = db.scalar(select(func.count()).select_from(BillingUsageReport)) or 0
    out.append(
        Check(
            "usage_capture_active",
            PASS if (ev or rep) else WARN,
            f"{ev} billable event(s), {rep} report(s)",
        )
    )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Billing authority readiness - Enterprise side (read-only)."
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--target", choices=("shadow", "central"), default="central")
    args = ap.parse_args(argv)
    try:
        with SessionLocal() as db:
            items = checks(db, args.target)
            db.rollback()
    except Exception as e:  # noqa: BLE001
        print(f"internal error: {type(e).__name__}", file=sys.stderr)  # noqa: T201
        return 2
    if args.json:
        print(json.dumps([asdict(c) for c in items], sort_keys=True))  # noqa: T201
    else:
        for c in items:
            print(f"{c.level} {c.name}: {c.reason}")  # noqa: T201
    return 1 if any(c.level == FAIL for c in items) else 0


if __name__ == "__main__":
    sys.exit(main())
