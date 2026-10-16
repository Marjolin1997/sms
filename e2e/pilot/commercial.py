"""Të dhëna komerciale Central për stack-un pilot M11-H (PostgreSQL h_cen). Variant i e2e/commercial_seed.py: enterprise-t = tenant-ët A/B të pilotit.

    . $PILOT_DIR/pilot.env && python -m e2e.pilot.commercial
(gjeneruar nga e2e/commercial_seed.py: vetëm roja e DB-së, adresat e adminit dhe lidhja enterprise→tenant ndryshojnë)

Krijon (me shërbimet reale, jo me SQL): admin i dytë (checker), llogari krediti + pagesë e miratuar + grant + pagesë në pritje,
plan + profile + abonime + fatura (një e paguar me notë krediti, një e hapur), libër çmimesh me rregulla dhe caktim.
Shton te <ids.json> vetëm ID-të e krijuara.
"""

import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.models.user import CentralUser
from apps.central.services import (
    billing,
    billing_plans,
    credit_accounts,
    credit_notes,
    grants,
    invoice_payments,
    payments,
    pricing,
)
from apps.central.services import products as prod
from apps.central.tools import create_admin

_NOW = datetime.now(UTC).replace(microsecond=0)
PAST = _NOW - timedelta(days=75)  # abonimi fillon këtu; periudha 0 mbyllet para AFTER
AFTER = _NOW - timedelta(
    days=35
)  # kohë e simuluar e faturimit: faturat e hapura janë tashmë me vonesë


def main(path: str) -> None:
    if "/h_cen" not in settings.database_url:
        raise SystemExit("refusing: only the temporary h_cen database")
    ids = json.load(open(path))
    ids["enterprise_acme"], ids["enterprise_globex"] = (
        ids["tenants"]["a"]["enterprise_id"],
        ids["tenants"]["b"]["enterprise_id"],
    )
    engine = make_engine()
    code, msg = create_admin.run(
        "admin2@pilot.test", os.environ["PILOT_ADMIN_PASSWORD"], "admin", engine
    )
    assert code == 0, msg
    with Session(engine) as db:
        a1 = db.scalar(select(CentralUser).where(CentralUser.email == "admin@pilot.test"))
        a2 = db.scalar(select(CentralUser).where(CentralUser.email == "admin2@pilot.test"))
        acme, globex = uuid.UUID(ids["enterprise_acme"]), uuid.UUID(ids["enterprise_globex"])
        # --- kredi ---
        product = prod.create(db, "sms", "SMS", "sms")
        acct = credit_accounts.create(db, acme, product.id, "EUR", a1)
        pay = payments.create(db, acct.id, "2500.50", actor=a1, external_reference="BANK-2026-001")
        payments.approve(db, pay.id, a2)
        grants.issue(
            db, acct.id, "500.25", idempotency_key="e2e-grant-0001", actor=a1, note="welcome credit"
        )
        pending = payments.create(db, acct.id, "75", actor=a1, external_reference="BANK-2026-002")
        ids.update(
            product=str(product.id),
            account=str(acct.id),
            payment=str(pay.id),
            pending_payment=str(pending.id),
        )
        # --- faturim ---
        plan = billing_plans.create_plan(db, a1, "growth", "Growth")
        ver = billing_plans.new_version(db, a1, plan.id, "EUR", "1000", 0)
        billing_plans.activate(db, a1, ver.id)
        invoices = {}
        for key, ent, name in (
            ("acme", acme, "Acme Sh.p.k."),
            ("globex", globex, "Globex Sh.p.k."),
        ):
            billing.set_profile(
                db,
                a1,
                ent,
                name,
                "Rr. Kryesore 1, Tirane",
                "al",
                f"{key}@example.test",
                "K12345678A",
                "0.2",
            )
            sub = billing.assign_plan(db, a1, ent, ver.id, now=PAST)
            res = billing.process_period(db, sub.id, AFTER)
            invoices[key] = res.invoice
        # M11-E2: 26 faturat shtesë me data të ndryshme (faqosje > 25 + filtra datash); çdo enterprise ka abonimin e vet
        from apps.central.services import enterprises

        extra = []
        for i in range(26):
            after = _NOW - timedelta(days=50 + 3 * i)
            ent = enterprises.create(db, f"Bulk {i:02d} Sh.p.k.").id
            billing.set_profile(
                db,
                a1,
                ent,
                f"Bulk {i:02d} Sh.p.k.",
                "Rr. Bulk 1, Tiranë",
                "al",
                f"b{i}@example.test",
                "K12345678A",
                "0.2",
            )
            sub = billing.assign_plan(db, a1, ent, ver.id, now=after - timedelta(days=40))
            extra.append(billing.process_period(db, sub.id, after).invoice)
        ids["invoice_bulk_oldest"] = str(extra[-1].id)
        ids["bulk_issue_day"] = (_NOW - timedelta(days=50 + 3 * 25)).strftime("%Y-%m-%d")
        ids["acme_issue_day"] = AFTER.strftime("%Y-%m-%d")
        paid = invoices["acme"]
        ip = invoice_payments.create(
            db, paid.id, str(paid.total), actor=a1, external_reference="INV-PAY-77", now=AFTER
        )
        invoice_payments.approve(db, ip.id, a2, now=AFTER)
        note = credit_notes.issue(
            db, a1, paid.id, "50", "goodwill discount", "e2e-note-0001", now=AFTER
        )
        ids.update(
            invoice_paid=str(paid.id),
            invoice_open=str(invoices["globex"].id),
            credit_note=str(note.id),
            invoice_paid_number=paid.number,
            invoice_open_number=invoices["globex"].number,
        )
        # --- çmime ---
        book = pricing.create_book(db, a1, "std", "Standard", "EUR", now=PAST)
        v = pricing.new_draft(db, a1, book.id, now=PAST)
        pricing.set_rule(db, a1, v.id, "sms", "0.045", prefix="355", now=PAST)
        pricing.set_rule(db, a1, v.id, "sms", "0.100", prefix="49", now=PAST)
        eff = PAST + timedelta(days=1)
        pricing.activate(db, a1, v.id, eff, now=PAST)
        pricing.assign(db, a1, acme, product.id, book.id, eff, now=PAST)
        ids.update(book=str(book.id), version=str(v.id))
        db.commit()
    with open(path, "w") as fh:
        json.dump(ids, fh)


if __name__ == "__main__":
    main(os.path.join(os.environ["PILOT_DIR"], "ids.json"))
