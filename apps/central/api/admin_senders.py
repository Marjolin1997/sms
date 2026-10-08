"""API admin e politikës së sender-ave dhe regjistrit global (M10-S1). Admin = shkrim, operator = lexim. Pa DELETE/PUT/PATCH, pa API klienti, pa transport drejt Enterprise.
Skema strikte (`extra=forbid`); aktori vjen VETËM nga principali; gabimet e domain-it mapohen nga `errors.install` (404/409/422/403)."""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime, BaseModel, Field, StrictBool, StrictStr
from sqlalchemy.orm import Session

from apps.central.api.admin_common import STRICT, Reason, iso, page
from apps.central.api.deps import get_db, require_role
from apps.central.models.sender import CountrySenderPolicy, SenderDecision, SenderRegistry
from apps.central.models.user import CentralUser, Role
from apps.central.services import senders as svc

router = APIRouter(prefix="/admin")
READ = require_role(Role.ADMIN, Role.OPERATOR)
WRITE = require_role(Role.ADMIN)
MAX_PAGE = 200
Country = Field(pattern=r"^[A-Za-z]{2}$")
Kind = Literal["alphanumeric", "numeric"]
Status = Literal["pending", "approved", "rejected", "revoked"]
Evidence = Field(default=None, min_length=1, max_length=128)


class PolicyIn(BaseModel):
    model_config = STRICT
    country: StrictStr = Country
    sender_kind: Kind
    allowed: StrictBool
    requires_approval: StrictBool
    reason: Reason


class SenderIn(BaseModel):
    model_config = STRICT
    enterprise_id: uuid.UUID
    external_ref: StrictStr | None = Field(default=None, pattern=r"^[A-Za-z0-9._:-]{1,64}$")
    country: StrictStr = Country
    value: StrictStr = Field(min_length=1, max_length=16)
    evidence_ref: StrictStr | None = Evidence


class DecisionIn(BaseModel):
    model_config = STRICT
    evidence_ref: StrictStr | None = Evidence


class ReasonedIn(BaseModel):
    model_config = STRICT
    reason: Reason
    evidence_ref: StrictStr | None = Evidence


def policy_out(p: CountrySenderPolicy) -> dict:
    return {"id": str(p.id), "country": p.country, "sender_kind": p.sender_kind, "revision": p.revision, "allowed": p.allowed, "requires_approval": p.requires_approval,
            "effective_from": iso(p.effective_from), "reason": p.reason, "content_hash": p.content_hash, "created_by_id": str(p.created_by_id), "created_at": iso(p.created_at)}  # fmt: skip


def view_out(v: svc.PolicyView) -> dict:
    return {"country": v.country, "sender_kind": v.sender_kind, "source": v.source, "allowed": v.allowed, "requires_approval": v.requires_approval,
            "policy_id": None if v.policy_id is None else str(v.policy_id), "revision": v.revision, "effective_from": iso(v.effective_from), "effective_to": iso(v.effective_to)}  # fmt: skip


def sender_out(s: SenderRegistry) -> dict:
    return {"id": str(s.id), "enterprise_id": str(s.enterprise_id), "external_ref": s.external_ref, "country": s.country, "sender_kind": s.sender_kind, "display_value": s.display_value,
            "norm_value": s.norm_value, "status": s.current_status, "approved_key": s.approved_key, "current_decision_id": None if s.current_decision_id is None else str(s.current_decision_id),
            "source": s.source, "created_at": iso(s.created_at), "updated_at": iso(s.updated_at)}  # fmt: skip


def decision_out(d: SenderDecision) -> dict:
    return {"id": str(d.id), "seq": d.seq, "decision": d.decision, "from_status": d.from_status, "to_status": d.to_status, "category": d.category, "decided_at": iso(d.decided_at),
            "decided_by_id": None if d.decided_by_id is None else str(d.decided_by_id), "actor_label": d.actor_label, "reason": d.reason, "evidence_ref": d.evidence_ref,
            "policy_source": d.policy_source, "policy_id": None if d.policy_id is None else str(d.policy_id), "policy_revision": d.policy_revision, "source": d.source}  # fmt: skip


# --- politikat ---------------------------------------------------------------------------------------------------------------


