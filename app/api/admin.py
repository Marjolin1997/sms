import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import DomainError
from app.core.security import Principal, require
from app.core.timeutil import as_utc
from app.models.admin import ApiKey, AuditLog, Switch
from app.models.sending import AccountPlan, DlrReceipt, Message, MessageStatus, Route
from app.services import apikeys, switches, twofactor
from app.services import emails as email_svc
from app.services import messages as msg_svc
from app.services.audit import audit

router = APIRouter(prefix="/v1/admin")
_STATUS = {"not_found": 404, "conflict": 409}


def _run(db: Session, fn):
    try:
        out = fn()
        db.commit()
        return out
    except DomainError as e:
        db.rollback()
        raise HTTPException(_STATUS.get(e.code, 422), {"code": e.code, "message": str(e)}) from e


# --- API keys -----------------------------------------------------------------


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    role: str
    owner_ref: str | None = Field(default=None, max_length=64)
    expires_at: datetime | None = None
    allowed_cidrs: list[str] | None = Field(default=None, max_length=20)


class RotateIn(BaseModel):
    grace_minutes: int = Field(default=60, ge=0, le=1440)


def _key_out(k: ApiKey, secret: str | None = None) -> dict:
    out = {
        "id": k.id, "prefix": k.prefix, "name": k.name, "role": k.role,
        "owner_ref": k.owner_ref, "status": k.status.value, "expires_at": k.expires_at,
        "last_used_at": k.last_used_at, "allowed_cidrs": apikeys.cidrs_of(k),
        "two_factor": k.totp_enabled,
    }  # fmt: skip
    if secret:
        out["key"] = secret  # shfaqet vetëm një herë
    return out


@router.post("/api-keys", status_code=201)
def create_key(
    body: KeyIn, db: Session = Depends(get_db), p: Principal = Depends(require("keys:manage"))
):
    def go():
        k, full = apikeys.create_key(
            db, body.name, body.role, body.owner_ref, p.actor, body.expires_at, body.allowed_cidrs
        )
        audit(db, p, "apikey.create", "apikey", k.id, {"role": k.role, "owner": k.owner_ref})
        return k, full

    k, full = _run(db, go)
    return _key_out(k, full)


@router.get("/api-keys")
def list_keys(db: Session = Depends(get_db), _: Principal = Depends(require("keys:manage"))):
    return [_key_out(k) for k in apikeys.list_keys(db)]


@router.post("/api-keys/{key_id}/rotate", status_code=201)
def rotate_key(
    key_id: int,
    body: RotateIn | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("keys:manage")),
):
    grace = (body or RotateIn()).grace_minutes

    def go():
        old, new, full = apikeys.rotate_key(db, key_id, p.actor, grace)
        audit(db, p, "apikey.rotate", "apikey", old.id, {"new": new.id, "grace_min": grace})
        return old, new, full

    old, new, full = _run(db, go)
    return {**_key_out(new, full), "replaces": _key_out(old)}


@router.post("/api-keys/{key_id}/reset-2fa")
def reset_2fa(
    key_id: int, db: Session = Depends(get_db), p: Principal = Depends(require("keys:manage"))
):
    def go():
        k = twofactor.reset(db, key_id)
        audit(db, p, "apikey.reset_2fa", "apikey", k.id)
        return k

    return _key_out(_run(db, go))


@router.post("/api-keys/{key_id}/revoke")
def revoke_key(
    key_id: int, db: Session = Depends(get_db), p: Principal = Depends(require("keys:manage"))
):
    def go():
        k = apikeys.revoke_key(db, key_id)
        audit(db, p, "apikey.revoke", "apikey", k.id)
        return k

    return _key_out(_run(db, go))


# --- Routes dhe plane ---------------------------------------------------------


class RouteIn(BaseModel):
    prefix: str = Field(pattern=r"^[1-9]\d{0,15}$")
    country: str = Field(pattern="^[A-Za-z]{2}$")
    provider: str = Field(min_length=1, max_length=32)
    priority: int = 100
    enabled: bool = True


