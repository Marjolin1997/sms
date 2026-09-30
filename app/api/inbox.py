"""Inbox: përgjigjet e marrësve. Përgjigjja dërgohet me POST /v1/messages si çdo SMS."""

import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.contacts import owner_for
from app.core.db import get_db
from app.core.security import Principal, require
from app.services import inbox as svc

router = APIRouter(prefix="/v1/inbox")


def _number(n: str) -> str:
    n = n.lstrip("+")
    if not re.fullmatch(r"\d{3,20}", n):
        raise HTTPException(422, {"code": "invalid_number", "message": "invalid phone number"})
    return n


@router.get("/threads")
def list_threads(
    q: str | None = None,
    unread_only: bool = False,
    before_id: int | None = None,
    limit: int = 30,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("messages:read")),
):
    return svc.threads(
        db, owner_for(p, owner_ref), q, unread_only, before_id, max(1, min(limit, 100))
    )


@router.get("/unread-count")
def unread_count(
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("messages:read")),
):
    return {"unread": svc.unread_count(db, owner_for(p, owner_ref))}


@router.get("/threads/{number}")
def get_thread(
    number: str,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("messages:read")),
):
    return svc.conversation(db, owner_for(p, owner_ref), _number(number))


@router.post("/threads/{number}/read")
def mark_thread_read(
    number: str,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("messages:read")),
):
    n = svc.mark_read(db, owner_for(p, owner_ref), _number(number))
    db.commit()
    return {"marked": n}
