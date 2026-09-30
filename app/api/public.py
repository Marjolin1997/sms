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
from app.core.texts import html_lang, tr
from app.services import emails as svc
from app.services.wallet import Conflict, NotFound

router = APIRouter()


def _page(body: str) -> str:
    return (
        f"<!doctype html><html lang={html_lang()}><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width'>"
        f"<title>{tr('Unsubscribe')}</title><body>{body}</body></html>"
    )


@router.get("/u/{token}", response_class=HTMLResponse)
def unsubscribe_page(token: str):
    """GET nuk çregjistron (skanerët e email-it e hapin lidhjen): vetëm konfirmim me POST."""
    if not svc.valid_unsubscribe_token(token):
        return HTMLResponse(_page(f"<p>{tr('This link is not valid.')}</p>"), status_code=404)
    body = (
        f"<h1>{tr('Unsubscribe')}</h1>"
        f"<p>{tr('Confirm that you no longer want to receive these emails.')}</p>"
        f"<form method=post action=/u/{token}>"
        f"<button type=submit>{tr('Unsubscribe')}</button></form>"
    )
    return HTMLResponse(_page(body))


@router.post("/u/{token}", response_class=HTMLResponse)
def unsubscribe_confirm(token: str, db: Session = Depends(get_db)):
    """Përdoret edhe nga klientët e email-it për RFC 8058 (List-Unsubscribe-Post)."""
    try:
        svc.unsubscribe(db, token)
        db.commit()
    except NotFound:
        db.rollback()
        return HTMLResponse(_page(f"<p>{tr('This link is not valid.')}</p>"), status_code=404)
    return HTMLResponse(_page(f"<h1>{tr('You are unsubscribed.')}</h1>"))


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


class PaymentEventIn(BaseModel):
    external_id: str = Field(min_length=1, max_length=128)
    status: str = Field(pattern="^(succeeded|failed)$")
    amount: str | None = Field(default=None, max_length=32)
    currency: str | None = Field(default=None, max_length=3)


def _handle_payment(provider: str, ev: PaymentEventIn) -> tuple[int, str]:
    from app.services import payments

    with SessionLocal() as db:
        try:
            payments.complete(db, provider, ev.external_id, ev.status, ev.amount, ev.currency)
            db.commit()
            return 200, "applied"
        except NotFound:
            db.rollback()
            return 404, "unknown_payment"
        except Conflict:
            db.commit()  # ruaj shënimin amount_mismatch/failed për rakordim
            return 409, "conflict"


@router.post("/webhooks/payments/{provider}")
async def payment_events(
    provider: str, request: Request, x_signature: str = Header(default="")
) -> dict:
    raw = await request.body()
    secret = settings.dlr_secrets.get(provider)
    if not secret or not verify_signature(secret, raw, x_signature):
        raise HTTPException(401, "invalid signature")
    try:
        ev = PaymentEventIn.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as e:
        raise HTTPException(422, "invalid payment payload") from e
    code, outcome = await run_in_threadpool(_handle_payment, provider, ev)
    if code != 200:
        raise HTTPException(code, outcome)
    return {"outcome": outcome}
