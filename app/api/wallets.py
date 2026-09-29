from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import require_admin
from app.models.wallet import EntryType, LedgerEntry, Topup, TopupMethod, Wallet
from app.services import wallet as svc

router = APIRouter(prefix="/v1", dependencies=[Depends(require_admin)])


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


@router.post("/wallets", response_model=WalletOut, status_code=201)
def create_wallet(body: WalletIn, db: Session = Depends(get_db)):
    w = svc.create_wallet(db, body.owner_ref, body.currency)
    db.commit()
    return _wallet_out(db, w)


@router.get("/wallets/{wallet_id}", response_model=WalletOut)
def get_wallet(wallet_id: int, db: Session = Depends(get_db)):
    w = db.get(Wallet, wallet_id)
    if w is None:
        raise HTTPException(404, {"code": "not_found", "message": "wallet not found"})
    return _wallet_out(db, w)


@router.get("/wallets/{wallet_id}/ledger", response_model=list[EntryOut])
def ledger(wallet_id: int, limit: int = 100, after_id: int = 0, db: Session = Depends(get_db)):
    rows = db.scalars(
        select(LedgerEntry)
        .where(LedgerEntry.wallet_id == wallet_id, LedgerEntry.id > after_id)
        .order_by(LedgerEntry.id)
        .limit(min(limit, 500))
    ).all()
    return [EntryOut.model_validate(r) for r in rows]


@router.post("/wallets/{wallet_id}/topups", response_model=TopupOut, status_code=201)
def create_topup(wallet_id: int, body: TopupIn, db: Session = Depends(get_db)):
    try:
        t = svc.create_topup(db, wallet_id, body.amount, body.method, body.external_ref)
        db.commit()
    except svc.WalletError as e:
        db.rollback()
        raise _http(e) from e
    return _topup_out(t)


@router.post("/topups/{topup_id}/confirm", response_model=TopupOut)
def confirm_topup(topup_id: int, db: Session = Depends(get_db)):
    try:
        t = svc.confirm_topup(db, topup_id)
        db.commit()
    except svc.WalletError as e:
        db.rollback()
        raise _http(e) from e
    return _topup_out(t)
