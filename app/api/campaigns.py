from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.tenant import tenant
from app.core.db import get_db
from app.core.errors import DomainError
from app.core.scope import owned
from app.core.security import Principal, require
from app.models.campaigns import Campaign, CampaignRecipient, CampaignStatus, RecipientStatus
from app.services import campaign_views as views
from app.services import campaigns as svc
from app.services.audit import audit

router = APIRouter(prefix="/v1/campaigns")
_STATUS = {
    "not_found": 404, "conflict": 409, "account_disabled": 403, "product_not_entitled": 403,
    "enterprise_suspended": 403, "product_suspended": 403,
}  # fmt: skip


def _run(db: Session, fn):
    try:
        out = fn()
        db.commit()
        return out
    except DomainError as e:
        db.rollback()
        raise HTTPException(_STATUS.get(e.code, 422), {"code": e.code, "message": str(e)}) from e


class CampaignIn(BaseModel):
    owner_ref: str | None = None
    name: str = Field(min_length=1, max_length=80)
    list_id: int
    sender: str = Field(default="", max_length=16)  # SMS: sender ID (i detyrueshëm)
    text: str | None = Field(default=None, max_length=100_000)  # SMS ≤ 1600 (kontrollohet)
    template_id: int | None = None
    channel: str = Field(default="sms", pattern="^(sms|email)$")
    subject: str | None = Field(default=None, max_length=200)
    html: str | None = Field(default=None, max_length=200_000)
    from_email: str | None = Field(default=None, max_length=254)
    from_name: str | None = Field(default=None, max_length=100)
    category: str = Field(default="marketing", pattern="^(marketing|transactional)$")
    max_cost: Decimal | None = Field(default=None, gt=0, max_digits=20, decimal_places=6)
    rate_per_minute: int = Field(default=300, ge=1, le=10_000)
    window_start_hour: int | None = Field(default=None, ge=0, le=23)
    window_end_hour: int | None = Field(default=None, ge=0, le=23)
    utc_offset_minutes: int = Field(default=0, ge=-840, le=840)


class ScheduleIn(BaseModel):
    scheduled_at: datetime | None = None  # bosh = tani


def _out(c: Campaign) -> dict:
    return {
        "id": c.id, "name": c.name, "channel": c.channel, "status": c.status.value,
        "list_id": c.list_id,
        "sender": c.sender, "category": c.category, "template_id": c.template_id,
        "max_cost": None if c.max_cost is None else str(c.max_cost),
        "rate_per_minute": c.rate_per_minute,
        "scheduled_at": c.scheduled_at, "started_at": c.started_at,
        "completed_at": c.completed_at, "pause_reason": c.pause_reason,
    }  # fmt: skip


@router.post("", status_code=201)
def create(
    body: CampaignIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("campaigns:write")),
):
    owner = tenant(db, p, body.owner_ref, write=True)
    fields = body.model_dump(exclude={"owner_ref"})

    def go():
        c = svc.create(db, owner, created_by=p.actor, **fields)
        audit(
            db,
            p,
            "campaign.create",
            "campaign",
            c.id,
            {"owner": owner.owner_ref, "list": c.list_id},
        )
        return c

    return _out(_run(db, go))


@router.get("")
def list_campaigns(
    status: CampaignStatus | None = None,
    q: str | None = None,
    before_id: int | None = None,
    limit: int = 200,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("campaigns:read")),
):
    """Më të rejat së pari (id DESC); filtra `status`, `q` (emër), `before_id`.
    Çdo rresht ka numërues të serverit nga pyetje të grupuara për faqen."""
    owner = tenant(db, p, owner_ref)
    stmt = select(Campaign).where(owned(Campaign, owner))
    if status:
        stmt = stmt.where(Campaign.status == status)
    if q:
        esc = q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(func.lower(Campaign.name).like(f"%{esc}%", escape="\\"))
    if before_id:
        stmt = stmt.where(Campaign.id < before_id)
    rows = list(db.scalars(stmt.order_by(Campaign.id.desc()).limit(max(1, min(limit, 201)))))
    sums = views.page_summaries(db, rows)
    return [{**_out(c), **sums.get(c.id, {})} for c in rows]


@router.get("/{campaign_id}")
def get_campaign(
    campaign_id: int,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("campaigns:read")),
):
    owner = tenant(db, p, owner_ref)
    c = _run(db, lambda: svc._get(db, owner, campaign_id))
    return {
        **_out(c), "stats": svc.stats(db, c), "created_at": c.created_at, "text": c.text,
        "subject": c.subject, "from_email": c.from_email,
        "list_name": views.list_name(db, c.list_id),
        "window_start_hour": c.window_start_hour, "window_end_hour": c.window_end_hour,
        "utc_offset_minutes": c.utc_offset_minutes,
    }  # fmt: skip


@router.get("/{campaign_id}/estimate")
def estimate(
    campaign_id: int,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("campaigns:read")),
):
    owner = tenant(db, p, owner_ref)
    e = _run(db, lambda: svc.estimate(db, owner, campaign_id))
    return {"recipients": e.recipients, "excluded": e.excluded, "segments": e.segments,
            "total": str(e.total), "currency": e.currency}  # fmt: skip


def _transition(name: str, fn):
    def endpoint(
        campaign_id: int,
        owner_ref: str | None = None,
        db: Session = Depends(get_db),
        p: Principal = Depends(require("campaigns:write")),
    ):
        owner = tenant(db, p, owner_ref)

        def go():
            c = fn(db, owner, campaign_id)
            audit(db, p, f"campaign.{name}", "campaign", c.id, {"owner": owner.owner_ref})
            return c

        return _out(_run(db, go))

    return endpoint


for _name, _fn in (("pause", svc.pause), ("resume", svc.resume), ("cancel", svc.cancel)):
    router.add_api_route(f"/{{campaign_id}}/{_name}", _transition(_name, _fn), methods=["POST"])


@router.post("/{campaign_id}/schedule")
def schedule(
    campaign_id: int,
    body: ScheduleIn,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("campaigns:write")),
):
    owner = tenant(db, p, owner_ref)

    def go():
        c = svc.schedule(db, owner, campaign_id, body.scheduled_at)
        audit(db, p, "campaign.schedule", "campaign", c.id, {"at": c.scheduled_at})
        return c

    return _out(_run(db, go))


@router.get("/{campaign_id}/recipients")
def recipients(
    campaign_id: int,
    status: RecipientStatus | None = None,
    after_id: int = 0,
    limit: int = 100,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("campaigns:read")),
):
    owner = tenant(db, p, owner_ref)
    _run(db, lambda: svc._get(db, owner, campaign_id))
    q = select(CampaignRecipient).where(
        CampaignRecipient.campaign_id == campaign_id, CampaignRecipient.id > after_id
    )
    if status:
        q = q.where(CampaignRecipient.status == status)
    rows = list(db.scalars(q.order_by(CampaignRecipient.id).limit(max(1, min(limit, 500)))))
    return views.recipient_rows(db, rows)


@router.get("/{campaign_id}/activity")
def activity(
    campaign_id: int,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("campaigns:read")),
):
    """Ngjarjet e audituara të campaign-it (vetëm ato që ekzistojnë). Aktori s'ekspozohet."""
    owner = tenant(db, p, owner_ref)
    _run(db, lambda: svc._get(db, owner, campaign_id))
    return views.activity(db, campaign_id)
