import hashlib
import hmac
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.errors import Conflict, NotFound
from app.models.sending import DlrReceipt
from app.services import inbox
from app.services import messages as svc

router = APIRouter()


class DlrIn(BaseModel):
    provider_message_id: str = Field(min_length=1, max_length=128)
    status: str = Field(pattern="^(delivered|failed)$")
    code: str | None = Field(default=None, max_length=64)
    # M9-a: opsionale (kur provider-i e kthen): `reference` = public_id; lidh DLR me mesazh UNKNOWN
    reference: str | None = Field(default=None, max_length=36)


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    """Header: 'sha256=<hex HMAC-SHA256(body)>' (prefiksi opsional)."""
    given = header.removeprefix("sha256=")
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(given, expected)


# Rezultatet pas të cilave një callback i identik (e njëjta gjurmë) është replay: përgjigjja e njëjtë, asnjë efekt i ri.
# `unknown_message` NUK hyn këtu: provider-i duhet të riprovojë dhe mesazhi mund të ekzistojë tani.
REPLAYABLE = {"applied": (200, "applied"), "conflict": (409, "conflict")}


def receipt_fingerprint(provider: str, dlr: DlrIn) -> str:
    """M11-J/G-05: sha256 mbi fushat e vlefshme të callback-ut (provider, id, status, kod, reference). Pa sekrete/firmë/PII.
    Twilio NUK jep vulë kohore as identifikues ngjarjeje në trup; kjo është e vetmja gjurmë e vërtetë e disponueshme."""
    canon = json.dumps(
        [provider, dlr.provider_message_id, dlr.status, dlr.code or "", dlr.reference or ""],
        separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode()).hexdigest()


def minimized_body(dlr: DlrIn) -> str:
    """M11-J/G-07: ruhet vetëm ajo që duhet për rakordim (id, status, kod, reference); kurrë trupi i papërpunuar (numra telefoni)."""
    data = {k: v for k, v in dlr.model_dump().items() if v is not None}
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _bump(db, receipt_id: int, now: datetime) -> None:
    db.execute(
        update(DlrReceipt)
        .where(DlrReceipt.id == receipt_id)
        .values(seen_count=DlrReceipt.seen_count + 1, last_seen_at=now)
    )


def _record(db, fp: str, provider: str, dlr: DlrIn, outcome: str, now: datetime) -> None:
    """Një rresht për (gjurmë, rezultat): callback i përsëritur rrit `seen_count`, nuk shton rresht."""
    row = db.scalar(
        select(DlrReceipt).where(DlrReceipt.fingerprint == fp, DlrReceipt.outcome == outcome)
    )
    if row is not None:
        _bump(db, row.id, now)
        return
    try:
        with db.begin_nested():
            db.add(
                DlrReceipt(
                    provider=provider, provider_message_id=dlr.provider_message_id,
                    status=dlr.status, outcome=outcome, raw_body=minimized_body(dlr),
                    fingerprint=fp, seen_count=1, last_seen_at=now, received_at=now,
                )
            )  # fmt: skip
            db.flush()
    except IntegrityError:  # garë: një callback identik u regjistrua njëkohësisht
        row = db.scalar(
            select(DlrReceipt).where(DlrReceipt.fingerprint == fp, DlrReceipt.outcome == outcome)
        )
        if row is not None:
            _bump(db, row.id, now)


def _handle(provider: str, dlr: DlrIn) -> tuple[int, str]:
    fp, now = receipt_fingerprint(provider, dlr), datetime.now(UTC)
    with SessionLocal() as db:
        prior = db.scalar(
            select(DlrReceipt)
            .where(DlrReceipt.fingerprint == fp, DlrReceipt.outcome.in_(tuple(REPLAYABLE)))
            .order_by(DlrReceipt.id)
            .limit(1)
        )
        if prior is not None:  # replay i saktë: asnjë efekt në mesazh/para, vetëm numërues
            _bump(db, prior.id, now)
            db.commit()
            return REPLAYABLE[prior.outcome]
        try:
            svc.apply_dlr(
                db,
                provider,
                dlr.provider_message_id,
                dlr.status == "delivered",
                dlr.code,
                reference=dlr.reference,
            )
            outcome, code = "applied", 200
            db.commit()
        except NotFound:
            db.rollback()
            outcome, code = "unknown_message", 503  # provider-i e riprovon
        except Conflict:
            db.rollback()
            outcome, code = "conflict", 409
        _record(db, fp, provider, dlr, outcome, now)
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
    code, outcome = await run_in_threadpool(_handle, provider, dlr)
    if code != 200:
        raise HTTPException(code, outcome)
    return {"outcome": outcome}


class InboundIn(BaseModel):
    to: str = Field(min_length=3, max_length=20)  # numri ynë që mori mesazhin
    from_: str = Field(alias="from", min_length=3, max_length=20)
    text: str = Field(max_length=1600)
    id: str | None = Field(default=None, min_length=1, max_length=128)  # për idempotencë


def _handle_inbound(provider: str, inb: InboundIn) -> str:
    with SessionLocal() as db:
        try:
            outcome, _ = inbox.receive(db, provider, inb.id, inb.to, inb.from_, inb.text)
            db.commit()
        except Exception:
            db.rollback()
            raise
        return outcome


@router.post("/webhooks/inbound/{provider}")
async def inbound_webhook(
    provider: str, request: Request, x_signature: str = Header(default="")
) -> dict:
    """SMS hyrës nga provider-i. STOP/START përditësojnë consent-in; me nënshkrim HMAC si DLR."""
    raw = await request.body()
    secret = settings.dlr_secrets.get(provider)
    if not secret or not verify_signature(secret, raw, x_signature):
        raise HTTPException(401, "invalid signature")
    try:
        inb = InboundIn.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as e:
        raise HTTPException(422, "invalid inbound payload") from e
    return {"outcome": await run_in_threadpool(_handle_inbound, provider, inb)}
