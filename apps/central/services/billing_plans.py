"""M9-g1: plane tregtare të versionuara (Central). Pa HTTP, pa commit (transaksioni i thirrësit).

`CommercialPlan` mban kodin; `PlanVersion` mban fushat financiare (monedhë, `monthly_fee`, `included_emails`) dhe
kalon draft → active → retired. Aktivizimi ngrin fushat dhe `content_hash`; korrigjimi = version i ri. Një draft për plan.
Çmimi i email overage NUK është këtu: vjen nga çmimi Central i produktit email (M9-e)."""

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.core.errors import Conflict, Invalid, NotFound
from apps.central.core.timeutil import utcnow
from apps.central.models.billing import (
    V_ACTIVE,
    V_DRAFT,
    V_RETIRED,
    CommercialPlan,
    PlanVersion,
)
from apps.central.services import audit, money_common

CODE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
QUANT = Decimal("0.000001")
MAX_INCLUDED = 1_000_000_000
RESOURCE_PLAN, RESOURCE_VERSION = "commercial_plan", "plan_version"


def fee(value) -> Decimal:
    """Tarifë mujore: Decimal/str/int (kurrë float/bool), e fundme, ≥ 0, ≤ 6 shifra pas presjes, brenda NUMERIC(20,6)."""
    if (
        isinstance(value, bool)
        or isinstance(value, float)
        or not isinstance(value, Decimal | str | int)
    ):
        raise Invalid("monthly_fee must be a Decimal, string or integer (never float)")
    if isinstance(value, str) and not re.fullmatch(r"\d{1,14}(\.\d{1,6})?", value):
        raise Invalid("monthly_fee must be a plain decimal (no exponent, sign or spaces)")
    try:
        d = Decimal(value)
    except Exception:  # noqa: BLE001
        raise Invalid("invalid monthly_fee") from None
    if not d.is_finite() or d < 0 or d != d.quantize(QUANT) or d > money_common.MAX_AMOUNT:
        raise Invalid("monthly_fee must be finite, >= 0, with at most 6 decimal places")
    return d.quantize(QUANT)