@router.put("/routes")
def upsert_route(
    body: RouteIn, db: Session = Depends(get_db), p: Principal = Depends(require("routes:write"))
):
    r = db.scalar(select(Route).where(Route.prefix == body.prefix, Route.provider == body.provider))
    if r is None:
        r = Route(prefix=body.prefix, provider=body.provider, country="", priority=0)
        db.add(r)
    r.country, r.priority, r.enabled = body.country.upper(), body.priority, body.enabled
    db.flush()
    audit(db, p, "route.upsert", "route", r.id, body.model_dump())
    db.commit()
    return {"id": r.id, **body.model_dump(), "country": r.country}


@router.get("/routes")
def list_routes(db: Session = Depends(get_db), _: Principal = Depends(require("routes:write"))):
    return [
        {"id": r.id, "prefix": r.prefix, "country": r.country, "provider": r.provider,
         "priority": r.priority, "enabled": r.enabled}
        for r in db.scalars(select(Route).order_by(Route.prefix, Route.priority.desc()))
    ]  # fmt: skip


class PlanIn(BaseModel):
    rate_card_id: int
    enabled: bool = True
    rate_limit_per_min: int | None = Field(default=None, ge=1, le=1_000_000)


@router.put("/plans/{owner_ref}")
def upsert_plan(
    owner_ref: str,
    body: PlanIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("plans:write")),
):
    plan = db.scalar(select(AccountPlan).where(AccountPlan.owner_ref == owner_ref))
    if plan is None:
        plan = AccountPlan(owner_ref=owner_ref, rate_card_id=body.rate_card_id)
        db.add(plan)
    plan.rate_card_id, plan.enabled = body.rate_card_id, body.enabled
    plan.rate_limit_per_min = body.rate_limit_per_min
    db.flush()
    audit(db, p, "plan.upsert", "plan", owner_ref, body.model_dump())
    db.commit()
    return {"owner_ref": owner_ref, **body.model_dump()}


# --- Kill switch --------------------------------------------------------------


class SwitchIn(BaseModel):
    enabled: bool
    reason: str | None = Field(default=None, max_length=255)


def _switch_out(db: Session, name: str) -> dict:
    row = db.get(Switch, name)
    return {
        "name": name,
        "enabled": True if row is None else row.enabled,
        "reason": row.reason if row else None,
        "updated_by": row.updated_by if row else None,
    }


@router.get("/switches")
def get_switches(db: Session = Depends(get_db), _: Principal = Depends(require("monitor:read"))):
    return [_switch_out(db, n) for n in sorted(switches.NAMES)]


@router.put("/switches/{name}")
def put_switch(
    name: str,
    body: SwitchIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("switch:write")),
):
    if name not in switches.NAMES:
        raise HTTPException(404, {"code": "not_found", "message": "unknown switch"})
    if not body.enabled and not body.reason:
        raise HTTPException(422, {"code": "invalid", "message": "a reason is required to disable"})
    switches.set_switch(db, name, body.enabled, p.actor, body.reason)
    audit(db, p, "switch.set", "switch", name, body.model_dump())
    db.commit()
    return _switch_out(db, name)


# --- Audit dhe monitorim ------------------------------------------------------


@router.get("/audit")
def audit_log(
    actor: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    after_id: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    _: Principal = Depends(require("audit:read")),
):
    q = select(AuditLog).where(AuditLog.id > after_id)
    if actor:
        q = q.where(AuditLog.actor == actor)
    if target_type:
        q = q.where(AuditLog.target_type == target_type)
    if target_id:
        q = q.where(AuditLog.target_id == target_id)
    rows = db.scalars(q.order_by(AuditLog.id).limit(max(1, min(limit, 500))))
    return [
        {"id": a.id, "at": a.created_at, "actor": a.actor, "role": a.role, "action": a.action,
         "target_type": a.target_type, "target_id": a.target_id,
         "detail": json.loads(a.detail) if a.detail else None}
        for a in rows
    ]  # fmt: skip


UNKNOWN_OUTCOME = "%outcome_unknown"
IN_FLIGHT = (
    MessageStatus.QUEUED, MessageStatus.SENDING, MessageStatus.SENT, MessageStatus.UNKNOWN,
)  # fmt: skip