@router.get("/sender-policies")
def list_policies(country: str | None = Query(None, pattern="^[A-Za-z]{2}$"), sender_kind: Kind | None = None, latest_only: bool = False,
                  limit: int = Query(50, ge=1, le=MAX_PAGE), offset: int = Query(0, ge=0), db: Session = Depends(get_db), _: CentralUser = Depends(READ)):  # fmt: skip
    rows = svc.list_policies(
        db, country=country, kind=sender_kind, latest_only=latest_only, limit=limit, offset=offset
    )
    return page(rows, limit, offset, policy_out)


@router.get("/sender-policies/effective")
def effective_policy(country: str = Query(pattern="^[A-Za-z]{2}$"), sender_kind: Kind = Query(), at: AwareDatetime | None = None, db: Session = Depends(get_db), _: CentralUser = Depends(READ)):  # fmt: skip
    return view_out(svc.effective_policy(db, country, sender_kind, at))


@router.get("/sender-policies/{policy_id}")
def get_policy(policy_id: uuid.UUID, db: Session = Depends(get_db), _: CentralUser = Depends(READ)):
    return policy_out(svc.get_policy(db, policy_id))


@router.post("/sender-policies", status_code=201)
def set_policy(body: PolicyIn, db: Session = Depends(get_db), actor: CentralUser = Depends(WRITE)):
    ch = svc.set_policy(
        db, actor, body.country, body.sender_kind, body.allowed, body.requires_approval, body.reason
    )
    db.commit()
    return {**policy_out(ch.policy), "created": ch.created, "revoked_senders": ch.revoked}


# --- sender-at ---------------------------------------------------------------------------------------------------------------


@router.get("/senders")
def list_senders(enterprise_id: uuid.UUID | None = None, country: str | None = Query(None, pattern="^[A-Za-z]{2}$"), status: Status | None = None, sender_kind: Kind | None = None,
                 limit: int = Query(50, ge=1, le=MAX_PAGE), offset: int = Query(0, ge=0), db: Session = Depends(get_db), _: CentralUser = Depends(READ)):  # fmt: skip
    rows = svc.list_senders(
        db,
        enterprise_id=enterprise_id,
        country=country,
        status=status,
        kind=sender_kind,
        limit=limit,
        offset=offset,
    )
    return page(rows, limit, offset, sender_out)


@router.get("/senders/{sender_id}")
def get_sender(sender_id: uuid.UUID, db: Session = Depends(get_db), _: CentralUser = Depends(READ)):
    return sender_out(svc.get_sender(db, sender_id))


@router.get("/senders/{sender_id}/history")
def sender_history(
    sender_id: uuid.UUID, db: Session = Depends(get_db), _: CentralUser = Depends(READ)
):
    return {"items": [decision_out(d) for d in svc.history(db, sender_id)]}


@router.post("/senders", status_code=201)
def request_sender(
    body: SenderIn, db: Session = Depends(get_db), actor: CentralUser = Depends(WRITE)
):
    """Kërkesë manuale e adminit (rikuperim/test); s'është API klienti. `external_ref` i gjeneruar nëse mungon."""
    ref = body.external_ref or f"admin:{uuid.uuid4().hex[:24]}"
    r = svc.request_sender(
        db,
        actor,
        body.enterprise_id,
        ref,
        body.country,
        body.value,
        body.evidence_ref,
        source="admin",
    )
    db.commit()
    return {**sender_out(r.sender), "created": r.created, "auto": r.auto}


@router.post("/senders/{sender_id}/approve")
def approve(
    sender_id: uuid.UUID,
    body: DecisionIn,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    row = svc.approve(db, actor, sender_id, body.evidence_ref)
    db.commit()
    return sender_out(row)


@router.post("/senders/{sender_id}/reject")
def reject(
    sender_id: uuid.UUID,
    body: ReasonedIn,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    row = svc.reject(db, actor, sender_id, body.reason, body.evidence_ref)
    db.commit()
    return sender_out(row)


@router.post("/senders/{sender_id}/revoke")
def revoke(
    sender_id: uuid.UUID,
    body: ReasonedIn,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    row = svc.revoke(db, actor, sender_id, body.reason, body.evidence_ref)
    db.commit()
    return sender_out(row)


@router.post("/senders/{sender_id}/resubmit")
def resubmit(
    sender_id: uuid.UUID,
    body: DecisionIn,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    r = svc.resubmit(db, actor, sender_id, body.evidence_ref)
    db.commit()
    return {**sender_out(r.sender), "auto": r.auto}


__all__ = ["router"]
