"""Raporte: përdorim ditor dhe eksport CSV (SMS/email) për llogarinë."""

import csv
import io
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.api.tenant import tenant
from app.core.db import SessionLocal, get_db
from app.core.scope import owned
from app.core.security import Principal, require
from app.models.email import Email, EmailStatus
from app.models.sending import Message, MessageStatus
from app.services.audit import audit

router = APIRouter(prefix="/v1/reports")

MAX_DAYS = 366
MAX_EXPORT_ROWS = 100_000


def _range(start: date | None, end: date | None) -> tuple[date, date]:
    end = end or datetime.now(UTC).date()
    start = start or end - timedelta(days=29)
    if start > end:
        raise HTTPException(422, {"code": "invalid", "message": "'from' must not be after 'to'"})
    if (end - start).days + 1 > MAX_DAYS:
        raise HTTPException(
            422, {"code": "invalid", "message": f"the range is limited to {MAX_DAYS} days"}
        )
    return start, end


def _bounds(start: date, end: date) -> tuple[datetime, datetime]:
    lo = datetime(start.year, start.month, start.day, tzinfo=UTC)
    return lo, datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)


@router.get("/usage")
def usage(
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("reports:read")),
):
    """Numërim ditor (UTC). Kostoja = ajo e tarifuar për mesazhet e dorëzuara."""
    owner = tenant(db, p, owner_ref)
    start, end = _range(from_, to)
    lo, hi = _bounds(start, end)
    days: dict[str, dict] = {}
    for i in range((end - start).days + 1):
        d = str(start + timedelta(days=i))
        days[d] = {
            "date": d,
            "sms": {"count": 0, "delivered": 0, "failed": 0, "segments": 0, "cost": "0"},
            "email": {"count": 0, "delivered": 0, "bounced": 0, "failed": 0},
        }
    day = func.date(Message.created_at)
    delivered = Message.status == MessageStatus.DELIVERED
    rows = db.execute(
        select(
            day,
            func.count(),
            func.sum(case((delivered, 1), else_=0)),
            func.sum(case((Message.status == MessageStatus.FAILED, 1), else_=0)),
            func.sum(Message.segments),
            func.sum(case((delivered, Message.total_price), else_=0)),
        )
        .where(owned(Message, owner), Message.created_at >= lo, Message.created_at < hi)
        .group_by(day)
    ).all()
    for d, n, ok, bad, segs, cost in rows:
        e = days[str(d)]["sms"]
        e.update(count=n, delivered=int(ok or 0), failed=int(bad or 0), segments=int(segs or 0),
                 cost=str(Decimal(cost or 0).normalize()))  # fmt: skip
    eday = func.date(Email.created_at)
    erows = db.execute(
        select(
            eday,
            func.count(),
            func.sum(case((Email.status == EmailStatus.DELIVERED, 1), else_=0)),
            func.sum(case((Email.status == EmailStatus.BOUNCED, 1), else_=0)),
            func.sum(case((Email.status == EmailStatus.FAILED, 1), else_=0)),
        )
        .where(owned(Email, owner), Email.created_at >= lo, Email.created_at < hi)
        .group_by(eday)
    ).all()
    for d, n, ok, bounced, bad in erows:
        days[str(d)]["email"].update(
            count=n, delivered=int(ok or 0), bounced=int(bounced or 0), failed=int(bad or 0)
        )
    series = list(days.values())
    totals = {
        "sms": {
            k: sum(x["sms"][k] for x in series)
            for k in ("count", "delivered", "failed", "segments")
        },
        "email": {
            k: sum(x["email"][k] for x in series)
            for k in ("count", "delivered", "bounced", "failed")
        },
    }
    totals["sms"]["cost"] = str(sum((Decimal(x["sms"]["cost"]) for x in series), Decimal(0)))
    return {"from": str(start), "to": str(end), "days": series, "totals": totals}


_PLAIN_NUMBER = re.compile(r"^\+?\d[\d.]*$")


def _safe(v) -> str:
    """Kundër CSV/formula injection: qelizat që fillojnë me = + - @ tab CR marrin një apostrof."""
    s = "" if v is None else (v.isoformat() if isinstance(v, datetime) else str(v))
    if _PLAIN_NUMBER.match(s):
        return s  # numër telefoni (+355...) ose numër i thjeshtë: pa formulë
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _csv(header: list[str], rows: Iterator[list]) -> Iterator[str]:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    yield "﻿" + buf.getvalue()  # BOM: Excel e hap si UTF-8 (ë, ç)
    for r in rows:
        buf.seek(0)
        buf.truncate()
        w.writerow([_safe(c) for c in r])
        yield buf.getvalue()


def _download(name: str, gen: Iterator[str]) -> StreamingResponse:
    return StreamingResponse(
        gen,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/messages.csv")
def export_messages(
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("reports:read")),
):
    owner = tenant(db, p, owner_ref)
    start, end = _range(from_, to)
    lo, hi = _bounds(start, end)
    audit(db, p, "report.export", "messages", owner.owner_ref, {"from": str(start), "to": str(end)})
    db.commit()
    stmt = (
        select(Message)
        .where(owned(Message, owner), Message.created_at >= lo, Message.created_at < hi)
        .order_by(Message.id)
        .limit(MAX_EXPORT_ROWS)
        .execution_options(yield_per=1000)
    )

    def rows():
        with SessionLocal() as s:  # sesioni i kërkesës mbyllet para se të dërgohet trupi
            for m in s.scalars(stmt):
                yield [m.public_id, m.created_at, f"+{m.destination}", m.sender, m.status.value,
                       m.category, m.segments, m.total_price, m.currency, m.error_code]  # fmt: skip

    header = ["id", "created_at", "to", "sender", "status", "category", "segments",
              "total_price", "currency", "error_code"]  # fmt: skip
    return _download(f"messages-{start}-{end}.csv", _csv(header, rows()))


@router.get("/emails.csv")
def export_emails(
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("reports:read")),
):
    owner = tenant(db, p, owner_ref)
    start, end = _range(from_, to)
    lo, hi = _bounds(start, end)
    audit(db, p, "report.export", "emails", owner.owner_ref, {"from": str(start), "to": str(end)})
    db.commit()
    stmt = (
        select(Email)
        .where(owned(Email, owner), Email.created_at >= lo, Email.created_at < hi)
        .order_by(Email.id)
        .limit(MAX_EXPORT_ROWS)
        .execution_options(yield_per=1000)
    )

    def rows():
        with SessionLocal() as s:
            for e in s.scalars(stmt):
                yield [e.public_id, e.created_at, e.to_email, e.from_email, e.subject,
                       e.status.value, e.category, e.error_code]  # fmt: skip

    header = ["id", "created_at", "to", "from", "subject", "status", "category", "error_code"]
    return _download(f"emails-{start}-{end}.csv", _csv(header, rows()))