@router.get("/providers")
def provider_health(
    hours: int = 24,
    db: Session = Depends(get_db),
    _: Principal = Depends(require("monitor:read")),
):
    """Shëndeti i çdo provider-i SMS në `hours` orët e fundit: dorëzim, dështime, kodet kryesore
    të gabimit, mesazhe me rezultat të paqartë dhe mosha e më të vjetrit në rrugë."""
    hours = max(1, min(hours, 24 * 30))
    now = datetime.now(UTC)
    since = now - timedelta(hours=hours)
    out: dict[str, dict] = {}

    def slot(name: str) -> dict:
        return out.setdefault(
            name,
            {"provider": name, "total": 0, "delivered": 0, "failed": 0, "in_flight": 0,
             "delivery_rate": None, "unknown_outcome": 0, "top_errors": [],
             "last_delivered_at": None, "oldest_in_flight_seconds": None},
        )  # fmt: skip

    for name, st, n in db.execute(
        select(Message.provider, Message.status, func.count())
        .where(Message.created_at >= since)
        .group_by(Message.provider, Message.status)
    ):
        s = slot(name)
        s["total"] += n
        if st == MessageStatus.DELIVERED:
            s["delivered"] += n
        elif st == MessageStatus.FAILED:
            s["failed"] += n
        else:
            s["in_flight"] += n
            if st == MessageStatus.UNKNOWN:  # M9-a: rezultat i panjohur (hold-i mbahet)
                s["unknown_outcome"] += n
    for name, code, n in db.execute(
        select(Message.provider, Message.error_code, func.count())
        .where(
            Message.created_at >= since,
            Message.status == MessageStatus.FAILED,
            Message.error_code.is_not(None),
        )
        .group_by(Message.provider, Message.error_code)
        .order_by(func.count().desc())
    ):
        s = slot(name)
        if len(s["top_errors"]) < 5:
            s["top_errors"].append({"code": code, "count": n})
        if code.endswith("outcome_unknown"):
            s["unknown_outcome"] += n
    for name, last in db.execute(
        select(Message.provider, func.max(Message.updated_at))
        .where(Message.status == MessageStatus.DELIVERED)
        .group_by(Message.provider)
    ):
        slot(name)["last_delivered_at"] = last
    for name, oldest in db.execute(
        select(Message.provider, func.min(Message.created_at))
        .where(Message.status.in_(IN_FLIGHT))
        .group_by(Message.provider)
    ):
        slot(name)["oldest_in_flight_seconds"] = max(0, int((now - as_utc(oldest)).total_seconds()))
    for s in out.values():
        done = s["delivered"] + s["failed"]
        s["delivery_rate"] = round(s["delivered"] / done, 4) if done else None
    return {"hours": hours, "providers": sorted(out.values(), key=lambda s: -s["total"])}


@router.get("/messages/unresolved")
def unresolved_messages(
    limit: int = 100,
    db: Session = Depends(get_db),
    _: Principal = Depends(require("monitor:read")),
):
    """Mesazhe që dështuan me rezultat të paqartë (p.sh. timeout pasi kërkesa mund të kishte
    mbërritur): duhen krahasuar me panelin e provider-it. Numri maskohet pjesërisht."""
    rows = db.scalars(
        select(Message)
        .where(Message.status == MessageStatus.FAILED, Message.error_code.like(UNKNOWN_OUTCOME))
        .order_by(Message.id.desc())
        .limit(max(1, min(limit, 500)))
    ).all()
    return [
        {"id": m.public_id, "owner_ref": m.owner_ref, "provider": m.provider,
         "to": f"+{m.destination[:5]}…{m.destination[-2:]}", "sender": m.sender,
         "error_code": m.error_code, "total_price": str(m.total_price), "currency": m.currency,
         "created_at": m.created_at}
        for m in rows
    ]  # fmt: skip


