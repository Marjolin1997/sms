"""M8-d: API ADMIN e regjistrimit + politikës. Admin: lexim+shkrim; operator: vetëm lexim.
Asnjë logjikë biznesi: validim → autorizim → shërbim → hartim gabimesh → commit/rollback.

Rregull për lidhjen me Enterprise ekzistues: `approve` NUK pranon `enterprise_id`; lidhja bëhet
VETËM te `provision` (shërbimi M8-c e kryen brenda Tx2 me audit). Kështu miratimi mbetet vendim i
pastër dhe gjendja e provisioning-ut s'ndryshon në dy vende. `provision` kthen gjendjen aktuale
(200 edhe kur dështimi regjistrohet si `failed`; kodi i qëndrueshëm është për stafin). Nuk pret M7.
"""

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from apps.central.api.deps import get_db, require_role
from apps.central.models.registration import RegistrationRequest
from apps.central.models.registration_policy import ProductRegistrationPolicy
from apps.central.models.user import CentralUser, Role
from apps.central.services import provisioning
from apps.central.services import registration_policy as policy
from apps.central.services import registrations as reg

router = APIRouter(tags=["registration-admin"])
READ = require_role(Role.ADMIN, Role.OPERATOR)
WRITE = require_role(Role.ADMIN)


class ProductLinkOut(BaseModel):
    product_id: uuid.UUID
    code: str
    name: str
    channel: str
    assignment_id: uuid.UUID | None
    assignment_status: str | None


class DecisionOut(BaseModel):
    mode: str | None
    decided_at: datetime | None
    decided_by_user_id: uuid.UUID | None
    decided_by_label: str | None
    reason: str | None


class ProvisioningOut(BaseModel):
    status: str | None
    attempts: int
    error_code: str | None  # vetëm për stafin


class RegistrationOut(BaseModel):
    id: uuid.UUID
    contact_email: str
    contact_name: str | None
    enterprise_name: str
    status: str
    decision: DecisionOut
    provisioning: ProvisioningOut
    enterprise_id: uuid.UUID | None
    products: list[ProductLinkOut]
    created_at: datetime
    updated_at: datetime


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=reg.REASON_MAX)


class ProvisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enterprise_id: uuid.UUID | None = None


class AssignmentResultOut(BaseModel):
    product_code: str
    assignment_id: uuid.UUID
    created: bool


class ProvisionOut(BaseModel):
    request_id: uuid.UUID
    result: Literal["provisioned", "failed"]
    already_provisioned: bool
    enterprise_id: uuid.UUID | None
    new_enterprise: bool
    attempts: int
    error_code: str | None
    assignments: list[AssignmentResultOut]
    auto_granted_clients: int


class PolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    self_registration_enabled: bool | None = None
    approval_mode: Literal["manual", "automatic"] | None = None

    @model_validator(mode="after")
    def _something(self):
        given = self.model_fields_set
        if not given & {"self_registration_enabled", "approval_mode"}:
            raise ValueError("provide self_registration_enabled and/or approval_mode")
        if any(getattr(self, k) is None for k in given):
            raise ValueError("null is not allowed")
        return self


class PolicyOut(BaseModel):
    product_id: uuid.UUID
    product_code: str
    product_name: str
    product_channel: str
    product_status: str
    configured: bool  # false = pa rresht politike (i mbyllur)
    self_registration_enabled: bool
    approval_mode: str
    created_at: datetime | None
    updated_at: datetime | None


def _out(db: Session, rows: list[RegistrationRequest]) -> list[RegistrationOut]:
    links = reg.product_links(db, [r.id for r in rows])
    return [
        RegistrationOut(
            id=r.id, contact_email=r.contact_email, contact_name=r.contact_name,
            enterprise_name=r.enterprise_name, status=r.status,
            decision=DecisionOut(mode=r.decision_mode, decided_at=r.decided_at,
                                 decided_by_user_id=r.decided_by_id,
                                 decided_by_label=r.decided_by_label, reason=r.decision_reason),
            provisioning=ProvisioningOut(status=r.provisioning_status,
                                         attempts=r.provisioning_attempts,
                                         error_code=r.provisioning_error_code),
            enterprise_id=r.enterprise_id,
            products=[ProductLinkOut(product_id=p.id, code=p.code, name=p.name, channel=p.channel,
                                     assignment_id=rp.assignment_id,
                                     assignment_status=ep.status if ep else None)
                      for rp, p, ep in links[r.id]],
            created_at=r.created_at, updated_at=r.updated_at,
        )
        for r in rows
    ]  # fmt: skip


