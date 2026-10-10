"""Inbox i SMS-eve hyrës dhe fjalët kyçe."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.tenant import tenant
from app.core.db import get_db
from app.core.errors import DomainError
from app.core.scope import owned
from app.core.security import Principal, require
from app.models.inbound import InboundMessage
from app.services import inbox as svc
from app.services.audit import audit

router = APIRouter(prefix="/v1")
_STATUS = {"not_found": 404, "conflict": 409}


def _run(db: Session, fn):
    try:
        out = fn()
        db.commit()
        return out
    except DomainError as e:
        db.rollback()
        raise HTTPException(_STATUS.get(e.code, 422), {"code": e.code, "message": str(e)}) from e


def _out(m: InboundMessage) -> dict:
    return {
        "id": m.id, "public_id": m.public_id, "from": m.from_number, "to": m.to_number,
        "text": m.text, "action": m.action, "keyword": m.keyword,
        "reply_status": m.reply_status, "reply_message_id": m.reply_message_id,
        "contact_id": m.contact_id, "read_at": m.read_at, "created_at": m.created_at,
    }  # fmt: skip


def _like(text: str) -> str:
    esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


@router.get("/inbox")
def list_inbox(
    unread: bool = False,
    q: str | None = None,
    before_id: int | None = None,
    limit: int = 50,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("inbox:read")),
):
    """Më të rejat së pari; `next_before_id` për faqen tjetër; `unread` = numri i palexuarave."""
    owner = tenant(db, p, owner_ref)
    lim = max(1, min(limit, 200))
    stmt = select(InboundMessage).where(owned(InboundMessage, owner))
    if unread:
        stmt = stmt.where(InboundMessage.read_at.is_(None))
    if q:
        term = _like(q.strip().lstrip("+"))
        stmt = stmt.where(
            InboundMessage.from_number.like(term, escape="\\")
            | InboundMessage.text.like(_like(q.strip()), escape="\\")
        )
    if before_id:
        stmt = stmt.where(InboundMessage.id < before_id)
    rows = db.scalars(stmt.order_by(InboundMessage.id.desc()).limit(lim + 1)).all()
    more = len(rows) > lim
    rows = rows[:lim]
    return {
        "items": [_out(m) for m in rows],
        "next_before_id": rows[-1].id if more else None,
        "unread": svc.unread_count(db, owner),
    }


@router.get("/inbox/unread")
def unread(
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("inbox:read")),
):
    return {"unread": svc.unread_count(db, tenant(db, p, owner_ref))}


class ReadIn(BaseModel):
    owner_ref: str | None = None
    ids: list[int] | None = Field(default=None, max_length=500)  # None = të gjitha


@router.post("/inbox/read")
def mark_read(
    body: ReadIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("inbox:write")),
):
    owner = tenant(db, p, body.owner_ref)
    return {"marked": _run(db, lambda: svc.mark_read(db, owner, body.ids))}


class KeywordIn(BaseModel):
    owner_ref: str | None = None
    keyword: str = Field(min_length=1, max_length=32)
    reply_text: str | None = Field(default=None, max_length=480)


def _kw_out(k) -> dict:
    return {
        "id": k.id,
        "keyword": k.keyword,
        "reply_text": k.reply_text,
        "created_at": k.created_at,
    }


@router.get("/keywords")
def list_keywords(
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("inbox:read")),
):
    return [_kw_out(k) for k in svc.list_keywords(db, tenant(db, p, owner_ref))]


@router.put("/keywords")
def put_keyword(
    body: KeywordIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("inbox:write")),
):
    """Krijon ose ndryshon një fjalë kyçe. Me `reply_text` dërgohet përgjigje automatike."""
    owner = tenant(db, p, body.owner_ref, write=True)

    def go():
        k = svc.set_keyword(db, owner, body.keyword, body.reply_text)
        audit(
            db, p, "keyword.set", "keyword", k.id, {"owner": owner.owner_ref, "keyword": k.keyword}
        )
        return _kw_out(k)

    return _run(db, go)


@router.delete("/keywords/{keyword_id}", status_code=204)
def delete_keyword(
    keyword_id: int,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("inbox:write")),
):
    owner = tenant(db, p, owner_ref)

    def go():
        svc.delete_keyword(db, owner, keyword_id)
        audit(db, p, "keyword.delete", "keyword", keyword_id, {"owner": owner.owner_ref})

    _run(db, go)