def included(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_INCLUDED:
        raise Invalid(f"included_emails must be an integer between 0 and {MAX_INCLUDED}")
    return value


def content_hash(
    plan_code: str, version: int, currency: str, monthly_fee: Decimal, included_emails: int
) -> str:
    doc = {"plan": plan_code, "version": version, "currency": currency,
           "monthly_fee": format(Decimal(monthly_fee).quantize(QUANT), "f"), "included_emails": included_emails}  # fmt: skip
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def get_plan(db: Session, plan_id, *, lock: bool = False) -> CommercialPlan:
    q = select(CommercialPlan).where(CommercialPlan.id == money_common.uid(plan_id, "plan id"))
    row = db.scalar(q.with_for_update() if lock else q)
    if row is None:
        raise NotFound("plan not found")
    return row


def get_version(db: Session, version_id, *, lock: bool = False) -> PlanVersion:
    q = select(PlanVersion).where(PlanVersion.id == money_common.uid(version_id, "plan version id"))
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(q)
    if row is None:
        raise NotFound("plan version not found")
    return row


def versions_of(db: Session, plan_id) -> list[PlanVersion]:
    return list(db.scalars(select(PlanVersion).where(PlanVersion.plan_id == money_common.uid(plan_id, "plan id"))
                           .order_by(PlanVersion.version)))  # fmt: skip


def create_plan(
    db: Session, actor, code: str, name: str, *, now: datetime | None = None
) -> CommercialPlan:
    actor = money_common.admin(actor)
    if not isinstance(code, str) or not CODE.match(code):
        raise Invalid("code must be 2-32 chars of a-z, 0-9, _ or -")
    nm = money_common.optional_text(name, "name")
    if not nm or len(nm) > 80:
        raise Invalid("name is required (<= 80 characters)")
    prior = db.scalar(select(CommercialPlan).where(CommercialPlan.code == code))
    if prior is not None:
        if prior.name != nm:
            raise Conflict("a plan with this code already exists with a different name")
        return prior
    p = CommercialPlan(code=code, name=nm, created_by_id=actor.id, created_at=now or utcnow())
    db.add(p)
    db.flush()
    audit.record(db, actor, "commercial_plan.create", RESOURCE_PLAN, p.id, {"code": code}, now=now)
    return p


def new_version(db: Session, actor, plan_id, currency, monthly_fee, included_emails=0, *,
                now: datetime | None = None) -> PlanVersion:  # fmt: skip
    """Draft i ri (version = i fundit + 1). Një draft për plan; monedha e çdo versioni të një plani është e njëjtë."""
    actor = money_common.admin(actor)
    cur, f, inc = money_common.currency(currency), fee(monthly_fee), included(included_emails)
    plan = get_plan(db, plan_id, lock=True)
    existing = versions_of(db, plan.id)
    if any(v.status == V_DRAFT for v in existing):
        raise Conflict("a draft already exists for this plan")
    if existing and existing[0].currency != cur:
        raise Conflict("all versions of a plan share one currency (no FX)")
    v = PlanVersion(plan_id=plan.id, version=(existing[-1].version + 1) if existing else 1, status=V_DRAFT,
                    currency=cur, monthly_fee=f, included_emails=inc, created_by_id=actor.id,
                    created_at=now or utcnow())  # fmt: skip
    db.add(v)
    db.flush()
    audit.record(db, actor, "plan_version.create", RESOURCE_VERSION, v.id,
                 {"plan": plan.code, "version": v.version, "currency": cur, "monthly_fee": str(f), "included_emails": inc}, now=now)  # fmt: skip
    return v


def update_draft(
    db: Session,
    actor,
    version_id,
    *,
    monthly_fee=None,
    included_emails=None,
    now: datetime | None = None,
) -> PlanVersion:
    """Korrigjim i një DRAFT (një version i aktivizuar s'ndryshon kurrë). Pa ndryshim real ⇒ no-op (pa audit)."""
    actor = money_common.admin(actor)
    v = get_version(db, version_id, lock=True)
    if v.status != V_DRAFT:
        raise Conflict(
            "plan version is not a draft: financial fields are immutable (create a new version)"
        )
    changes = {}
    if monthly_fee is not None and (f := fee(monthly_fee)) != v.monthly_fee:
        changes["monthly_fee"] = {"before": str(v.monthly_fee), "after": str(f)}
        v.monthly_fee = f
    if included_emails is not None and (inc := included(included_emails)) != v.included_emails:
        changes["included_emails"] = {"before": v.included_emails, "after": inc}
        v.included_emails = inc
    if changes:
        db.flush()
        audit.record(db, actor, "plan_version.update", RESOURCE_VERSION, v.id, changes, now=now)
    return v


def activate(db: Session, actor, version_id, *, now: datetime | None = None) -> PlanVersion:
    """draft → active: ngrin fushat financiare + `content_hash`. active ⇒ no-op. retired ⇒ Conflict."""
    actor = money_common.admin(actor)
    v = get_version(db, version_id, lock=True)
    if v.status == V_ACTIVE:
        return v
    if v.status != V_DRAFT:
        raise Conflict(f"plan version is {v.status}; create a new version")
    plan = get_plan(db, v.plan_id)
    now = now or utcnow()
    v.status, v.activated_at, v.activated_by_id = V_ACTIVE, now, actor.id
    v.content_hash = content_hash(
        plan.code, v.version, v.currency, v.monthly_fee, v.included_emails
    )
    db.flush()
    audit.record(db, actor, "plan_version.activate", RESOURCE_VERSION, v.id,
                 {"plan": plan.code, "version": v.version, "content_hash": v.content_hash}, now=now)  # fmt: skip
    return v


def retire(db: Session, actor, version_id, reason, *, now: datetime | None = None) -> PlanVersion:
    """active → retired: s'caktohet më te abonime të reja; abonimet ekzistuese vazhdojnë të faturohen me të. retired ⇒ no-op."""
    actor = money_common.admin(actor)
    why = money_common.reason(reason)
    v = get_version(db, version_id, lock=True)
    if v.status == V_RETIRED:
        return v
    if v.status != V_ACTIVE:
        raise Conflict("only an active plan version can be retired")
    now = now or utcnow()
    v.status, v.retired_at, v.retired_by_id, v.retire_reason = V_RETIRED, now, actor.id, why
    db.flush()
    audit.record(db, actor, "plan_version.retire", RESOURCE_VERSION, v.id, {"reason": why}, now=now)
    return v
