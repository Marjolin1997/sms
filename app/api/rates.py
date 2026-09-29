from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import Principal, require
from app.services import rates as svc
from app.services.audit import audit
from app.services.wallet import WalletError

router = APIRouter(prefix="/v1")


class CardIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    currency: str = Field(min_length=3, max_length=3)


class RateIn(BaseModel):
    prefix: str
    operator: str = Field(default="", max_length=8)
    price_per_segment: Decimal = Field(ge=0, max_digits=20, decimal_places=6)


class PublishIn(BaseModel):
    effective_from: datetime


class QuoteIn(BaseModel):
    number: str
    text: str
    operator: str = ""
    at: datetime | None = None


class RateOut(BaseModel):
    id: int
    prefix: str
    operator: str
    price_per_segment: Decimal


class QuoteOut(BaseModel):
    card_id: int
    version_id: int
    rate_id: int
    currency: str
    encoding: str
    segments: int
    unit_price: Decimal
    total: Decimal


def _http(e: WalletError) -> HTTPException:
    status = {"not_found": 404, "conflict": 409, "no_rate": 404}.get(e.code, 422)
    return HTTPException(status, {"code": e.code, "message": str(e)})


def _run(db: Session, fn):
    try:
        out = fn()
        db.commit()
        return out
    except WalletError as e:
        db.rollback()
        raise _http(e) from e


@router.post("/rate-cards", status_code=201)
def create_card(
    body: CardIn, db: Session = Depends(get_db), p: Principal = Depends(require("rates:write"))
):
    def go():
        c = svc.create_card(db, body.name, body.currency)
        audit(db, p, "ratecard.create", "ratecard", c.id, body.model_dump())
        return c

    c = _run(db, go)
    return {"id": c.id, "name": c.name, "currency": c.currency}


@router.post("/rate-cards/{card_id}/versions", status_code=201)
def new_version(
    card_id: int, db: Session = Depends(get_db), p: Principal = Depends(require("rates:write"))
):
    def go():
        v = svc.new_draft(db, card_id)
        audit(db, p, "ratecard.draft", "ratecard", card_id, {"version": v.version})
        return v

    v = _run(db, go)
    return {"id": v.id, "version": v.version, "status": v.status.value}


@router.put("/rate-card-versions/{version_id}/rates", response_model=RateOut)
def put_rate(
    version_id: int,
    body: RateIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("rates:write")),
):
    def go():
        r = svc.set_rate(db, version_id, body.prefix, body.price_per_segment, body.operator)
        audit(db, p, "rate.set", "ratecard_version", version_id, body.model_dump())
        return r

    r = _run(db, go)
    return {
        "id": r.id,
        "prefix": r.prefix,
        "operator": r.operator,
        "price_per_segment": r.price_per_segment,
    }


@router.post("/rate-card-versions/{version_id}/publish")
def publish(
    version_id: int,
    body: PublishIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("rates:write")),
):
    def go():
        v = svc.publish(db, version_id, body.effective_from)
        audit(
            db,
            p,
            "ratecard.publish",
            "ratecard_version",
            v.id,
            {"effective_from": v.effective_from},
        )
        return v

    v = _run(db, go)
    return {
        "id": v.id,
        "version": v.version,
        "status": v.status.value,
        "effective_from": v.effective_from,
    }


@router.post("/rate-cards/{card_id}/quote", response_model=QuoteOut)
def quote(
    card_id: int,
    body: QuoteIn,
    db: Session = Depends(get_db),
    _: Principal = Depends(require("rates:read")),
):
    q = _run(db, lambda: svc.quote(db, card_id, body.number, body.text, body.at, body.operator))
    return QuoteOut(**q.__dict__)
