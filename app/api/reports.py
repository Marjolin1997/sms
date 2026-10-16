"""Raporte: përdorim ditor dhe eksport CSV (SMS/email) për llogarinë."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.api.tenant import tenant
from app.core.db import SessionLocal, get_db
from app.core.scope import owned
from app.core.security import Principal, require
from app.models.admin import AuditLog
from app.models.campaigns import Campaign, CampaignRecipient, CampaignStatus
from app.models.contacts import Contact
from app.models.email import Email, EmailStatus
from app.models.sending import Message, MessageStatus
from app.services import report_views as rv
from app.services.audit import audit
from app.services.csv_export import csv_stream as _csv
from app.services.csv_export import download, require_bounded, safe_cell

_download = download  # emrat e vjetër (testet/importet ekzistuese)
_safe = safe_cell  # kompat: app.api.reports._safe

router = APIRouter(prefix="/v1/reports")

MAX_DAYS = 366


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
    return _usage(db, owner, start, end)


def _usage(db: Session, owner, start: date, end: date) -> dict:
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


def _msg_filters(stmt, owner, lo, hi, status, campaign_id, sender):
    stmt = stmt.where(owned(Message, owner), Message.created_at >= lo, Message.created_at < hi)
    if status:
        stmt = stmt.where(
            Message.status.in_([MessageStatus.SENDING, MessageStatus.UNKNOWN])
            if status == "sending"
            else Message.status == MessageStatus(status)
        )
    if sender:
        stmt = stmt.where(Message.sender == sender)
    if campaign_id is not None:
        stmt = stmt.join(CampaignRecipient, CampaignRecipient.message_id == Message.id).where(
            CampaignRecipient.campaign_id == campaign_id
        )
    return stmt


def _own_campaign(db: Session, owner, campaign_id: int | None) -> None:
    """Filtri `campaign_id` i huaj ⇒ 404 (jo rezultat bosh që zbulon ekzistencën)."""
    if campaign_id is not None and not db.scalar(
        select(Campaign.id).where(Campaign.id == campaign_id, owned(Campaign, owner))
    ):
        raise HTTPException(404, {"code": "not_found", "message": "campaign not found"})


@router.get("/messages.csv")
def export_messages(
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    status: str | None = Query(default=None, pattern="^(queued|sending|sent|delivered|failed)$"),
    campaign_id: int | None = None,
    sender: str | None = Query(default=None, max_length=16),
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("reports:read")),
):
    owner = tenant(db, p, owner_ref)
    start, end = _range(from_, to)
    lo, hi = _bounds(start, end)
    _own_campaign(db, owner, campaign_id)
    stmt = _msg_filters(select(Message), owner, lo, hi, status, campaign_id, sender)
    require_bounded(db, stmt.with_only_columns(Message.id))
    audit(db, p, "report.export", "messages", owner.owner_ref,
          {"from": str(start), "to": str(end), "owner": owner.owner_ref})  # fmt: skip
    db.commit()
    stmt = stmt.order_by(Message.id).execution_options(yield_per=1000)

    def rows():
        with SessionLocal() as s:  # sesioni i kërkesës mbyllet para se të dërgohet trupi
            for m in s.scalars(stmt):
                yield [m.public_id, m.created_at, f"+{m.destination}", m.sender, rv.public(m.status),
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
    where = (owned(Email, owner), Email.created_at >= lo, Email.created_at < hi)
    require_bounded(db, select(Email.id).where(*where))
    audit(db, p, "report.export", "emails", owner.owner_ref,
          {"from": str(start), "to": str(end), "owner": owner.owner_ref})  # fmt: skip
    db.commit()
    stmt = select(Email).where(*where).order_by(Email.id).execution_options(yield_per=1000)

    def rows():
        with SessionLocal() as s:
            for e in s.scalars(stmt):
                yield [e.public_id, e.created_at, e.to_email, e.from_email, e.subject,
                       rv.public(e.status), e.category, e.error_code]  # fmt: skip

    header = ["id", "created_at", "to", "from", "subject", "status", "category", "error_code"]
    return _download(f"emails-{start}-{end}.csv", _csv(header, rows()))


# --- M11-G: raporte me agregate nga serveri (vetëm-lexim) -------------------------------------------------------------


@router.get("/overview")
def overview(
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("reports:read")),
):
    """Përmbledhje e periudhës (ditë UTC, të dyja skajet përfshihen): mesazhe sipas statusit PUBLIK, campaign-e,
    dështimet kryesore, 5 campaign-et e fundit. `delivery_rate` = delivered ÷ (delivered + failed), në %."""
    owner = tenant(db, p, owner_ref)
    start, end = _range(from_, to)
    lo, hi = _bounds(start, end)
    sms = rv.message_status_counts(db, owner, lo, hi)
    rows, truncated, _ = rv.failure_rows(db, owner, lo, hi)
    recent = list(db.scalars(rv.campaign_stmt(owner, lo, hi).order_by(Campaign.id.desc()).limit(5)))
    return {
        "from": str(start), "to": str(end), "timezone": "UTC",
        "messages": {"total": sum(sms.values()), "by_status": sms,
                     "delivery_rate": rv.delivery_rate(sms)},
        "emails": {"by_status": rv.email_status_counts(db, owner, lo, hi)},
        "campaigns": {"by_status": rv.campaign_status_counts(db, owner, lo, hi)},
        "top_failures": rows[:5], "failures_truncated": truncated,
        "recent_campaigns": rv.campaign_report_rows(db, recent),
    }  # fmt: skip


@router.get("/campaigns")
def campaign_report(
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    status: CampaignStatus | None = None,
    q: str | None = Query(default=None, max_length=80),
    before_id: int | None = None,
    limit: int = Query(default=25, ge=1, le=201),
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("reports:read")),
):
    """Campaign-et e krijuara në periudhë (id DESC, keyset `before_id`) me numëruesit e serverit për faqen."""
    owner = tenant(db, p, owner_ref)
    start, end = _range(from_, to)
    lo, hi = _bounds(start, end)
    stmt = rv.campaign_stmt(owner, lo, hi, status, q)
    if before_id:
        stmt = stmt.where(Campaign.id < before_id)
    return rv.campaign_report_rows(
        db, list(db.scalars(stmt.order_by(Campaign.id.desc()).limit(limit)))
    )


@router.get("/campaigns.csv")
def export_campaigns(
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    status: CampaignStatus | None = None,
    q: str | None = Query(default=None, max_length=80),
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("reports:read")),
):
    owner = tenant(db, p, owner_ref)
    start, end = _range(from_, to)
    lo, hi = _bounds(start, end)
    stmt = rv.campaign_stmt(owner, lo, hi, status, q)
    require_bounded(db, stmt.with_only_columns(Campaign.id), 20_000)
    audit(db, p, "report.export", "campaigns", owner.owner_ref,
          {"from": str(start), "to": str(end), "owner": owner.owner_ref})  # fmt: skip
    db.commit()
    stmt = stmt.order_by(Campaign.id)

    def rows():
        with SessionLocal() as s:
            cur = s.scalars(stmt).all()
            for i in range(0, len(cur), 200):  # agregatet me grupe 200 ⇒ pyetje të kufizuara
                for r in rv.campaign_report_rows(s, cur[i : i + 200]):
                    c = r["message_counts"]
                    yield [r["id"], r["name"], r["channel"], r["status"], r["created_at"],
                           r["scheduled_at"], r["started_at"], r["completed_at"],
                           r["recipients_total"], c.get("queued", 0), c.get("sending", 0),
                           c.get("sent", 0), c.get("delivered", 0),
                           c.get("failed", 0) + c.get("bounced", 0),
                           "" if r["delivery_rate"] is None else r["delivery_rate"]]  # fmt: skip

    header = ["id", "name", "channel", "status", "created_at", "scheduled_at", "started_at",
              "completed_at", "recipients", "queued", "sending", "sent", "delivered", "failed",
              "delivery_rate_percent"]  # fmt: skip
    return _download(f"campaigns-{start}-{end}.csv", _csv(header, rows()))


@router.get("/campaigns/{campaign_id}/recipients.csv")
def export_campaign_recipients(
    campaign_id: int,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("reports:read")),
):
    owner = tenant(db, p, owner_ref)
    _own_campaign(db, owner, campaign_id)
    where = (CampaignRecipient.campaign_id == campaign_id,)
    require_bounded(db, select(CampaignRecipient.id).where(*where))
    audit(db, p, "report.export", "campaign_recipients", campaign_id,
          {"owner": owner.owner_ref})  # fmt: skip
    db.commit()
    stmt = (
        select(
            CampaignRecipient, Contact.phone, Message.status, Message.public_id, Message.error_code
        )
        .join(Contact, Contact.id == CampaignRecipient.contact_id, isouter=True)
        .join(Message, Message.id == CampaignRecipient.message_id, isouter=True)
        .where(*where)
        .order_by(CampaignRecipient.id)
        .execution_options(yield_per=1000)
    )

    def rows():
        with SessionLocal() as s:
            for r, phone, mst, mid, err in s.execute(stmt):
                yield [r.id, f"+{phone}" if phone else r.address, r.status.value,
                       "" if mst is None else rv.public(mst), mid or "", err or ""]  # fmt: skip

    header = [
        "recipient_id",
        "to",
        "recipient_status",
        "message_status",
        "message_id",
        "error_code",
    ]
    return _download(f"campaign-{campaign_id}-recipients.csv", _csv(header, rows()))


@router.get("/failures")
def failures(
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    campaign_id: int | None = None,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("reports:read")),
):
    """(status, error_code) → numërim, nga serveri. Kategoria është paraqitje e `error_code` te UI; kodi i panjohur mbetet i dukshëm."""
    owner = tenant(db, p, owner_ref)
    start, end = _range(from_, to)
    lo, hi = _bounds(start, end)
    _own_campaign(db, owner, campaign_id)
    rows, truncated, by_status = rv.failure_rows(db, owner, lo, hi, campaign_id)
    return {
        "from": str(start), "to": str(end), "timezone": "UTC", "campaign_id": campaign_id,
        "total_messages": sum(by_status.values()), "by_status": by_status,
        "rows": rows, "truncated": truncated,
    }  # fmt: skip


@router.get("/usage.csv")
def export_usage(
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("reports:read")),
):
    owner = tenant(db, p, owner_ref)
    start, end = _range(from_, to)
    data = _usage(db, owner, start, end)
    audit(db, p, "report.export", "usage", owner.owner_ref,
          {"from": str(start), "to": str(end), "owner": owner.owner_ref})  # fmt: skip
    db.commit()
    header = ["date_utc", "sms", "sms_delivered", "sms_failed", "sms_segments", "sms_cost_delivered",
              "email", "email_delivered", "email_bounced", "email_failed"]  # fmt: skip
    return _download(f"usage-{start}-{end}.csv", _csv(header, rv.usage_csv_rows(data["days"])))


@router.get("/audit")
def audit_list(
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    action: str | None = Query(default=None, max_length=48),
    target_type: str | None = Query(default=None, max_length=32),
    before_id: int | None = None,
    limit: int = Query(default=50, ge=1, le=201),
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("reports:read")),
):
    """Audit i tenant-it (vetëm rreshtat me `owner_ref`-in e tij; histori para 0034 s'shfaqet). Aktori: you/staff/system."""
    owner = tenant(db, p, owner_ref)
    start, end = _range(from_, to)
    lo, hi = _bounds(start, end)
    stmt = rv.audit_stmt(owner, lo, hi, action, target_type)
    if before_id:
        stmt = stmt.where(AuditLog.id < before_id)
    return [rv.audit_item(a) for a in db.scalars(stmt.order_by(AuditLog.id.desc()).limit(limit))]


@router.get("/audit/facets")
def audit_facets(
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("reports:read")),
):
    return rv.audit_facets(db, tenant(db, p, owner_ref))


@router.get("/audit.csv")
def export_audit(
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    action: str | None = Query(default=None, max_length=48),
    target_type: str | None = Query(default=None, max_length=32),
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("reports:read")),
):
    owner = tenant(db, p, owner_ref)
    start, end = _range(from_, to)
    lo, hi = _bounds(start, end)
    stmt = rv.audit_stmt(owner, lo, hi, action, target_type)
    require_bounded(db, stmt.with_only_columns(AuditLog.id))
    audit(db, p, "report.export", "audit", owner.owner_ref,
          {"from": str(start), "to": str(end), "owner": owner.owner_ref})  # fmt: skip
    db.commit()
    stmt = stmt.order_by(AuditLog.id).execution_options(yield_per=1000)

    def rows():
        with SessionLocal() as s:
            for a in s.scalars(stmt):
                i = rv.audit_item(a)
                yield [i["id"], i["at"], i["by"], i["action"], i["target_type"], i["target_id"],
                       json.dumps(i["detail"], default=str, sort_keys=True, ensure_ascii=False) if i["detail"] else ""]  # fmt: skip

    header = ["id", "at_utc", "by", "action", "target_type", "target_id", "detail"]
    return _download(f"audit-{start}-{end}.csv", _csv(header, rows()))
