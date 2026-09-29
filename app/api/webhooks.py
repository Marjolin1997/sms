import hashlib
import hmac
import json

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError

from app.core.config import settings
from app.core.db import SessionLocal
from app.models.sending import DlrReceipt
from app.services import messages as svc
from app.services.wallet import Conflict, NotFound

router = APIRouter()


class DlrIn(BaseModel):
    provider_message_id: str = Field(min_length=1, max_length=128)
    status: str = Field(pattern="^(delivered|failed)$")
    code: str | None = Field(default=None, max_length=64)


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    """Header: 'sha256=<hex HMAC-SHA256(body)>' (prefiksi opsional)."""
    given = header.removeprefix("sha256=")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(given, expected)


def _handle(provider: str, raw: bytes, dlr: DlrIn) -> tuple[int, str]:
    with SessionLocal() as db:
        try:
            svc.apply_dlr(
                db, provider, dlr.provider_message_id, dlr.status == "delivered", dlr.code
            )
            outcome, code = "applied", 200
            db.commit()
        except NotFound:
            db.rollback()
            outcome, code = "unknown_message", 503  # provider-i e riprovon
        except Conflict:
            db.rollback()
            outcome, code = "conflict", 409
        db.add(
            DlrReceipt(
                provider=provider, provider_message_id=dlr.provider_message_id,
                status=dlr.status, outcome=outcome, raw_body=raw.decode("utf-8", "replace"),
            )
        )  # fmt: skip
        db.commit()
    return code, outcome


@router.post("/webhooks/dlr/{provider}")
async def dlr_webhook(
    provider: str, request: Request, x_signature: str = Header(default="")
) -> dict:
    raw = await request.body()
    secret = settings.dlr_secrets.get(provider)
    if not secret or not verify_signature(secret, raw, x_signature):
        raise HTTPException(401, "invalid signature")
    try:
        dlr = DlrIn.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as e:
        raise HTTPException(422, "invalid DLR payload") from e
    code, outcome = await run_in_threadpool(_handle, provider, raw, dlr)
    if code != 200:
        raise HTTPException(code, outcome)
    return {"outcome": outcome}
