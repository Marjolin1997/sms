"""Analitika: KPI me krahasim me periudhën e mëparshme, seri ditore, ndarje dhe eksport CSV."""

import csv
import io
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.api.contacts import owner_for
from app.core.db import get_db
from app.core.security import Principal, require
from app.models.email import Email, EmailStatus
from app.models.sending import Message, MessageStatus

router = APIRouter(prefix="/v1/analytics")

MAX_DAYS = 366
EXPORT_LIMIT = 50_000
TOP_N = 8


def _bad(msg: str) -> HTTPException:
    return HTTPException(422, {"code": "invalid", "message": msg})


def _range(date_from: date | None, date_to: date | None) -> tuple[datetime, datetime]:
    """[nga, deri) në UTC; `date_to` përfshihet. Parazgjedhje: 30 ditët e fundit (me sot)."""
    end_day = date_to or datetime.now(UTC).date()
    start_day = date_from or end_day - timedelta(days=29)
    if start_day > end_day:
        raise _bad("date_from must not be after date_to")
    if (end_day - start_day).days + 1 > MAX_DAYS:
        raise _bad(f"the range can be at most {MAX_DAYS} days")
    start = datetime.combine(start_day, datetime.min.time(), UTC)
    return start, datetime.combine(end_day + timedelta(days=1), datetime.min.time(), UTC)


def _day(col, db: Session):
    """Dita UTC si tekst YYYY-MM-DD, e njëjtë në PostgreSQL dhe SQLite."""
    if db.get_bind().dialect.name == "postgresql":
        return func.to_char(func.timezone("UTC", col), "YYYY-MM-DD")
    return func.strftime("%Y-%m-%d", col)


def _rate(delivered: int, failed: int) -> float | None:
    """Delivery rate mbi mesazhet e mbyllura (në fluturim nuk numërohen); None pa të dhëna."""
    done = delivered + failed
    return round(delivered / done * 100, 1) if done else None


def _days(start: datetime, end: datetime) -> list[str]:
    n = (end - start).days
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


class _Channel:
    """Çfarë ndryshon mes SMS dhe email: modeli, statuset dhe kolonat e disponueshme."""

    def __init__(self, kind: str):
        sms = kind == "sms"
        self.kind = kind
        self.model = Message if sms else Email
        self.delivered = {MessageStatus.DELIVERED if sms else EmailStatus.DELIVERED}
        self.failed = (
            {MessageStatus.FAILED}
            if sms
            else {EmailStatus.FAILED, EmailStatus.BOUNCED, EmailStatus.COMPLAINED}
        )
        self.in_flight = (
            {MessageStatus.QUEUED, MessageStatus.SENDING, MessageStatus.SENT}
            if sms
            else {EmailStatus.QUEUED, EmailStatus.SENDING, EmailStatus.SENT}
        )

    def count(self, statuses):
        m = self.model
        return func.coalesce(func.sum(case((m.status.in_(statuses), 1), else_=0)), 0)


def _totals(db: Session, ch: _Channel, owner: str, start: datetime, end: datetime) -> dict:
    m = ch.model
    row = db.execute(
        select(
            func.count(m.id),
            ch.count(ch.delivered),
            ch.count(ch.failed),
            ch.count(ch.in_flight),
        ).where(m.owner_ref == owner, m.created_at >= start, m.created_at < end)
    ).one()
    total, delivered, failed, in_flight = (int(x or 0) for x in row)
    out = {
        "total": total,
        "delivered": delivered,
        "failed": failed,
        "in_flight": in_flight,
        "delivery_rate": _rate(delivered, failed),
    }
    if ch.kind == "sms":
        seg = db.scalar(
            select(func.coalesce(func.sum(Message.segments), 0)).where(
                Message.owner_ref == owner, Message.created_at >= start, Message.created_at < end
            )
        )
        # Faturohet vetëm ajo që u dorëzua (rezervimi i dështuarve lirohet).
        spend = db.execute(
            select(Message.currency, func.sum(Message.total_price))
            .where(
                Message.owner_ref == owner,
                Message.created_at >= start,
                Message.created_at < end,
                Message.status == MessageStatus.DELIVERED,
            )
            .group_by(Message.currency)
            .order_by(Message.currency)
        ).all()
        out["segments"] = int(seg or 0)
        out["spend"] = [{"currency": c, "amount": str(Decimal(a))} for c, a in spend]
    return out