def _policy_out(p, pol: ProductRegistrationPolicy | None) -> PolicyOut:
    return PolicyOut(
        product_id=p.id, product_code=p.code, product_name=p.name, product_channel=p.channel,
        product_status=p.status, configured=pol is not None,
        self_registration_enabled=bool(pol and pol.self_registration_enabled),
        approval_mode=pol.approval_mode if pol else "manual",
        created_at=pol.created_at if pol else None, updated_at=pol.updated_at if pol else None,
    )  # fmt: skip


@router.get("/admin/registrations", response_model=list[RegistrationOut])
def list_registrations(
    status: Literal["submitted", "approved", "rejected"] | None = None,
    provisioning_status: Literal["pending", "provisioned", "failed"] | None = None,
    contact_email: str | None = Query(default=None, max_length=254),
    product_id: uuid.UUID | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: CentralUser = Depends(READ),
):
    rows = reg.list_requests(
        db, status=status, provisioning_status=provisioning_status, contact_email=contact_email,
        product_id=product_id, created_from=created_from, created_to=created_to,
        limit=limit, offset=offset,
    )  # fmt: skip
    return _out(db, rows)


@router.get("/admin/registrations/{registration_id}", response_model=RegistrationOut)
def get_registration(
    registration_id: uuid.UUID, db: Session = Depends(get_db), _: CentralUser = Depends(READ)
):
    return _out(db, [reg.get(db, registration_id)])[0]


@router.post("/admin/registrations/{registration_id}/approve", response_model=RegistrationOut)
def approve(
    registration_id: uuid.UUID, db: Session = Depends(get_db), actor: CentralUser = Depends(WRITE)
):
    row = reg.approve(db, registration_id, actor)
    db.commit()
    return _out(db, [row])[0]


@router.post("/admin/registrations/{registration_id}/reject", response_model=RegistrationOut)
def reject(
    registration_id: uuid.UUID,
    body: RejectIn,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    row = reg.reject(db, registration_id, actor, body.reason)
    db.commit()
    return _out(db, [row])[0]


@router.post("/admin/registrations/{registration_id}/provision", response_model=ProvisionOut)
def provision(
    registration_id: uuid.UUID,
    request: Request,
    body: ProvisionIn | None = None,
    actor: CentralUser = Depends(WRITE),
):
    """Një përpjekje (pa retry të brendshëm). Shërbimi zotëron transaksionet Tx2/Tx3."""
    res = provisioning.run(
        request.app.state.sessionmaker, registration_id,
        enterprise_id=body.enterprise_id if body else None, actor=actor,
    )  # fmt: skip
    return ProvisionOut(
        request_id=res.request_id, result=res.status, already_provisioned=res.already_provisioned,
        enterprise_id=res.enterprise_id, new_enterprise=res.new_enterprise, attempts=res.attempts,
        error_code=res.error_code,
        assignments=[AssignmentResultOut(**a) for a in res.assignments],
        auto_granted_clients=len(res.auto_grants),
    )  # fmt: skip


@router.get("/admin/registration-policies", response_model=list[PolicyOut])
def list_policies(db: Session = Depends(get_db), _: CentralUser = Depends(READ)):
    return [_policy_out(p, pol) for p, pol in policy.list_policies(db)]


@router.put("/admin/products/{product_id}/registration-policy", response_model=PolicyOut)
def put_policy(
    product_id: uuid.UUID,
    body: PolicyIn,
    db: Session = Depends(get_db),
    actor: CentralUser = Depends(WRITE),
):
    fields = {k: getattr(body, k) for k in body.model_fields_set}
    row, _ = policy.set_policy(db, product_id, actor, **fields)
    db.commit()
    p = next(p for p, _pol in policy.list_policies(db) if p.id == product_id)
    return _policy_out(p, row)
