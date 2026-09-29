from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import Principal, require
from app.models.wallet import EntryType, LedgerEntry, Topup, TopupMethod, Wallet
from app.services import wallet as svc
from app.services.audit import audit

router = APIRouter(prefix="/v1")


class WalletIn(BaseModel):
    owner_ref: str = Field(min_length=1, max_length=64)
    currency: str = Field(min_length=3, max_length=3)


class WalletOut(BaseModel):
    id: int
    owner_ref: str
    currency: str
    available: Decimal
    held: Decimal


class TopupIn(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=6)
    method: TopupMethod
    external_ref: str | None = Field(default=None, max_length=128)


class TopupOut(BaseModel):
    id: int
    wallet_id: int
    amount: Decimal
    method: TopupMethod
    status: str
    external_ref: str | None


class EntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    entry_type: EntryType
    available_delta: Decimal
    held_delta: Decimal
    available_after: Decimal
    held_after: Decimal
    ref_type: str | None
    ref_id: str | None


def _wallet_out(db: Session, w: Wallet) -> WalletOut:
    avail, held = svc.balances(db, w.id)
    return WalletOut(
        id=w.id, owner_ref=w.owner_ref, currency=w.currency, available=avail, held=held
    )


def _topup_out(t: Topup) -> TopupOut:
    return TopupOut(
        id=t.id,
        wallet_id=t.wallet_id,
        amount=t.amount,
        method=t.method,
        status=t.status.value,
        external_ref=t.external_ref,
    )


def _http(e: svc.WalletError) -> HTTPException:
    status = {"not_found": 404, "conflict": 409, "insufficient_funds": 402}.get(e.code, 422)
    return HTTPException(status, {"code": e.code, "message": str(e)})


def _own_wallet(db: Session, wallet_id: int, p: Principal) -> Wallet:
    w = db.get(Wallet, wallet_id)
    if w is None:
        raise HTTPException(404, {"code": "not_found", "message": "wallet not found"})
    p.check_owner(w.owner_ref)
    return w


def _run(db: Session, fn):
    try:
        out = fn()
        db.commit()
        return out
    except svc.WalletError as e:
        db.rollback()
        raise _http(e) from e


class AdjustIn(BaseModel):
    delta: Decimal = Field(max_digits=20, decimal_places=6)
    key: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=3, max_length=200)


@router.post("/wallets", response_model=WalletOut, status_code=201)
def create_wallet(
    body: WalletIn, db: Session = Depends(get_db), p: Principal = Depends(require("wallet:write"))
):
    def go():
        w = svc.create_wallet(db, body.owner_ref, body.currency)
        audit(db, p, "wallet.create", "wallet", w.id, body.model_dump())
        return w

    return _wallet_out(db, _run(db, go))


@router.get("/wallets/{wallet_id}", response_model=WalletOut)
def get_wallet(
    wallet_id: int, db: Session = Depends(get_db), p: Principal = Depends(require("wallet:read"))
):
    return _wallet_out(db, _own_wallet(db, wallet_id, p))


@router.get("/wallets/{wallet_id}/ledger", response_model=list[EntryOut])
def ledger(
    wallet_id: int,
    limit: int = 100,
    after_id: int = 0,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("wallet:read")),
):
    _own_wallet(db, wallet_id, p)
    rows = db.scalars(
        select(LedgerEntry)
        .where(LedgerEntry.wallet_id == wallet_id, LedgerEntry.id > after_id)
        .order_by(LedgerEntry.id)
        .limit(max(1, min(limit, 500)))
    ).all()
    return [EntryOut.model_validate(r) for r in rows]


@router.post("/wallets/{wallet_id}/topups", response_model=TopupOut, status_code=201)
def create_topup(
    wallet_id: int,
    body: TopupIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("topup:write")),
):
    def go():
        t = svc.create_topup(db, wallet_id, body.amount, body.method, body.external_ref, p.actor)
        audit(db, p, "topup.create", "topup", t.id, {"wallet": wallet_id, "amount": body.amount})
        return t

    return _topup_out(_run(db, go))


@router.post("/topups/{topup_id}/confirm", response_model=TopupOut)
def confirm_topup(
    topup_id: int, db: Session = Depends(get_db), p: Principal = Depends(require("topup:confirm"))
):
    def go():
        t = db.get(Topup, topup_id)
        # Ndarja e detyrave: ai që e krijoi nuk e konfirmon (përveç superadmin/bootstrap).
        if t and t.created_by == p.actor and p.role != "superadmin":
            raise HTTPException(
                403, {"code": "forbidden", "message": "creator cannot confirm own top-up"}
            )
        t = svc.confirm_topup(db, topup_id)
        audit(db, p, "topup.confirm", "topup", t.id, {"amount": t.amount})
        return t

    return _topup_out(_run(db, go))


@router.post("/wallets/{wallet_id}/adjustments", response_model=EntryOut, status_code=201)
def adjust(
    wallet_id: int,
    body: AdjustIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("wallet:adjust")),
):
    def go():
        e = svc.adjustment(db, wallet_id, body.delta, body.key, body.note)
        audit(db, p, "wallet.adjust", "wallet", wallet_id, {"delta": body.delta, "note": body.note})
        return e

    return EntryOut.model_validate(_run(db, go))


@router.get("/wallets/{wallet_id}/verify")
def verify(
    wallet_id: int, db: Session = Depends(get_db), p: Principal = Depends(require("monitor:read"))
):
    """Kontroll integriteti: balanca e ruajtur = SUM(delta) e ledger-it."""
    return {"wallet_id": wallet_id, "consistent": svc.verify_wallet(db, wallet_id)}
