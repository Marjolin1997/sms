"""M9-g4: eksporti i faturimit legacy të Enterprise → artifact `cp.billing.legacy_export.v1` për importin në Central. VETËM LEXIM, pa mutacion të rreshtave legacy.

Lexon në NJË snapshot (REPEATABLE READ, read-only në PostgreSQL) plane, profile, abonime, fatura+linja, pagesa fature, dëshmi wallet (pa rindërtim debiti), maksimumet e
numërimit dhe gjendjen e hapjes së përdorimit të email-it. Artifact-i është offline (asnjë lidhje DB mes Enterprise dhe Central). Asgjë s'shkurtohet në heshtje:
fusha mbi kufijtë e kontratës ngrenë `ValueError` (eksport i ndërprerë, jo i prerë)."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import as_utc, utcnow
from app.models.billing import (
    BillingProfile,
    Invoice,
    InvoiceCounter,
    InvoiceLine,
    Payment,
    PaymentPurpose,
    Plan,
    Subscription,
    SubStatus,
)
from app.models.billing_usage import BillingUsageReport, EmailBillableEvent
from app.models.wallet import EntryType, LedgerEntry
from app.services import billing, billing_usage
from app.services.money_usage import snapshot_session
from packages.contracts.control_plane.billing import legacy_export_v1 as lx

Q6 = Decimal("0.000001")


def dec(x) -> str:
    return format(Decimal(x).quantize(Q6), "f")


def ts(x) -> str:
    return lx.format_ts(as_utc(x))


def ots(x) -> str | None:
    return None if x is None else ts(x)


def uid(x) -> str | None:
    return None if x is None else str(x)


def alembic_head(db: Session) -> str:
    from sqlalchemy import text

    try:
        return str(
            db.execute(text("select version_num from sms_alembic_version")).scalar() or "unknown"
        )[:32]
    except Exception:  # noqa: BLE001  (SQLite/test: pa tabelë versioni)
        db.rollback()
        return "unknown"


def build(db: Session, now: datetime | None = None, export_id: uuid.UUID | None = None) -> dict:
    now = as_utc(now or utcnow())
    plans = list(db.scalars(select(Plan).order_by(Plan.id)))
    profs = list(db.scalars(select(BillingProfile).order_by(BillingProfile.id)))
    subs = list(db.scalars(select(Subscription).order_by(Subscription.id)))
    invs = list(db.scalars(select(Invoice).order_by(Invoice.id)))
    lines: dict[int, list[InvoiceLine]] = {}
    for ln in db.scalars(select(InvoiceLine).order_by(InvoiceLine.invoice_id, InvoiceLine.id)):
        lines.setdefault(ln.invoice_id, []).append(ln)
    pays = list(
        db.scalars(
            select(Payment).where(Payment.purpose == PaymentPurpose.INVOICE).order_by(Payment.id)
        )
    )
    wallet = []
    for inv in invs:
        if inv.paid_via == "wallet":
            e = db.scalar(select(LedgerEntry).where(LedgerEntry.entry_type == EntryType.INVOICE, LedgerEntry.ref_type == "invoice",
                                                    LedgerEntry.ref_id == inv.number).order_by(LedgerEntry.id).limit(1))  # fmt: skip
            if e is not None:
                wallet.append({"invoice_source_id": inv.id, "ledger_entry_id": e.id, "amount": dec(-Decimal(e.available_delta)),
                               "currency": inv.currency, "created_at": ts(e.created_at)})  # fmt: skip
    counters = list(db.scalars(select(InvoiceCounter).order_by(InvoiceCounter.year)))
    cap_candidates = [
        t
        for t in (
            db.scalar(select(func.min(EmailBillableEvent.created_at))),
            db.scalar(select(func.min(BillingUsageReport.created_at))),
        )
        if t is not None
    ]
    capture_since = ots(min(as_utc(t) for t in cap_candidates)) if cap_candidates else None
    usage = []
    due = 0
    for s in subs:
        if s.status != SubStatus.ACTIVE:
            continue
        start, end = billing.period(s)
        if end <= now:
            due += 1
        if s.enterprise_id is None:
            continue
        pid = billing_usage.email_product_for(db, s.enterprise_id)
        row = db.execute(select(func.count(), func.coalesce(func.max(EmailBillableEvent.id), 0)).where(
            EmailBillableEvent.enterprise_id == s.enterprise_id, EmailBillableEvent.billable_at < start)).one()  # fmt: skip
        total = (
            db.scalar(
                select(func.count())
                .select_from(EmailBillableEvent)
                .where(EmailBillableEvent.enterprise_id == s.enterprise_id)
            )
            or 0
        )
        usage.append({"enterprise_id": str(s.enterprise_id), "product_id": uid(pid), "boundary": ts(start), "cumulative_before_boundary": int(row[0]),
                      "watermark_before_boundary": int(row[1]), "capture_active_since": capture_since, "events_total": int(total)})  # fmt: skip
    usage.sort(key=lambda r: r["enterprise_id"])
    doc = {
        "schema": lx.SCHEMA, "export_id": str(export_id or uuid.uuid4()), "generated_at": ts(now),
        "source": {"system": "enterprise", "alembic_head": alembic_head(db)},
        "authority": {"mode": settings.billing_authority, "due_unbilled_periods": due},
        "plans": [{"source_id": p.id, "code": p.code, "name": p.name, "currency": p.currency, "monthly_fee": dec(p.monthly_fee),
                   "included_emails": int(p.included_emails), "email_overage_price": dec(p.email_overage_price), "status": p.status.value,
                   "created_at": ts(p.created_at)} for p in plans],  # fmt: skip
        "profiles": [{"source_id": p.id, "enterprise_id": uid(p.enterprise_id), "owner_ref": p.owner_ref, "legal_name": p.legal_name, "address": p.address,
                      "country": p.country, "tax_id": p.tax_id, "email": p.email, "vat_rate": dec(p.vat_rate), "updated_at": ts(p.updated_at)}
                     for p in profs],  # fmt: skip
        "subscriptions": [{"source_id": s.id, "enterprise_id": uid(s.enterprise_id), "owner_ref": s.owner_ref, "plan_source_id": s.plan_id,
                           "pending_plan_source_id": s.pending_plan_id, "status": s.status.value, "started_at": ts(s.started_at),
                           "periods_billed": int(s.periods_billed), "cancel_at_period_end": bool(s.cancel_at_period_end), "auto_pay": bool(s.auto_pay),
                           "created_at": ts(s.created_at)} for s in subs],  # fmt: skip
        "invoices": [
            {"source_id": i.id, "number": i.number, "enterprise_id": uid(i.enterprise_id), "owner_ref": i.owner_ref, "subscription_source_id": i.subscription_id,
             "period_start": ts(i.period_start), "period_end": ts(i.period_end), "currency": i.currency, "subtotal": dec(i.subtotal), "vat_rate": dec(i.vat_rate),
             "tax": dec(i.tax), "total": dec(i.total), "status": i.status.value, "bill_to": i.bill_to, "issued_at": ts(i.issued_at), "due_at": ts(i.due_at),
             "paid_at": ots(i.paid_at), "paid_via": i.paid_via, "voided_reason": i.voided_reason,
             "lines": [{"source_id": ln.id, "description": ln.description, "quantity": dec(ln.quantity), "unit_price": dec(ln.unit_price),
                        "amount": dec(ln.amount), "pricing_source": ln.pricing_source, "pricing_version_ref": uid(ln.pricing_version_ref)}
                       for ln in lines.get(i.id, [])]}
            for i in invs
        ],  # fmt: skip
        "payments": [{"source_id": p.id, "enterprise_id": uid(p.enterprise_id), "invoice_source_id": p.invoice_id, "amount": dec(p.amount), "currency": p.currency,
                      "provider": p.provider, "external_id": p.external_id, "status": p.status.value, "completed_at": ots(p.completed_at)}
                     for p in pays if p.invoice_id is not None],  # fmt: skip
        "wallet_settlements": sorted(wallet, key=lambda r: r["invoice_source_id"]),
        "sequences": {"invoice_counters": [{"year": int(c.year), "last_number": int(c.last_number)} for c in counters], "credit_note_like": 0},
        "usage": usage,
    }  # fmt: skip
    doc["counts"] = {k: len(doc[k]) for k in lx.ENTITIES}
    return lx.seal(doc)


def export(engine: Engine, now: datetime | None = None, export_id: uuid.UUID | None = None) -> dict:
    """Snapshot REPEATABLE READ + validim i plotë i artifact-it para se të kthehet (artifact i pavlefshëm s'del kurrë)."""
    with snapshot_session(engine) as db:
        doc = build(db, now, export_id)
    lx.parse(doc)
    return doc