def _series(db: Session, ch: _Channel, owner: str, start: datetime, end: datetime) -> list[dict]:
    m = ch.model
    day = _day(m.created_at, db).label("d")
    rows = db.execute(
        select(day, func.count(m.id), ch.count(ch.delivered), ch.count(ch.failed))
        .where(m.owner_ref == owner, m.created_at >= start, m.created_at < end)
        .group_by(day)
    ).all()
    by_day = {d: (int(t), int(ok), int(bad)) for d, t, ok, bad in rows}
    # Ditët pa aktivitet shfaqen me 0, që grafiku të mos "kapërcejë" data.
    return [
        {
            "date": d,
            **dict(zip(("total", "delivered", "failed"), by_day.get(d, (0, 0, 0)), strict=True)),
        }
        for d in _days(start, end)
    ]


def _breakdown(db: Session, ch: _Channel, owner: str, start, end, col, only_failed=False):
    m = ch.model
    stmt = select(col, func.count(m.id), ch.count(ch.delivered), ch.count(ch.failed)).where(
        m.owner_ref == owner, m.created_at >= start, m.created_at < end, col.is_not(None)
    )
    if only_failed:
        stmt = stmt.where(m.status.in_(ch.failed))
    rows = db.execute(stmt.group_by(col).order_by(func.count(m.id).desc(), col).limit(TOP_N)).all()
    return [
        {"key": k, "total": int(t), "delivered": int(ok), "failed": int(bad),
         "delivery_rate": _rate(int(ok), int(bad))}
        for k, t, ok, bad in rows
    ]  # fmt: skip


@router.get("/overview")
def overview(
    channel: str = "sms",
    date_from: date | None = None,
    date_to: date | None = None,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("messages:read")),
):
    """KPI, krahasim me periudhën e mëparshme me të njëjtën gjatësi, seri ditore dhe ndarje."""
    if channel not in ("sms", "email"):
        raise _bad("channel must be 'sms' or 'email'")
    owner = owner_for(p, owner_ref)
    start, end = _range(date_from, date_to)
    span = end - start
    ch = _Channel(channel)
    m = ch.model
    out = {
        "channel": channel,
        "range": {
            "from": start.date().isoformat(),
            "to": (end - timedelta(days=1)).date().isoformat(),
        },
        "current": _totals(db, ch, owner, start, end),
        "previous": _totals(db, ch, owner, start - span, start),
        "series": _series(db, ch, owner, start, end),
        "top_errors": _breakdown(db, ch, owner, start, end, m.error_code, only_failed=True),
        "by_category": _breakdown(db, ch, owner, start, end, m.category),
    }
    if channel == "sms":
        out["by_country"] = _breakdown(db, ch, owner, start, end, m.country)
        out["by_sender"] = _breakdown(db, ch, owner, start, end, m.sender)
    return out


def _safe(v) -> str:
    """Kundër CSV/formula injection kur skedari hapet në Excel/Sheets."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


@router.get("/export.csv")
def export_csv(
    channel: str = "sms",
    date_from: date | None = None,
    date_to: date | None = None,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("messages:read")),
):
    """Mesazhet e periudhës si CSV (pa tekst). Deri në 50 000 rreshta, më të rejat së pari."""
    if channel not in ("sms", "email"):
        raise _bad("channel must be 'sms' or 'email'")
    owner = owner_for(p, owner_ref)
    start, end = _range(date_from, date_to)
    ch = _Channel(channel)
    m = ch.model
    buf = io.StringIO()
    w = csv.writer(buf)
    if channel == "sms":
        w.writerow(["id", "created_at", "to", "sender", "country", "category", "status",
                    "segments", "cost", "currency", "error_code"])  # fmt: skip
        cols = [m.public_id, m.created_at, m.destination, m.sender, m.country, m.category,
                m.status, m.segments, m.total_price, m.currency, m.error_code]  # fmt: skip
    else:
        w.writerow(["id", "created_at", "to", "subject", "category", "status", "error_code"])
        cols = [
            m.public_id,
            m.created_at,
            m.to_email,
            m.subject,
            m.category,
            m.status,
            m.error_code,
        ]
    rows = db.execute(
        select(*cols)
        .where(m.owner_ref == owner, m.created_at >= start, m.created_at < end)
        .order_by(m.id.desc())
        .limit(EXPORT_LIMIT)
    )
    for r in rows:
        w.writerow(
            _safe(v.isoformat() if isinstance(v, datetime) else getattr(v, "value", v)) for v in r
        )
    name = f"{channel}-{start.date()}_{(end - timedelta(days=1)).date()}.csv"
    return Response(
        "﻿" + buf.getvalue(),  # BOM: Excel e lexon UTF-8 saktë
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
