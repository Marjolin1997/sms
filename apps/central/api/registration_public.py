"""M8-d: API PUBLIKE e regjistrimit (pa auth stafi). Vetëm: validim, thirrje shërbimesh, hartim
gabimesh, commit/rollback. Asnjë logjikë biznesi këtu.

Sigurimi: kodet publike janë të qëndrueshme dhe të pazbuluese (`registration_unavailable`,
`products_unavailable`, `invalid_request`, `idempotency_conflict`, `too_many_requests`,
`request_too_large`, `not_found`); asnjë detaj SQL/politike/provisioning. Tokeni vjen vetëm në header
(`X-Registration-Token`), kurrë në URL/log. Trupi kufizohet në 4 KB në nivel aplikacioni (lexim me
stream, pa u mbështetur te Content-Length); proxy-t duhet ta forcojë edhe `client_max_body_size`.
"""

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from apps.central.api.deps import get_db
from apps.central.core.config import settings
from apps.central.core.errors import CentralError, Conflict, Invalid, TooManyRequests
from apps.central.models.registration import (
    APPROVED,
    FAILED,
    PENDING,
    PROVISIONED,
    REJECTED,
    SUBMITTED,
    RegistrationRequest,
)
from apps.central.services import registration_policy as policy
from apps.central.services import registrations as reg

log = logging.getLogger("central.registration")
router = APIRouter(prefix="/registration", tags=["registration-public"])
MAX_BODY_BYTES = 4096

SAFE = {
    "registration_unavailable": (503, "registration is currently unavailable"),
    "products_unavailable": (422, "one or more requested products are unavailable"),
    "invalid_request": (422, "the request is invalid"),
    "idempotency_conflict": (409, "this Idempotency-Key was already used with a different request"),
    "too_many_requests": (429, "too many registration requests for this contact"),
    "request_too_large": (413, "the request body is too large"),
    "not_found": (404, "not found"),
}


class PublicError(Exception):
    def __init__(self, code: str):
        self.code, (self.status, self.message) = code, SAFE[code]


def public_enabled() -> None:
    if not settings.public_registration_enabled:
        raise PublicError("registration_unavailable")


async def bounded_body(request: Request, _: None = Depends(public_enabled)) -> bytes:
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > MAX_BODY_BYTES:
            raise PublicError("request_too_large")
    return bytes(buf)


class RegistrationIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=False)
    contact_email: str = Field(min_length=3, max_length=254)
    contact_name: str | None = Field(default=None, max_length=120)
    enterprise_name: str = Field(min_length=1, max_length=200)
    product_ids: list[uuid.UUID] = Field(min_length=1, max_length=reg.MAX_PRODUCTS)


class PublicProductOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    description: str | None
    channel: str
    # EFEKTIV: `automatic` vetëm nëse politika e lejon dhe gate-i i sigurisë është i hapur
    approval_mode: Literal["manual", "automatic"]


class SubmitOut(BaseModel):
    id: uuid.UUID
    status: Literal["in_review", "activating", "active", "rejected"]
    access_token: str | None  # vetëm në krijimin e parë; replay ⇒ null (s'rikuperohet)
    token_issued: bool


class StatusOut(BaseModel):
    id: uuid.UUID
    status: Literal["in_review", "activating", "active", "rejected"]


def public_status(row: RegistrationRequest) -> str:
    """submitted→in_review · approved+pending|failed→activating · approved+provisioned→active
    · rejected→rejected. `active` = provisioning i Central-it përfundoi (jo garanci operative pa
    vonesë: Enterprise e merr gjendjen përmes M7 në mënyrë asinkrone)."""
    if row.status == SUBMITTED:
        return "in_review"
    if row.status == REJECTED:
        return "rejected"
    if row.status == APPROVED and row.provisioning_status == PROVISIONED:
        return "active"
    if row.status == APPROVED and row.provisioning_status in (PENDING, FAILED):
        return "activating"
    return "activating"  # i paarritshëm nga CHECK-et; mos ekspozo gjendje të brendshme


@router.get(
    "/products", response_model=list[PublicProductOut], dependencies=[Depends(public_enabled)]
)
def list_products(db: Session = Depends(get_db)):
    return [
        PublicProductOut(id=p.id, code=p.code, name=p.name, description=p.description,
                         channel=p.channel, approval_mode=mode)
        for p, mode in policy.public_products(db)
    ]  # fmt: skip


@router.post("", status_code=202, response_model=SubmitOut, dependencies=[Depends(public_enabled)])
def submit(
    response: Response,
    raw: bytes = Depends(bounded_body),
    idempotency_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    try:
        body = RegistrationIn.model_validate_json(raw)
    except ValidationError:
        raise PublicError("invalid_request") from None
    try:
        res = reg.submit(
            db, enterprise_name=body.enterprise_name, contact_email=body.contact_email,
            contact_name=body.contact_name, product_ids=body.product_ids,
            submission_key=idempotency_key,
            max_per_email_24h=settings.public_registration_max_per_email_24h,
        )  # fmt: skip
        db.commit()
    except Invalid as e:
        db.rollback()
        raise PublicError(
            "products_unavailable" if str(e) == reg.UNAVAILABLE else "invalid_request"
        ) from None
    except Conflict:
        db.rollback()
        raise PublicError("idempotency_conflict") from None
    except TooManyRequests:
        db.rollback()
        raise PublicError("too_many_requests") from None
    except CentralError:
        db.rollback()
        raise PublicError("invalid_request") from None
    log.info("registration submit id=%s created=%s", res.request.id, res.created)
    if not res.created:
        response.status_code = 200  # replay: pa token të ri
    return SubmitOut(id=res.request.id, status=public_status(res.request),
                     access_token=res.access_token, token_issued=res.created)  # fmt: skip


@router.get(
    "/{registration_id}/status", response_model=StatusOut, dependencies=[Depends(public_enabled)]
)
def status(
    registration_id: str,
    x_registration_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """ID i keq, i panjohur, token i mungon ose i gabuar ⇒ I NJËJTI 404 (anti-enumerim);
    krahasimi i tokenit është në kohë të qëndrueshme edhe kur rreshti mungon."""
    try:
        row = reg.get(db, registration_id)
    except CentralError:
        row = None
    token = (
        x_registration_token if x_registration_token and len(x_registration_token) <= 256 else None
    )
    if not reg.verify_access_token(row, token):
        raise PublicError("not_found")
    return StatusOut(id=row.id, status=public_status(row))
