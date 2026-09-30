from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.tenant import scoped, tenant
from app.core.db import get_db
from app.core.security import Principal, require
from app.models.sending import Message, MessageEvent
from app.services import messages as svc
from app.services.wallet import WalletError

router = APIRouter(prefix="/v1")

_STATUS = {
    "not_found": 404, "conflict": 409, "insufficient_funds": 402, "no_rate": 422,
    "no_route": 422, "sender_not_allowed": 403, "account_disabled": 403,
    "template_not_usable": 403, "sending_paused": 503, "recipient_suppressed": 422,
    "rate_limited": 429,
}  # fmt: skip


class SendIn(BaseModel):
    owner_ref: str = Field(min_length=1, max_length=64)
    to: str
    sender: str
    text: str | None = Field(default=None, max_length=1600)  # ≤ 10 segmente GSM-7
    template_id: int | None = None
    category: str = Field(default="transactional", pattern="^(transactional|marketing)$")
    values: dict[str, str] = {}


class MessageOut(BaseModel):
    id: str
    status: str
    to: str
    sender: str
    segments: int
    currency: str
    unit_price: Decimal
    total_price: Decimal
    provider_message_id: str | None
    error_code: str | None


def _own_message(db: Session, public_id: str, p: Principal) -> Message:
    m = db.scalar(scoped(db, p, Message, select(Message).where(Message.public_id == public_id)))
    if m is None:
        raise HTTPException(404, {"code": "not_found", "message": "message not found"})
    return m


def _out(m: Message) -> MessageOut:
    return MessageOut(
        id=m.public_id, status=m.status.value, to=m.destination, sender=m.sender,
        segments=m.segments, currency=m.currency, unit_price=m.unit_price,
        total_price=m.total_price, provider_message_id=m.provider_message_id,
        error_code=m.error_code,
    )  # fmt: skip


@router.post("/messages", response_model=MessageOut, status_code=202)
def send(
    body: SendIn,
    idempotency_key: str = Header(default=""),
    db: Session = Depends(get_db),
    p: Principal = Depends(require("messages:send")),
):
    owner = tenant(db, p, body.owner_ref, write=True)
    try:
        m = svc.submit(
            db, owner, idempotency_key, body.to, body.sender,
            text=body.text, template_id=body.template_id, values=body.values,
            category=body.category,
        )  # fmt: skip
        db.commit()
    except WalletError as e:
        db.rollback()
        raise HTTPException(_STATUS.get(e.code, 422), {"code": e.code, "message": str(e)}) from e
    return _out(m)


@router.get("/messages/{public_id}", response_model=MessageOut)
def get_message(
    public_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("messages:read"))
):
    return _out(_own_message(db, public_id, p))


@router.get("/messages/{public_id}/events")
def events(
    public_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("messages:read"))
):
    m = _own_message(db, public_id, p)
    rows = db.scalars(
        select(MessageEvent).where(MessageEvent.message_id == m.id).order_by(MessageEvent.id)
    )
    return [{"from": e.from_status, "to": e.to_status, "detail": e.detail} for e in rows]
