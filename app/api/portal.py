"""Portal vetë-shërbyes (API): webhooks, event log, çelësa API, pasqyrë përdorimi."""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.contacts import owner_for
from app.core.config import settings
from app.core.db import get_db
from app.core.security import ROLE_PERMS, Principal, current_principal, require
from app.models.admin import ApiKey
from app.models.campaigns import Campaign
from app.models.email import Email
from app.models.events import (
    DeliveryStatus,
    EndpointStatus,
    Event,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.models.sending import Message
from app.models.wallet import Wallet
from app.services import apikeys, twofactor, webhooks
from app.services import wallet as wallets
from app.services.audit import audit
from app.services.wallet import WalletError

router = APIRouter(prefix="/v1")
_STATUS = {"not_found": 404, "conflict": 409}


def _run(db: Session, fn):
    try:
        out = fn()
        db.commit()
        return out
    except WalletError as e:
        db.rollback()
        raise HTTPException(_STATUS.get(e.code, 422), {"code": e.code, "message": str(e)}) from e


@router.get("/me")
def me(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    """Identiteti i thirrësit: përdoret nga paneli për të treguar vetëm çka lejohet."""
    key = db.get(ApiKey, p.key_id) if p.key_id else None
    return {"actor": p.actor, "role": p.role, "owner_ref": p.owner_ref,
            "permissions": sorted(ROLE_PERMS.get(p.role, set())),
            "key_id": p.key_id, "two_factor": bool(key and key.totp_enabled),
            "two_factor_required": settings.require_staff_2fa and p.owner_ref is None
            and p.key_id is not None}  # fmt: skip


class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=10)


def _own_key_id(p: Principal) -> int:
    if p.key_id is None:
        raise HTTPException(
            400,
            {"code": "no_key", "message": "two-factor applies to API keys, not the bootstrap key"},
        )
    return p.key_id


@router.post("/me/2fa/enroll", status_code=201)
def enroll_2fa(db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    kid = _own_key_id(p)

    def go():
        secret, uri = twofactor.enroll(db, kid)
        audit(db, p, "2fa.enroll_started", "apikey", kid)
        return {"secret": secret, "otpauth_uri": uri}

    return _run(db, go)


@router.post("/me/2fa/confirm")
def confirm_2fa(
    body: CodeIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)
):
    kid = _own_key_id(p)

    def go():
        twofactor.confirm(db, kid, body.code)
        audit(db, p, "2fa.enabled", "apikey", kid)
        return {"two_factor": True}

    return _run(db, go)


# --- Webhook endpoints ---------------------------------------------------------------


class EndpointIn(BaseModel):
    owner_ref: str | None = None
    url: str = Field(max_length=2000)
    event_types: list[str] | None = Field(default=None, max_length=30)
    description: str | None = Field(default=None, max_length=120)


class EndpointPatch(BaseModel):
    url: str | None = Field(default=None, max_length=2000)
    event_types: list[str] | None = Field(default=None, max_length=30)
    enabled: bool | None = None
    description: str | None = Field(default=None, max_length=120)


def _ep_out(ep: WebhookEndpoint, secret: str | None = None) -> dict:
    out = {
        "id": ep.id, "url": ep.url, "event_types": ep.event_types, "status": ep.status.value,
        "disabled_reason": ep.disabled_reason, "description": ep.description,
        "consecutive_failures": ep.consecutive_failures,
    }  # fmt: skip
    if secret:
        out["secret"] = secret  # shfaqet vetëm një herë
    return out


@router.post("/webhooks/endpoints", status_code=201)
def create_endpoint(
    body: EndpointIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("webhooks:write")),
):
    owner = owner_for(p, body.owner_ref)

    def go():
        ep, secret = webhooks.create_endpoint(
            db, owner, body.url, body.event_types, body.description
        )
        audit(db, p, "webhook.create", "webhook", ep.id, {"owner": owner, "url": ep.url})
        return ep, secret

    ep, secret = _run(db, go)
    return _ep_out(ep, secret)


@router.get("/webhooks/endpoints")
def list_endpoints(
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("webhooks:read")),
):
    owner = owner_for(p, owner_ref)
    rows = db.scalars(
        select(WebhookEndpoint)
        .where(WebhookEndpoint.owner_ref == owner)
        .order_by(WebhookEndpoint.id)
    )
    return [_ep_out(ep) for ep in rows]