@router.get("/stats")
def stats(db: Session = Depends(get_db), _: Principal = Depends(require("monitor:read"))):
    now = datetime.now(UTC)
    by_status = dict.fromkeys((s.value for s in MessageStatus), 0)
    by_status.update(
        {
            st.value: n
            for st, n in db.execute(select(Message.status, func.count()).group_by(Message.status))
        }
    )
    oldest = db.scalar(
        select(func.min(Message.next_attempt_at)).where(Message.status == MessageStatus.QUEUED)
    )
    stuck = msg_svc.stuck_sending(db, timedelta(minutes=10), now)
    bad_dlr = db.execute(
        select(DlrReceipt.outcome, func.count())
        .where(DlrReceipt.outcome != "applied", DlrReceipt.received_at > now - timedelta(hours=24))
        .group_by(DlrReceipt.outcome)
    )
    return {
        "messages_by_status": by_status,
        "oldest_queued_age_seconds": (
            max(0, int((now - as_utc(oldest)).total_seconds())) if oldest else None
        ),
        "stuck_sending": len(stuck),
        "unknown_outcome": _unknown_summary(db, now),
        "dlr_problems_24h": {o: n for o, n in bad_dlr},
        "switches": [_switch_out(db, n) for n in sorted(switches.NAMES)],
    }


# --- UNKNOWN: rezultat i panjohur i dërgimit (M9-a) -------------------------------------------


def _unknown_summary(db: Session, now: datetime) -> dict:
    sms = msg_svc.list_unknown(db, now, 500)
    mail = email_svc.list_unknown(db, now, 500)
    ages = [r["age_seconds"] for r in sms + mail]
    held: dict[str, str] = {}
    for r in sms:
        held[r["currency"]] = str(Decimal(held.get(r["currency"], "0")) + Decimal(r["held_amount"]))
    return {"sms": len(sms), "email": len(mail), "oldest_age_seconds": max(ages, default=None),
            "held_amount_by_currency": held}  # fmt: skip


class ResolveIn(BaseModel):
    outcome: str = Field(max_length=32)
    reason: str = Field(min_length=1, max_length=500)
    provider_message_id: str | None = Field(default=None, min_length=1, max_length=190)


class AttachIn(BaseModel):
    provider_message_id: str = Field(min_length=1, max_length=190)
    reason: str = Field(min_length=1, max_length=500)


@router.get("/queue/unknown")
def unknown_list(
    limit: int = 200,
    db: Session = Depends(get_db),
    _: Principal = Depends(require("monitor:read")),
):
    """Vetëm lexim: mesazhe/email UNKNOWN (moshë, provider, shuma e mbajtur, përpjekja e fundit)."""
    now = datetime.now(UTC)
    return {
        "summary": _unknown_summary(db, now),
        "sms": msg_svc.list_unknown(db, now, limit),
        "email": email_svc.list_unknown(db, now, limit),
    }


def _resolve(db: Session, svc, public_id: str, body: ResolveIn, p: Principal) -> dict:
    def go():
        item = svc.resolve_unknown(
            db, public_id, body.outcome, actor=p.actor, role=p.role, reason=body.reason,
            provider_message_id=body.provider_message_id,
        )  # fmt: skip
        return {"id": item.public_id, "status": item.status.value,
                "provider_message_id": item.provider_message_id}  # fmt: skip

    return _run(db, go)


@router.post("/queue/sms/{public_id}/resolve")
def resolve_sms(
    public_id: str,
    body: ResolveIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("queue:resolve")),
):
    """billable_delivered ⇒ capture i hold-it; non_billable_failed ⇒ release. Vetëm UNKNOWN."""
    return _resolve(db, msg_svc, public_id, body, p)


@router.post("/queue/email/{public_id}/resolve")
def resolve_email(
    public_id: str,
    body: ResolveIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("queue:resolve")),
):
    """confirmed_sent ⇒ SENT; not_sent ⇒ FAILED. Pa para (email s'ka wallet)."""
    return _resolve(db, email_svc, public_id, body, p)


def _attach(db: Session, svc, public_id: str, body: AttachIn, p: Principal) -> dict:
    def go():
        item = svc.attach_provider_message_id(
            db, public_id, body.provider_message_id, actor=p.actor, role=p.role, reason=body.reason
        )
        return {"id": item.public_id, "status": item.status.value,
                "provider_message_id": item.provider_message_id}  # fmt: skip

    return _run(db, go)


@router.post("/queue/sms/{public_id}/provider-id")
def attach_sms_provider_id(
    public_id: str,
    body: AttachIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("queue:resolve")),
):
    return _attach(db, msg_svc, public_id, body, p)


@router.post("/queue/email/{public_id}/provider-id")
def attach_email_provider_id(
    public_id: str,
    body: AttachIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("queue:resolve")),
):
    return _attach(db, email_svc, public_id, body, p)
