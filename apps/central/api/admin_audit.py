"""M11-G: audit READ-ONLY për konsolën Central (admin + operator). Pa shkrime, pa audit për çdo lexim.

`GET /admin/audit` (stafi/sistemi), `GET /admin/audit/sender-decisions` (historiku i vendimeve), eksporte CSV të kufizuara
me të njëjtat filtra. Kufiri i eksportit është i shprehur (422 `export_too_large`), jo prerje e heshtur."""

import csv
import io
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.central.api.admin_common import page
from apps.central.api.deps import get_db, require_role
from apps.central.models.user import CentralUser, Role
from apps.central.services import audit_reports as rep

router = APIRouter(prefix="/admin")
READ = require_role(Role.ADMIN, Role.OPERATOR)
MAX_PAGE = 200
MAX_EXPORT_ROWS = 50_000
MAX_DAYS = 366
Scope = Literal["all", "sender", "commercial", "identity"]


def _bounds(start: date | None, end: date | None) -> tuple[datetime | None, datetime | None]:
    if start and end and start > end:
        raise HTTPException(422, {"code": "invalid", "message": "'from' must not be after 'to'"})
    if start and end and (end - start).days + 1 > MAX_DAYS:
        raise HTTPException(
            422, {"code": "invalid", "message": f"the range is limited to {MAX_DAYS} days"}
        )
    lo = datetime(start.year, start.month, start.day, tzinfo=UTC) if start else None
    hi = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1) if end else None
    return lo, hi


def _cell(v) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _bounded(db: Session, stmt) -> None:
    n = db.scalar(
        select(func.count()).select_from(stmt.order_by(None).limit(MAX_EXPORT_ROWS + 1).subquery())
    )
    if (n or 0) > MAX_EXPORT_ROWS:
        raise HTTPException(
            422,
            {"code": "export_too_large",
             "message": f"the export has more than {MAX_EXPORT_ROWS} rows; narrow the filters or the dates"},
        )  # fmt: skip


def _csv(name: str, header: list[str], rows: Iterator[list]) -> StreamingResponse:
    def gen():
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\r\n")
        w.writerow(header)
        yield "\ufeff" + buf.getvalue()  # BOM UTF-8
        buf.seek(0)
        buf.truncate()
        for n, r in enumerate(rows, 1):
            w.writerow([_cell(c) for c in r])
            if n % 500 == 0:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        if buf.tell():
            yield buf.getvalue()

    return StreamingResponse(
        gen(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )  # fmt: skip


def _audit_filters(scope, action, resource_type, actor_kind, actor_id, start, end):
    lo, hi = _bounds(start, end)
    return rep.audit_stmt(scope, action, resource_type, actor_kind, actor_id, lo, hi)


@router.get("/audit")
def list_audit(
    scope: Scope = "all",
    action: str | None = Query(None, max_length=48),
    resource_type: str | None = Query(None, max_length=32),
    actor_kind: Literal["user", "system"] | None = None,
    actor_id: uuid.UUID | None = None,
    from_: date | None = Query(None, alias="from"),
    to: date | None = None,
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0, le=100_000),
    db: Session = Depends(get_db),
    _: CentralUser = Depends(READ),
):
    stmt = _audit_filters(scope, action, resource_type, actor_kind, actor_id, from_, to)
    rows = db.execute(stmt.limit(limit + 1).offset(offset)).all()
    out = page(rows, limit, offset, lambda r: rep.audit_item(r[0], r[1]))
    db.rollback()
    return out


@router.get("/audit/facets")
def audit_facets(
    scope: Scope = "all", db: Session = Depends(get_db), _: CentralUser = Depends(READ)
):
    out = rep.facets(db, scope)
    db.rollback()
    return out


