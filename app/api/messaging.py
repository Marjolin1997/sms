from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import Principal, require
from app.models.messaging import ApprovalStatus, SenderId, Template, TemplateVersion
from app.services import sender_ids as sid
from app.services import templates as tpl
from app.services.audit import audit
from app.services.wallet import WalletError

router = APIRouter(prefix="/v1")

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


def _own_template(db: Session, template_id: int, p: Principal) -> None:
    t = db.get(Template, template_id)
    if t is None:
        raise HTTPException(404, {"code": "not_found", "message": "template not found"})
    p.check_owner(t.owner_ref)


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
def request_sender(
    body: SenderIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("sender:request")),
):
    p.check_owner(body.owner_ref)

    def go():
        s = sid.request(db, body.owner_ref, body.country, body.value)
        audit(db, p, "sender.request", "sender_id", s.id, body.model_dump())
        return s

    return _sender(_run(db, go))


def _sender_review(action: str, fn):
    def endpoint(
        sender_id: int,
        body: ReviewIn,
        db: Session = Depends(get_db),
        p: Principal = Depends(require("sender:review")),
    ):
        def go():
            s = (
                fn(db, sender_id, p.actor, body.reason or "")
                if action != "approve"
                else fn(db, sender_id, p.actor)
            )
            audit(db, p, f"sender.{action}", "sender_id", sender_id, {"reason": body.reason})
            return s

        return _sender(_run(db, go))

    return endpoint


for _action, _fn in (("approve", sid.approve), ("reject", sid.reject), ("revoke", sid.revoke)):
    router.add_api_route(
        f"/sender-ids/{{sender_id}}/{_action}",
        _sender_review(_action, _fn),
        methods=["POST"],
        response_model=SenderOut,
    )


@router.post("/templates", response_model=VersionOut, status_code=201)
def create_template(
    body: TemplateIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("template:write")),
):
    p.check_owner(body.owner_ref)

    def go():
        v = tpl.create(db, body.owner_ref, body.name, body.body)
        audit(db, p, "template.create", "template", v.template_id, {"name": body.name})
        return v

    return _version(_run(db, go))


@router.post("/templates/{template_id}/versions", response_model=VersionOut, status_code=201)
def add_version(
    template_id: int,
    body: VersionBodyIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("template:write")),
):
    _own_template(db, template_id, p)

    def go():
        v = tpl.new_version(db, template_id, body.body)
        audit(db, p, "template.version", "template", template_id, {"version": v.version})
        return v

    return _version(_run(db, go))


@router.post("/template-versions/{version_id}/{action}", response_model=VersionOut)
def review_version(
    version_id: int,
    action: str,
    body: ReviewIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("template:review")),
):
    if action not in ("approve", "reject", "revoke"):
        raise HTTPException(404, "unknown action")

    def go():
        v = tpl.review(db, version_id, action, p.actor, body.reason)
        audit(db, p, f"template.{action}", "template_version", version_id, {"reason": body.reason})
        return v

    return _version(_run(db, go))


@router.post("/templates/{template_id}/render", response_model=RenderOut)
def render(
    template_id: int,
    body: RenderIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("template:render")),
):
    p.check_owner(body.owner_ref)
    r = _run(db, lambda: tpl.render(db, body.owner_ref, template_id, body.values))
    return RenderOut(**r.__dict__)
