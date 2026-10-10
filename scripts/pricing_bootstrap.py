"""M9-e: eksporti (VETËM-LEXIM) i tarifave ekzistuese të Enterprise si propozim `pricing-bootstrap.v1` për Central.

    python -m scripts.pricing_bootstrap export --out pricing.json

Përmban: libra (rate cards me versionet e PUBLIKUARA + rregullat; drafte përjashtohen) dhe libra email (planet me `email_overage_price` > 0),
caktime (owner_ref → enterprise_id kur ekziston, përndryshe `null` ⇒ `unmapped` te Central). Nuk shkruan asgjë, nuk zhvendos/fshin asgjë.
Hapi tjetër: `python -m apps.central.tools.pricing_import --proposal pricing.json` (dry-run), pastaj `--apply` me miratim të hash-it.
Klasifikimi exact/conflict/invalid/unmapped bëhet te Central (ai njeh librat ekzistues)."""

import argparse
import json
import sys
from datetime import UTC

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.timeutil import utcnow
from app.models.billing import Plan, Subscription
from app.models.enterprise import Enterprise
from app.models.rates import Rate, RateCard, RateCardVersion, VersionStatus
from app.models.sending import AccountPlan

SCHEMA = "pricing-bootstrap.v1"


def _iso(dt) -> str:
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.isoformat(timespec="microseconds")


def build(db) -> dict:
    books, assignments = [], []
    cards = {c.id: c for c in db.scalars(select(RateCard).order_by(RateCard.id))}
    for c in cards.values():
        versions = []
        for v in db.scalars(select(RateCardVersion).where(RateCardVersion.rate_card_id == c.id,
                                                          RateCardVersion.status == VersionStatus.PUBLISHED)
                            .order_by(RateCardVersion.effective_from)):  # fmt: skip
            rules = [{"channel": "sms", "prefix": r.prefix, "operator": r.operator, "unit_price": format(r.price_per_segment, "f")}
                     for r in db.scalars(select(Rate).where(Rate.version_id == v.id).order_by(Rate.prefix, Rate.operator))]  # fmt: skip
            versions.append(
                {"version": v.version, "effective_from": _iso(v.effective_from), "rules": rules}
            )
        books.append({"code": c.name, "name": c.name, "currency": c.currency, "versions": versions})
    for p in db.scalars(select(AccountPlan).order_by(AccountPlan.owner_ref)):
        ent = db.scalar(select(Enterprise).where(Enterprise.owner_ref == p.owner_ref))
        card = cards.get(p.rate_card_id)
        assignments.append({"owner_ref": p.owner_ref, "enterprise_id": str(ent.id) if ent else None, "channel": "sms",
                            "book_code": card.name if card else None})  # fmt: skip
    seen = set()
    for plan in db.scalars(select(Plan).where(Plan.email_overage_price > 0).order_by(Plan.id)):
        code = f"email-{plan.code}"
        books.append({"code": code, "name": f"Email overage {plan.name}", "currency": plan.currency,
                      "versions": [{"version": 1, "effective_from": _iso(plan.created_at),
                                    "rules": [{"channel": "email", "prefix": "", "operator": "", "unit_price": format(plan.email_overage_price, "f")}]}]})  # fmt: skip
        for sub in db.scalars(select(Subscription).where(Subscription.plan_id == plan.id)):
            if (sub.owner_ref, code) in seen:
                continue
            seen.add((sub.owner_ref, code))
            ent = db.scalar(select(Enterprise).where(Enterprise.owner_ref == sub.owner_ref))
            assignments.append(
                {
                    "owner_ref": sub.owner_ref,
                    "enterprise_id": str(ent.id) if ent else None,
                    "channel": "email",
                    "book_code": code,
                }
            )
    return {
        "schema": SCHEMA,
        "generated_at": _iso(utcnow()),
        "books": books,
        "assignments": assignments,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    with SessionLocal() as db:
        doc = build(db)
        db.rollback()
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
    print(
        f"{len(doc['books'])} book(s), {len(doc['assignments'])} assignment(s) written to {a.out} (read-only export)"
    )  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