@router.get("/audit.csv")
def export_audit(
    scope: Scope = "all",
    action: str | None = Query(None, max_length=48),
    resource_type: str | None = Query(None, max_length=32),
    actor_kind: Literal["user", "system"] | None = None,
    actor_id: uuid.UUID | None = None,
    from_: date | None = Query(None, alias="from"),
    to: date | None = None,
    db: Session = Depends(get_db),
    _: CentralUser = Depends(READ),
):
    stmt = _audit_filters(scope, action, resource_type, actor_kind, actor_id, from_, to)
    _bounded(db, stmt)
    items = [rep.audit_item(a, e) for a, e in db.execute(stmt.limit(MAX_EXPORT_ROWS)).all()]
    db.rollback()
    return _csv(
        f"central-audit-{datetime.now(UTC):%Y%m%d}.csv",
        ["id", "at_utc", "actor_kind", "actor", "action", "resource_type", "resource_id", "detail"],
        (
            [
                i["id"],
                i["at"],
                i["actor_kind"],
                i["actor"],
                i["action"],
                i["resource_type"],
                i["resource_id"],
                json.dumps(i["detail"], default=str, sort_keys=True, ensure_ascii=False)
                if i["detail"]
                else "",
            ]  # fmt: skip
            for i in items
        ),
    )


def _decision_filters(decision, category, source, country, enterprise_id, registry_id, start, end):
    lo, hi = _bounds(start, end)
    return rep.decision_stmt(
        decision, category, source, country, enterprise_id, registry_id, lo, hi
    )


Decision = Literal["requested", "approved", "rejected", "revoked", "resubmitted"]
Category = Literal[
    "request", "manual", "resubmit", "policy_denied", "policy_auto_approved", "policy_revoked"
]


@router.get("/audit/sender-decisions")
def sender_decisions(
    decision: Decision | None = None,
    category: Category | None = None,
    source: Literal["admin", "enterprise", "import"] | None = None,
    country: str | None = Query(None, pattern="^[A-Za-z]{2}$"),
    enterprise_id: uuid.UUID | None = None,
    registry_id: uuid.UUID | None = None,
    from_: date | None = Query(None, alias="from"),
    to: date | None = None,
    limit: int = Query(50, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0, le=100_000),
    db: Session = Depends(get_db),
    _: CentralUser = Depends(READ),
):
    stmt = _decision_filters(
        decision, category, source, country, enterprise_id, registry_id, from_, to
    )
    rows = db.execute(stmt.limit(limit + 1).offset(offset)).all()
    out = page(rows, limit, offset, lambda r: rep.decision_item(*r))
    db.rollback()
    return out


@router.get("/audit/sender-decisions.csv")
def export_sender_decisions(
    decision: Decision | None = None,
    category: Category | None = None,
    source: Literal["admin", "enterprise", "import"] | None = None,
    country: str | None = Query(None, pattern="^[A-Za-z]{2}$"),
    enterprise_id: uuid.UUID | None = None,
    registry_id: uuid.UUID | None = None,
    from_: date | None = Query(None, alias="from"),
    to: date | None = None,
    db: Session = Depends(get_db),
    _: CentralUser = Depends(READ),
):
    stmt = _decision_filters(
        decision, category, source, country, enterprise_id, registry_id, from_, to
    )
    _bounded(db, stmt)
    items = [rep.decision_item(*r) for r in db.execute(stmt.limit(MAX_EXPORT_ROWS)).all()]
    db.rollback()
    return _csv(
        f"sender-decisions-{datetime.now(UTC):%Y%m%d}.csv",
        [
            "decided_at_utc",
            "enterprise",
            "sender",
            "country",
            "sender_kind",
            "decision",
            "from_status",
            "to_status",
            "category",
            "source",
            "actor",
            "policy_source",
            "policy_revision",
            "reason",
            "evidence_ref",
        ],
        (
            [
                i["decided_at"],
                i["enterprise"],
                i["sender"],
                i["country"],
                i["sender_kind"],
                i["decision"],
                i["from_status"],
                i["to_status"],
                i["category"],
                i["source"],
                i["actor"],
                i["policy_source"],
                i["policy_revision"],
                i["reason"],
                i["evidence_ref"],
            ]  # fmt: skip
            for i in items
        ),
    )
