"""Endpoint-e publike (pa auth): çregjistrimi me një klik dhe webhook-u i bounce/complaint."""

import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.api.webhooks import verify_signature
from app.core.config import settings
from app.core.db import SessionLocal, get_db
from app.services import emails as svc
from app.services.wallet import Conflict, NotFound

router = APIRouter()

_PAGE = (
    "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
    "<title>Unsubscribe</title><body>{body}</body>"
)


@router.get("/u/{token}", response_class=HTMLResponse)
def unsubscribe_page(token: str):
    """GET nuk çregjistron (skanerët e email-it e hapin lidhjen): vetëm konfirmim me POST."""
    if not svc.valid_unsubscribe_token(token):
        return HTMLResponse(_PAGE.format(body="<p>This link is not valid.</p>"), status_code=404)
    body = (
        "<h1>Unsubscribe</h1><p>Confirm that you no longer want to receive these emails.</p>"
        f"<form method=post action=/u/{token}><button type=submit>Unsubscribe</button></form>"
    )
    return HTMLResponse(_PAGE.format(body=body))


@router.post("/u/{token}", response_class=HTMLResponse)
def unsubscribe_confirm(token: str, db: Session = Depends(get_db)):
    """Përdoret edhe nga klientët e email-it për RFC 8058 (List-Unsubscribe-Post)."""
    try:
        svc.unsubscribe(db, token)
        db.commit()
    except NotFound:
        db.rollback()
        return HTMLResponse(_PAGE.format(body="<p>This link is not valid.</p>"), status_code=404)
    return HTMLResponse(_PAGE.format(body="<h1>You are unsubscribed.</h1>"))


class EmailEventIn(BaseModel):
    provider_message_id: str = Field(min_length=1, max_length=190)
    event: str = Field(pattern="^(delivered|bounce_hard|bounce_soft|complaint)$")
    code: str | None = Field(default=None, max_length=190)


def _handle(provider: str, ev: EmailEventIn) -> tuple[int, str]:
    with SessionLocal() as db:
        try:
            svc.apply_event(db, provider, ev.provider_message_id, ev.event, ev.code)
            db.commit()
            return 200, "applied"
        except NotFound:
            db.rollback()
            return 503, "unknown_message"  # provider-i e riprovon
        except Conflict:
            db.rollback()
            return 409, "conflict"


@router.post("/webhooks/email/{provider}")
async def email_events(
    provider: str, request: Request, x_signature: str = Header(default="")
) -> dict:
    raw = await request.body()
    secret = settings.dlr_secrets.get(provider)
    if not secret or not verify_signature(secret, raw, x_signature):
        raise HTTPException(401, "invalid signature")
    try:
        ev = EmailEventIn.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as e:
        raise HTTPException(422, "invalid event payload") from e
    code, outcome = await run_in_threadpool(_handle, provider, ev)
    if code != 200:
        raise HTTPException(code, outcome)
    return {"outcome": outcome}
