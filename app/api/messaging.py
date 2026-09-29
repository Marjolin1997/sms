from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import require_admin
from app.models.messaging import ApprovalStatus, SenderId, TemplateVersion
from app.services import sender_ids as sid
from app.services import templates as tpl
from app.services.wallet import WalletError

router = APIRouter(prefix="/v1", dependencies=[Depends(require_admin)])

_STATUS = {"not_found": 404, "conflict": 409, "sender_not_allowed": 403, "template_not_usable": 403}


def _run(db: Session, fn):
    try:
        out = fn()
        db.commit()
        return out
    except WalletError as e:
        db.rollback()
        raise HTTPException(_STATUS.get(e.code, 422), {"code": e.code, "message": str(e)}) from e


class SenderIn(BaseModel):
    owner_ref: str = Field(min_length=1, max_length=64)
    country: str = Field(min_length=2, max_length=2)
    value: str = Field(min_length=1, max_length=16)


class SenderOut(BaseModel):
    id: int
    owner_ref: str
    country: str
    value: str
    kind: str
    status: ApprovalStatus
    reason: str | None


class ReviewIn(BaseModel):
    actor: str = Field(min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=255)


class TemplateIn(BaseModel):
    owner_ref: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    body: str


class VersionBodyIn(BaseModel):
    body: str


class VersionOut(BaseModel):
    id: int
    template_id: int
    version: int
    status: ApprovalStatus
    variables: list[str]


class RenderIn(BaseModel):
    owner_ref: str
    values: dict[str, str] = {}


class RenderOut(BaseModel):
    version_id: int
    text: str
    encoding: str
    segments: int


def _sender(s: SenderId) -> SenderOut:
    return SenderOut(
        id=s.id, owner_ref=s.owner_ref, country=s.country, value=s.value,
        kind=s.kind.value, status=s.status, reason=s.reason,
    )  # fmt: skip


def _version(v: TemplateVersion) -> VersionOut:
    return VersionOut(
        id=v.id, template_id=v.template_id, version=v.version, status=v.status,
        variables=tpl.variables(v.body),
    )  # fmt: skip


@router.post("/sender-ids", response_model=SenderOut, status_code=201)
def request_sender(body: SenderIn, db: Session = Depends(get_db)):
    return _sender(_run(db, lambda: sid.request(db, body.owner_ref, body.country, body.value)))


@router.post("/sender-ids/{sender_id}/approve", response_model=SenderOut)
def approve_sender(sender_id: int, body: ReviewIn, db: Session = Depends(get_db)):
    return _sender(_run(db, lambda: sid.approve(db, sender_id, body.actor)))


@router.post("/sender-ids/{sender_id}/reject", response_model=SenderOut)
def reject_sender(sender_id: int, body: ReviewIn, db: Session = Depends(get_db)):
    return _sender(_run(db, lambda: sid.reject(db, sender_id, body.actor, body.reason or "")))


@router.post("/sender-ids/{sender_id}/revoke", response_model=SenderOut)
def revoke_sender(sender_id: int, body: ReviewIn, db: Session = Depends(get_db)):
    return _sender(_run(db, lambda: sid.revoke(db, sender_id, body.actor, body.reason or "")))


@router.post("/templates", response_model=VersionOut, status_code=201)
def create_template(body: TemplateIn, db: Session = Depends(get_db)):
    return _version(_run(db, lambda: tpl.create(db, body.owner_ref, body.name, body.body)))


@router.post("/templates/{template_id}/versions", response_model=VersionOut, status_code=201)
def add_version(template_id: int, body: VersionBodyIn, db: Session = Depends(get_db)):
    return _version(_run(db, lambda: tpl.new_version(db, template_id, body.body)))


@router.post("/template-versions/{version_id}/{action}", response_model=VersionOut)
def review_version(version_id: int, action: str, body: ReviewIn, db: Session = Depends(get_db)):
    if action not in ("approve", "reject", "revoke"):
        raise HTTPException(404, "unknown action")
    return _version(_run(db, lambda: tpl.review(db, version_id, action, body.actor, body.reason)))


@router.post("/templates/{template_id}/render", response_model=RenderOut)
def render(template_id: int, body: RenderIn, db: Session = Depends(get_db)):
    r = _run(db, lambda: tpl.render(db, body.owner_ref, template_id, body.values))
    return RenderOut(**r.__dict__)