@router.patch("/webhooks/endpoints/{endpoint_id}")
def patch_endpoint(
    endpoint_id: int,
    body: EndpointPatch,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("webhooks:write")),
):
    owner = owner_for(p, owner_ref)
    fields = body.model_dump(exclude_unset=True)

    def go():
        ep = webhooks.update_endpoint(db, owner, endpoint_id, **fields)
        audit(db, p, "webhook.update", "webhook", ep.id, {"fields": sorted(fields)})
        return ep

    return _ep_out(_run(db, go))


@router.delete("/webhooks/endpoints/{endpoint_id}", status_code=204)
def delete_endpoint(
    endpoint_id: int,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("webhooks:write")),
):
    owner = owner_for(p, owner_ref)

    def go():
        webhooks.delete_endpoint(db, owner, endpoint_id)
        audit(db, p, "webhook.delete", "webhook", endpoint_id)

    _run(db, go)
    return Response(status_code=204)


@router.post("/webhooks/endpoints/{endpoint_id}/rotate-secret")
def rotate_secret(
    endpoint_id: int,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("webhooks:write")),
):
    owner = owner_for(p, owner_ref)

    def go():
        ep, secret = webhooks.rotate_secret(db, owner, endpoint_id)
        audit(db, p, "webhook.rotate_secret", "webhook", ep.id)
        return ep, secret

    ep, secret = _run(db, go)
    return _ep_out(ep, secret)


@router.post("/webhooks/endpoints/{endpoint_id}/test", status_code=202)
def test_endpoint(
    endpoint_id: int,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("webhooks:write")),
):
    owner = owner_for(p, owner_ref)
    ev = _run(db, lambda: webhooks.send_test(db, owner, endpoint_id))
    return {"event_id": f"evt_{ev.id}", "type": ev.type}


@router.get("/webhooks/deliveries")
def list_deliveries(
    endpoint_id: int | None = None,
    status: DeliveryStatus | None = None,
    after_id: int = 0,
    limit: int = 100,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("webhooks:read")),
):
    owner = owner_for(p, owner_ref)
    q = (
        select(WebhookDelivery, Event.type)
        .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
        .join(Event, Event.id == WebhookDelivery.event_id)
        .where(WebhookEndpoint.owner_ref == owner, WebhookDelivery.id > after_id)
    )
    if endpoint_id:
        q = q.where(WebhookDelivery.endpoint_id == endpoint_id)
    if status:
        q = q.where(WebhookDelivery.status == status)
    rows = db.execute(q.order_by(WebhookDelivery.id).limit(max(1, min(limit, 500))))
    return [
        {"id": d.id, "endpoint_id": d.endpoint_id, "event_id": f"evt_{d.event_id}", "type": t,
         "status": d.status.value, "attempts": d.attempts, "last_status_code": d.last_status_code,
         "last_error": d.last_error, "next_attempt_at": d.next_attempt_at}
        for d, t in rows
    ]  # fmt: skip


@router.post("/webhooks/deliveries/{delivery_id}/redeliver")
def redeliver(
    delivery_id: int,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("webhooks:write")),
):
    owner = owner_for(p, owner_ref)
    d = _run(db, lambda: webhooks.redeliver(db, owner, delivery_id))
    return {"id": d.id, "status": d.status.value}


# --- Event log (pull) ----------------------------------------------------------------


@router.get("/events")
def list_events(
    type: str | None = None,
    after_id: int = 0,
    limit: int = 100,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("events:read")),
):
    """Alternativë pull ndaj webhook-eve: kursori `after_id`, renditje rritëse."""
    owner = owner_for(p, owner_ref)
    q = select(Event).where(Event.owner_ref == owner, Event.id > after_id)
    if type:
        q = q.where(Event.type == type)
    rows = db.scalars(q.order_by(Event.id).limit(max(1, min(limit, 500))))
    return [
        {"id": f"evt_{e.id}", "cursor": e.id, "type": e.type, "created_at": e.created_at,
         "data": {"resource_type": e.resource_type, "resource_id": e.resource_id, **(e.data or {})}}
        for e in rows
    ]  # fmt: skip


# --- Çelësa API vetë-shërbyes ----------------------------------------------------------


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    expires_at: datetime | None = None
    allowed_cidrs: list[str] | None = Field(default=None, max_length=20)


