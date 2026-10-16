"""Callback-et e Twilio: statusi i dorëzimit dhe SMS hyrës. Të dyja verifikojnë X-Twilio-Signature
mbi URL-në publike (SMS_PUBLIC_BASE_URL), jo mbi URL-në e brendshme pas proxy-t."""

from urllib.parse import parse_qsl

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from app.api.webhooks import DlrIn, _handle
from app.core.config import settings
from app.core.db import SessionLocal
from app.providers.twilio import FINAL_BAD, verify_twilio_signature
from app.services import inbox

router = APIRouter()

EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response/>'


async def _verified(request: Request, signature: str) -> tuple[bytes, list[tuple[str, str]]]:
    raw = await request.body()
    params = parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True)
    url = settings.public_base_url.rstrip("/") + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    if not verify_twilio_signature(settings.twilio_auth_token, url, params, signature):
        raise HTTPException(401, "invalid signature")
    return raw, params


@router.post("/webhooks/twilio/status")
async def twilio_status(request: Request, x_twilio_signature: str = Header(default="")) -> Response:
    raw, params = await _verified(request, x_twilio_signature)
    p = dict(params)
    sid, status = p.get("MessageSid", ""), p.get("MessageStatus", "")
    if not sid:
        raise HTTPException(422, "MessageSid is required")
    if status != "delivered" and status not in FINAL_BAD:
        return Response(
            EMPTY_TWIML, media_type="text/xml"
        )  # queued/sent/sending: s'ka çfarë të bëjmë
    dlr = DlrIn(
        provider_message_id=sid,
        status="delivered" if status == "delivered" else "failed",
        code=(f"twilio_{p['ErrorCode']}" if p.get("ErrorCode") else f"twilio_{status}")[:64],
    )
    code, outcome = await run_in_threadpool(_handle, "twilio", raw, dlr)
    if code == 409:
        return Response(
            EMPTY_TWIML, media_type="text/xml"
        )  # status i vonuar/kontradiktor: injorohet
    if code != 200:
        raise HTTPException(code, outcome)  # 503: mesazhi s'është ruajtur ende, Twilio riprovon
    return Response(EMPTY_TWIML, media_type="text/xml")


def _receive(p: dict[str, str]) -> None:
    with SessionLocal() as db:
        try:
            inbox.receive(
                db, "twilio", p.get("MessageSid") or None, p["To"], p["From"], p.get("Body", "")
            )
            db.commit()
        except Exception:
            db.rollback()
            raise


@router.post("/webhooks/twilio/inbound")
async def twilio_inbound(
    request: Request, x_twilio_signature: str = Header(default="")
) -> Response:
    _, params = await _verified(request, x_twilio_signature)
    p = dict(params)
    if not p.get("To") or not p.get("From"):
        raise HTTPException(422, "To and From are required")
    await run_in_threadpool(_receive, p)
    return Response(
        EMPTY_TWIML, media_type="text/xml"
    )  # TwiML bosh: pa përgjigje automatike nga Twilio
