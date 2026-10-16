"""API admin për çmimet e klientit (M9-f). Admin = shkrim, operator = lexim. Pa DELETE: heqja e rregullës nga një DRAFT është
`POST …/rules/remove`. Versioni aktiv/retired është i pandryshueshëm (shërbimi + trigger PG). Çmimet janë string dhjetorë."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, StrictStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.api.admin_common import (
    STRICT,
    Aware,
    Currency,
    Reason,
    UnitPrice,
    iso,
    page,
)
from apps.central.api.deps import get_db, require_role
from apps.central.core.errors import Invalid
from apps.central.core.timeutil import utcnow
from apps.central.models.pricing import (
    V_ACTIVE,
    PriceAssignment,
    PriceBook,
    PriceVersion,
)
from apps.central.models.user import CentralUser, Role
from apps.central.services import financial_ops, pricing
from packages.contracts.control_plane.pricing import v1 as pv

router = APIRouter(prefix="/admin/pricing")
READ = require_role(Role.ADMIN, Role.OPERATOR)
WRITE = require_role(Role.ADMIN)
MAX_PAGE = 200


class BookIn(BaseModel):
    model_config = STRICT
    code: StrictStr
    name: StrictStr
    currency: Currency


class RuleIn(BaseModel):
    model_config = STRICT
    channel: StrictStr
    unit_price: UnitPrice
    prefix: StrictStr = ""
    operator: StrictStr = ""


class RuleRef(BaseModel):
    model_config = STRICT
    channel: StrictStr
    prefix: StrictStr = ""
    operator: StrictStr = ""


class ActivateIn(BaseModel):
    model_config = STRICT
    effective_from: Aware


class RetireIn(BaseModel):
    model_config = STRICT
    reason: Reason


class AssignIn(BaseModel):
    model_config = STRICT
    enterprise_id: uuid.UUID
    product_id: uuid.UUID
    book_id: uuid.UUID
    effective_from: Aware


def book_out(b: PriceBook) -> dict:
    return {
        "id": str(b.id),
        "code": b.code,
        "name": b.name,
        "currency": b.currency,
        "created_at": iso(b.created_at),
    }


def version_out(
    v: PriceVersion, rules: list[dict] | None = None, rule_count: int | None = None
) -> dict:
    out = {
        "id": str(v.id), "book_id": str(v.price_book_id), "version": v.version, "status": v.status,
        "effective_from": iso(v.effective_from), "content_hash": v.content_hash, "imported": v.imported,
        "created_at": iso(v.created_at), "activated_at": iso(v.activated_at), "retired_at": iso(v.retired_at),
        "retire_reason": v.retire_reason, "editable": v.status == "draft",
    }  # fmt: skip
    if rules is not None:
        out["rules"] = rules
        out["rule_count"] = len(rules)
    elif rule_count is not None:
        out["rule_count"] = rule_count
    return out


def assignment_out(a: PriceAssignment) -> dict:
    return {"id": str(a.id), "enterprise_id": str(a.enterprise_id), "product_id": str(a.product_id),
            "book_id": str(a.price_book_id), "effective_from": iso(a.effective_from), "created_at": iso(a.created_at)}  # fmt: skip


def _rules(db: Session, version_id) -> list[dict]:
    return sorted(
        pricing.rules_of(db, version_id), key=lambda r: (r["channel"], r["prefix"], r["operator"])
    )


# --- librat -----------------------------------------------------------------------------------------------------


@router.get("/books")
def list_books(limit: int = Query(50, ge=1, le=MAX_PAGE), offset: int = Query(0, ge=0),
               db: Session = Depends(get_db), _: CentralUser = Depends(READ)):  # fmt: skip
    rows = list(
        db.scalars(
            select(PriceBook)
            .order_by(PriceBook.created_at, PriceBook.id)
            .limit(limit + 1)
            .offset(offset)
        )
    )
    return page(rows, limit, offset, book_out)


@router.post("/books", status_code=201)
def create_book(body: BookIn, db: Session = Depends(get_db), actor: CentralUser = Depends(WRITE)):
    b = pricing.create_book(db, actor, body.code, body.name, body.currency)
    db.commit()
    return book_out(b)


@router.get("/books/{book_id}")
def get_book(book_id: uuid.UUID, db: Session = Depends(get_db), _: CentralUser = Depends(READ)):
    b = pricing.get_book(db, book_id)
    versions = list(
        db.scalars(
            select(PriceVersion)
            .where(PriceVersion.price_book_id == b.id)
            .order_by(PriceVersion.version)
        )
    )
    return {
        **book_out(b),
        "versions": [version_out(v, rule_count=len(pricing.rules_of(db, v.id))) for v in versions],
    }


@router.post("/books/{book_id}/versions", status_code=201)
def new_draft(
    book_id: uuid.UUID, db: Session = Depends(get_db), actor: CentralUser = Depends(WRITE)
):
    v = pricing.new_draft(db, actor, book_id)
    db.commit()
    return version_out(v, _rules(db, v.id))


# --- versionet dhe rregullat ---------------------------------------------------------------------------------------------


@router.get("/versions/{version_id}")
def get_version(
    version_id: uuid.UUID, db: Session = Depends(get_db), _: CentralUser = Depends(READ)
):
    v = pricing.get_version(db, version_id)
    return version_out(v, _rules(db, v.id))


@router.post("/versions/{version_id}/rules")
def set_rule(
    version_id: uuid.UUID,
    body: RuleIn,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    _, changed = pricing.set_rule(db, actor, version_id, body.channel, body.unit_price,
                                  prefix=body.prefix, operator=body.operator)  # fmt: skip
    db.commit()
    return {
        "changed": changed,
        "version": version_out(pricing.get_version(db, version_id), _rules(db, version_id)),
    }


@router.post("/versions/{version_id}/rules/remove")
def remove_rule(
    version_id: uuid.UUID,
    body: RuleRef,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    changed = pricing.remove_rule(
        db, actor, version_id, body.channel, prefix=body.prefix, operator=body.operator
    )
    db.commit()
    return {
        "changed": changed,
        "version": version_out(pricing.get_version(db, version_id), _rules(db, version_id)),
    }


@router.post("/versions/{version_id}/activate")
def activate(
    version_id: uuid.UUID,
    body: ActivateIn,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    v = pricing.activate(db, actor, version_id, body.effective_from)
    db.commit()
    return version_out(v, _rules(db, v.id))


@router.post("/versions/{version_id}/retire")
def retire(
    version_id: uuid.UUID,
    body: RetireIn,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    v = pricing.retire(db, actor, version_id, body.reason)
    db.commit()
    return version_out(v, _rules(db, v.id))


# --- caktimet ---------------------------------------------------------------------------------------------------


@router.get("/assignments")
def list_assignments(
    enterprise_id: uuid.UUID | None = None,
    product_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: CentralUser = Depends(READ),
):
    q = select(PriceAssignment)
    if enterprise_id is not None:
        q = q.where(PriceAssignment.enterprise_id == enterprise_id)
    if product_id is not None:
        q = q.where(PriceAssignment.product_id == product_id)
    q = q.order_by(
        PriceAssignment.enterprise_id, PriceAssignment.product_id, PriceAssignment.effective_from
    )
    return page(list(db.scalars(q.limit(limit + 1).offset(offset))), limit, offset, assignment_out)


@router.post("/assignments", status_code=201)
def assign(body: AssignIn, db: Session = Depends(get_db), actor: CentralUser = Depends(WRITE)):
    a = pricing.assign(
        db, actor, body.enterprise_id, body.product_id, body.book_id, body.effective_from
    )
    db.commit()
    return assignment_out(a)


# --- gjendja, parapamja, gatishmëria (vetëm lexim) ---------------------------------------------------------------------------


@router.get("/state")
def state(db: Session = Depends(get_db), _: CentralUser = Depends(READ)):
    epoch, revision = pricing.read_state(db)
    now = utcnow()
    books = []
    for b in db.scalars(select(PriceBook).order_by(PriceBook.code)):
        vs = pricing.versions_of(db, b.id)
        cur, why = pv.select_version(vs, now)
        books.append({"book_id": str(b.id), "code": b.code, "currency": b.currency,
                      "effective_version_id": None if cur is None else cur["version_id"], "reason": why,
                      "versions": len(vs), "active_versions": sum(1 for v in vs if v["status"] == V_ACTIVE)})  # fmt: skip
    return {"epoch": str(epoch), "revision": revision, "books": books}


@router.get("/preview")
def preview(
    enterprise_id: uuid.UUID,
    product_id: uuid.UUID,
    channel: str = Query("sms", pattern="^(sms|email)$"),
    number: str = Query("", max_length=20),
    operator: str = Query("", max_length=8),
    at: datetime | None = None,
    db: Session = Depends(get_db),
    _: CentralUser = Depends(READ),
):
    """Çmimi që do të zgjidhej (njësi, pa segmente) — i njëjti kërkim si snapshot-i; fail-closed ⇒ 409."""
    when = at or utcnow()
    if at is not None and at.tzinfo is None:
        raise Invalid("`at` must be timezone-aware")
    a = pricing.assignment_at(db, enterprise_id, product_id, when)
    if a is None:
        raise pricing.NoPrice("no price assignment is effective for this enterprise/product")
    q = pricing.lookup(db, a.price_book_id, channel, number, when, operator)
    return {"book_id": str(q.book_id), "version_id": str(q.version_id), "rule_id": str(q.rule_id),
            "currency": q.currency, "unit_price": pv.format_price(q.unit_price)}  # fmt: skip


@router.get("/readiness")
def readiness(db: Session = Depends(get_db), _: CentralUser = Depends(READ)):
    checks = financial_ops.pricing_checks(db, utcnow())
    return {"checks": [{"name": c.name, "level": c.level, "reason": c.reason} for c in checks],
            "ok": all(c.level != financial_ops.FAIL for c in checks)}  # fmt: skip
