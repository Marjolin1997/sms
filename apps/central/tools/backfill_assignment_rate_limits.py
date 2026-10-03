"""Backfill manual i `EnterpriseProduct.rate_limit_per_min` nga AccountPlan legacy (M7-g).

    ENTERPRISE_DATABASE_URL=... CENTRAL_DATABASE_URL=... \\
        python -m apps.central.tools.backfill_assignment_rate_limits [--apply] [--format json]

DRY-RUN është parazgjedhja. Enterprise DB lexohet vetëm-lexim; shkruhet vetëm Central, vetëm
assignment-e EKZISTUESE (nuk krijon assignment/Product/Enterprise), me shërbimin normal
`set_rate_limit` (revision + outbox) dhe audit `system:rate_limit_backfill`, një transaksion.

SMS   : AccountPlan.rate_limit_per_min       → assignment-i `sms`
Email : AccountPlan.email_rate_limit_per_min → assignment-i `email`
Rregullat: legacy NULL ose 0 ("përdor default-in": `or DEFAULT` te legacy) ⇒ noop (NULL ruhet NULL,
kurrë default-i 600 si vlerë e ruajtur); Central NULL + legacy X ⇒ set X; Central == legacy ⇒ noop;
Central ≠ legacy ⇒ CONFLICT (kurrë mbishkrim); legacy jashtë 1..1_000_000 ⇒ invalid; assignment
mungon ⇒ no_assignment (raportohet; e krijon M7-f); enterprise mungon në Central ⇒ invalid.
Invalid/conflict ⇒ ZERO shkrime. Kodet: 0 ok, 1 konflikte/invalid, 2 konfigurim."""

import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.models.enterprise import Enterprise
from apps.central.models.enterprise_product import EnterpriseProduct
from apps.central.models.product import Product
from apps.central.services import audit
from apps.central.services import enterprise_products as asg

ACTOR_LABEL = "system:rate_limit_backfill"
CHANNELS = (("sms", "rate_limit_per_min"), ("email", "email_rate_limit_per_min"))
SET, NOOP, CONFLICT, INVALID, NO_ASSIGNMENT = "set", "noop", "conflict", "invalid", "no_assignment"

_SOURCE = text(
    "select e.id, e.owner_ref, p.rate_limit_per_min, p.email_rate_limit_per_min "
    "from sms_enterprises e join sms_account_plans p on p.owner_ref = e.owner_ref "
    "order by e.created_at, e.id"
)


@dataclass
class Row:
    enterprise_id: str
    owner_ref: str
    product_code: str
    legacy: int | None
    central: int | None
    action: str
    reason: str = ""


@dataclass
class Report:
    rows: list[Row] = field(default_factory=list)
    apply: bool = False
    written: int = 0
    apply_error: str | None = None

    def n(self, action: str) -> int:
        return sum(1 for r in self.rows if r.action == action)

    @property
    def ok(self) -> bool:
        return not (self.n(CONFLICT) or self.n(INVALID) or self.apply_error)

    def counts(self) -> dict:
        return {a: self.n(a) for a in (SET, NOOP, CONFLICT, INVALID, NO_ASSIGNMENT)}

    def to_dict(self) -> dict:
        return {
            "mode": "apply" if self.apply else "dry-run", "ok": self.ok, "written": self.written,
            "counts": self.counts(), "apply_error": self.apply_error,
            "rows": [r.__dict__ for r in self.rows],
        }  # fmt: skip

    def render(self) -> str:
        out = [f"{k}: {v}" for k, v in self.counts().items()]
        out.append(
            f"Mode: {'apply' if self.apply else 'dry-run (0 writes)'}; written: {self.written}"
        )
        for r in self.rows:
            if r.action != NOOP:
                out.append(f"{r.action.upper():13} enterprise_id={r.enterprise_id} product={r.product_code} "
                           f"legacy={r.legacy} central={r.central} {r.reason}".rstrip())  # fmt: skip
        if self.apply_error:
            out.append(f"APPLY_ERROR {self.apply_error}")
        return "\n".join(out)


def read_source(url: str) -> list[tuple]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SET TRANSACTION READ ONLY" if engine.dialect.name == "postgresql"
                              else "PRAGMA query_only = ON"))  # fmt: skip
            return [tuple(r) for r in conn.execute(_SOURCE)]
    finally:
        engine.dispose()


