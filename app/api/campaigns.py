from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.contacts import owner_for
from app.core.db import get_db
from app.core.security import Principal, require
from app.models.campaigns import Campaign, CampaignRecipient, RecipientStatus
from app.services import campaigns as svc
from app.services.audit import audit
from app.services.wallet import WalletError

router = APIRouter(prefix="/v1/campaigns")
_STATUS = {"not_found": 404, "conflict": 409, "account_disabled": 403}


def _run(db: Session, fn):
    try:
        out = fn()
        db.commit()
        return out
    except WalletError as e:
        db.rollback()
        raise HTTPException(_STATUS.get(e.code, 422), {"code": e.code, "message": str(e)}) from e


class CampaignIn(BaseModel):
    owner_ref: str | None = None
    name: str = Field(min_length=1, max_length=80)
    list_id: int
    sender: str = Field(min_length=1, max_length=16)
    text: str | None = Field(default=None, max_length=1600)
    template_id: int | None = None
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
        "id": c.id, "name": c.name, "status": c.status.value, "list_id": c.list_id,
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
    owner = owner_for(p, body.owner_ref)
    fields = body.model_dump(exclude={"owner_ref"})

    def go():
        c = svc.create(db, owner, created_by=p.actor, **fields)
        audit(db, p, "campaign.create", "campaign", c.id, {"owner": owner, "list": c.list_id})
        return c

    return _out(_run(db, go))


@router.get("")
def list_campaigns(
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("campaigns:read")),
):
    owner = owner_for(p, owner_ref)
    rows = db.scalars(
        select(Campaign).where(Campaign.owner_ref == owner).order_by(Campaign.id.desc())
    )
    return [_out(c) for c in rows.fetchmany(200)]


@router.get("/{campaign_id}")
def get_campaign(
    campaign_id: int,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("campaigns:read")),
):
    owner = owner_for(p, owner_ref)
    c = _run(db, lambda: svc._get(db, owner, campaign_id))
    return {**_out(c), "stats": svc.stats(db, c)}


@router.get("/{campaign_id}/estimate")
def estimate(
    campaign_id: int,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("campaigns:read")),
):
    owner = owner_for(p, owner_ref)
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
        owner = owner_for(p, owner_ref)

        def go():
            c = fn(db, owner, campaign_id)
            audit(db, p, f"campaign.{name}", "campaign", c.id, {"owner": owner})
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
    owner = owner_for(p, owner_ref)

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
    owner = owner_for(p, owner_ref)
    _run(db, lambda: svc._get(db, owner, campaign_id))
    q = select(CampaignRecipient).where(
        CampaignRecipient.campaign_id == campaign_id, CampaignRecipient.id > after_id
    )
    if status:
        q = q.where(CampaignRecipient.status == status)
    rows = db.scalars(q.order_by(CampaignRecipient.id).limit(max(1, min(limit, 500))))
    return [
        {"id": r.id, "contact_id": r.contact_id, "status": r.status.value, "reason": r.reason,
         "message_id": r.message_id}
        for r in rows
    ]  # fmt: skip