class RotateIn(BaseModel):
    grace_minutes: int = Field(default=60, ge=0, le=1440)


def _key_out(k, secret: str | None = None) -> dict:
    out = {"id": k.id, "prefix": k.prefix, "name": k.name, "status": k.status.value,
           "expires_at": k.expires_at, "last_used_at": k.last_used_at,
           "allowed_cidrs": apikeys.cidrs_of(k)}  # fmt: skip
    if secret:
        out["key"] = secret
    return out


def _client_only(p: Principal) -> str:
    if not p.owner_ref:
        raise HTTPException(
            403, {"code": "forbidden", "message": "self-service keys are for client accounts"}
        )
    return p.owner_ref


@router.post("/portal/api-keys", status_code=201)
def create_own_key(
    body: KeyIn, db: Session = Depends(get_db), p: Principal = Depends(require("keys:self"))
):
    owner = _client_only(p)

    def go():
        k, full = apikeys.create_own_key(
            db, owner, body.name, p.actor, body.expires_at, body.allowed_cidrs
        )
        audit(db, p, "apikey.self_create", "apikey", k.id, {"owner": owner})
        return k, full

    k, full = _run(db, go)
    return _key_out(k, full)


@router.get("/portal/api-keys")
def list_own_keys(db: Session = Depends(get_db), p: Principal = Depends(require("keys:self"))):
    return [_key_out(k) for k in apikeys.list_own_keys(db, _client_only(p))]


@router.post("/portal/api-keys/{key_id}/rotate", status_code=201)
def rotate_own_key(
    key_id: int,
    body: RotateIn | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("keys:self")),
):
    owner = _client_only(p)
    grace = (body or RotateIn()).grace_minutes

    def go():
        old, new, full = apikeys.rotate_own_key(db, owner, key_id, p.actor, grace)
        audit(db, p, "apikey.self_rotate", "apikey", old.id, {"new": new.id, "owner": owner})
        return old, new, full

    old, new, full = _run(db, go)
    return {**_key_out(new, full), "replaces": _key_out(old)}


@router.post("/portal/api-keys/{key_id}/revoke")
def revoke_own_key(
    key_id: int, db: Session = Depends(get_db), p: Principal = Depends(require("keys:self"))
):
    owner = _client_only(p)

    def go():
        k = apikeys.revoke_own_key(db, owner, key_id)
        audit(db, p, "apikey.self_revoke", "apikey", k.id, {"owner": owner})
        return k

    return _key_out(_run(db, go))


# --- Pasqyrë ---------------------------------------------------------------------------


@router.get("/portal/overview")
def overview(
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("portal:read")),
):
    owner = owner_for(p, owner_ref)
    since = datetime.now(UTC) - timedelta(days=30)
    day = datetime.now(UTC) - timedelta(hours=24)

    def counts(model, status_col, *where):
        rows = db.execute(select(status_col, func.count()).where(*where).group_by(status_col))
        return {getattr(s, "value", s): n for s, n in rows}

    wallet_rows = []
    for w in db.scalars(select(Wallet).where(Wallet.owner_ref == owner).order_by(Wallet.id)):
        avail, held = wallets.balances(db, w.id)
        wallet_rows.append({"id": w.id, "currency": w.currency, "available": str(avail),
                            "held": str(held)})  # fmt: skip
    return {
        "wallets": wallet_rows,
        "sms_last_30d": counts(Message, Message.status, Message.owner_ref == owner,
                               Message.created_at >= since),  # fmt: skip
        "email_last_30d": counts(Email, Email.status, Email.owner_ref == owner,
                                 Email.created_at >= since),  # fmt: skip
        "campaigns": counts(Campaign, Campaign.status, Campaign.owner_ref == owner),
        "webhooks": {
            "active_endpoints": db.scalar(
                select(func.count()).select_from(WebhookEndpoint).where(
                    WebhookEndpoint.owner_ref == owner,
                    WebhookEndpoint.status == EndpointStatus.ACTIVE)
            ),
            "failed_deliveries_24h": db.scalar(
                select(func.count()).select_from(WebhookDelivery)
                .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
                .where(WebhookEndpoint.owner_ref == owner,
                       WebhookDelivery.status == DeliveryStatus.FAILED,
                       WebhookDelivery.created_at >= day)
            ),
        },
    }  # fmt: skip