def _uuid(v) -> uuid.UUID | None:
    if isinstance(v, uuid.UUID) or v is None:
        return v
    try:
        return uuid.UUID(str(v))
    except ValueError:
        return None


def build_plan(source: list[tuple], central: Session) -> Report:
    rep = Report()
    ents = {e.id for e in central.scalars(select(Enterprise))}
    asgs = {(ep.enterprise_id, p.code): ep for ep, p in central.execute(
        select(EnterpriseProduct, Product).join(Product, Product.id == EnterpriseProduct.product_id))}  # fmt: skip
    for raw_id, owner, sms_lim, email_lim in source:
        eid = _uuid(raw_id)
        for (code, _col), legacy in zip(CHANNELS, (sms_lim, email_lim), strict=True):
            row = Row(str(eid or raw_id), str(owner), code, legacy, None, NOOP)
            rep.rows.append(row)
            if eid is None or eid not in ents:
                row.action, row.reason = INVALID, "enterprise missing in Central"
                continue
            if legacy is not None and (isinstance(legacy, bool) or not isinstance(legacy, int)
                                       or legacy < 0 or legacy > asg.RATE_LIMIT_MAX):  # fmt: skip
                row.action, row.reason = INVALID, "legacy value outside 0..1000000"
                continue
            ep = asgs.get((eid, code))
            if ep is None:
                row.action, row.reason = NO_ASSIGNMENT, "no Central assignment (M7-f creates it)"
                continue
            row.central = ep.rate_limit_per_min
            if not legacy:  # NULL ose 0 ⇒ default lokal: asgjë për të ruajtur
                row.reason = "legacy inherits the local default"
            elif ep.rate_limit_per_min is None:
                row.action = SET
            elif ep.rate_limit_per_min == legacy:
                row.reason = "matching"
            else:
                row.action, row.reason = CONFLICT, "Central value differs; not overwritten"
    return rep


def run(
    enterprise_url: str, central_url: str, *, apply: bool = False, central_engine=None
) -> Report:
    if enterprise_url == central_url:
        raise ValueError("ENTERPRISE_DATABASE_URL and CENTRAL_DATABASE_URL must differ")
    source = read_source(enterprise_url)
    engine = central_engine or make_engine(central_url)
    try:
        with Session(engine, expire_on_commit=False) as db:
            rep = build_plan(source, db)
            rep.apply = apply
            todo = [r for r in rep.rows if r.action == SET]
            if not apply or not rep.ok or not todo:
                db.rollback()
                return rep
            try:
                now = datetime.now(UTC)
                for r in todo:
                    ep = db.scalar(
                        select(EnterpriseProduct).join(Product, Product.id == EnterpriseProduct.product_id)
                        .where(EnterpriseProduct.enterprise_id == uuid.UUID(r.enterprise_id),
                               Product.code == r.product_code)
                    )  # fmt: skip
                    _, _, changes = asg.set_rate_limit(
                        db, ep.enterprise_id, ep.id, r.legacy, now=now
                    )
                    audit.record_system(
                        db, label=ACTOR_LABEL, action="enterprise_product.rate_limit_backfill",
                        resource_type="enterprise_product", resource_id=ep.id,
                        detail={"enterprise_id": r.enterprise_id, "product_code": r.product_code,
                                "source": "AccountPlan", **changes},
                        now=now,
                    )  # fmt: skip
                db.commit()
                rep.written = len(todo)
            except Exception as e:  # noqa: BLE001
                db.rollback()
                rep.apply_error = f"{type(e).__name__}: {str(e)[:200]}"
            return rep
    finally:
        if central_engine is None:
            engine.dispose()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backfill assignment rate limits from AccountPlan.")
    ap.add_argument("--apply", action="store_true", help="shkruaj (parazgjedhja: dry-run)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args(argv)
    ent = os.environ.get("ENTERPRISE_DATABASE_URL")
    if (args.apply and args.dry_run) or not ent:
        print(
            "ENTERPRISE_DATABASE_URL is required; --apply and --dry-run are exclusive",
            file=sys.stderr,
        )  # noqa: T201
        return 2
    try:
        rep = run(ent, settings.database_url, apply=args.apply)
    except Exception as e:  # pa URL/sekrete
        print(f"backfill failed: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)  # noqa: T201
        return 2
    print(
        json.dumps(rep.to_dict(), indent=1, sort_keys=True)
        if args.format == "json"
        else rep.render()
    )  # noqa: T201
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
