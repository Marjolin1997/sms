from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.context import worker_owner
from app.core.errors import (  # noqa: F401  (Conflict/NotFound: alias + përdorim)
    Conflict,
    DomainError,
    NotFound,
)
from app.core.scope import Owner, owned, ref
from app.models.wallet import (
    EntryType,
    Hold,
    HoldStatus,
    LedgerEntry,
    Topup,
    TopupMethod,
    TopupStatus,
    Wallet,
)

ZERO = Decimal("0")
QUANT = Decimal("0.000001")


# Gabimet bazë jetojnë te `app.core.errors` (burimi i vetëm). `WalletError`, `NotFound`, `Conflict`
# mbeten këtu vetëm si ALIASE përputhshmërie (të njëjtët objekte); kodi i ri: core.errors.
WalletError = DomainError


class InsufficientFunds(DomainError):
    code = "insufficient_funds"


class InvalidAmount(DomainError):
    code = "invalid_amount"


def money(value: Decimal | str | int) -> Decimal:
    """Pranon vetëm Decimal/str/int (kurrë float), maksimumi 6 shifra pas presjes."""
    if isinstance(value, float):
        raise InvalidAmount("float is not allowed for money")
    d = Decimal(value)
    if not d.is_finite() or d != d.quantize(QUANT):
        raise InvalidAmount("amount has more than 6 decimal places or is not finite")
    return d.quantize(QUANT)


def positive(value) -> Decimal:
    d = money(value)
    if d <= 0:
        raise InvalidAmount("amount must be > 0")
    return d


def create_wallet(db: Session, owner: Owner, currency: str) -> Wallet:
    currency = currency.upper()
    existing = db.scalar(select(Wallet).where(owned(Wallet, owner), Wallet.currency == currency))
    if existing:
        return existing
    w = Wallet(owner_ref=ref(owner), currency=currency)
    db.add(w)
    db.flush()
    return w


def lock_wallet(db: Session, wallet_id: int) -> Wallet:
    w = db.scalar(select(Wallet).where(Wallet.id == wallet_id).with_for_update())
    if w is None:
        raise NotFound("wallet not found")
    return w


def _last_entry(db: Session, wallet_id: int) -> LedgerEntry | None:
    return db.scalar(
        select(LedgerEntry)
        .where(LedgerEntry.wallet_id == wallet_id)
        .order_by(LedgerEntry.id.desc())
        .limit(1)
    )


def balances(db: Session, wallet_id: int) -> tuple[Decimal, Decimal]:
    last = _last_entry(db, wallet_id)
    return (last.available_after, last.held_after) if last else (ZERO, ZERO)


def _post(
    db: Session,
    wallet_id: int,
    entry_type: EntryType,
    available_delta: Decimal,
    held_delta: Decimal,
    key: str,
    ref_type: str | None = None,
    ref_id: str | None = None,
    note: str | None = None,
) -> LedgerEntry:
    """Shton një rresht në ledger. Thirret vetëm me wallet-in të kyçur."""
    dup = db.scalar(
        select(LedgerEntry).where(
            LedgerEntry.wallet_id == wallet_id, LedgerEntry.idempotency_key == key
        )
    )
    if dup is not None:
        if (dup.entry_type, dup.available_delta, dup.held_delta) != (
            entry_type,
            available_delta,
            held_delta,
        ):
            raise Conflict("idempotency key reused with different parameters")
        return dup
    avail, held = balances(db, wallet_id)
    if avail + available_delta < 0 or held + held_delta < 0:
        raise InsufficientFunds("insufficient available funds")
    entry = LedgerEntry(
        wallet_id=wallet_id,
        entry_type=entry_type,
        available_delta=available_delta,
        held_delta=held_delta,
        available_after=avail + available_delta,
        held_after=held + held_delta,
        idempotency_key=key,
        ref_type=ref_type,
        ref_id=ref_id,
        note=note,
    )
    db.add(entry)
    db.flush()
    _check_low_balance(db, wallet_id, entry.available_after)
    return entry


def _check_low_balance(db: Session, wallet_id: int, available: Decimal) -> None:
    """Event një herë kur balanca bie nën prag; flamuri rifutet kur ngrihet mbi prag.
    Thirret brenda transaksionit të lëvizjes (wallet-i është i kyçur)."""
    w = db.get(Wallet, wallet_id)
    if w is None or w.low_balance_threshold is None:
        return
    if available < w.low_balance_threshold and not w.low_balance_notified:
        w.low_balance_notified = True
        from app.services import events  # vonuar: shmang varësinë rrethore

        events.emit(
            db, worker_owner(db, w), "wallet.low_balance", "wallet", w.id,
            {"currency": w.currency, "available": str(available),
             "threshold": str(w.low_balance_threshold)},
        )  # fmt: skip
    elif available >= w.low_balance_threshold and w.low_balance_notified:
        w.low_balance_notified = False


def set_low_balance_threshold(db: Session, wallet_id: int, threshold) -> Wallet:
    """None/0 e çaktivizon. Vlerësohet menjëherë kundrejt balancës aktuale."""
    w = lock_wallet(db, wallet_id)
    value = None if threshold is None else money(threshold)
    if value is not None and value < 0:
        raise InvalidAmount("threshold must not be negative")
    w.low_balance_threshold = value or None
    w.low_balance_notified = False
    db.flush()
    _check_low_balance(db, wallet_id, balances(db, wallet_id)[0])
    return w


# --- Top-up -----------------------------------------------------------------


def create_topup(
    db: Session,
    wallet_id: int,
    amount,
    method: TopupMethod,
    external_ref: str | None = None,
    created_by: str | None = None,
) -> Topup:
    amount = positive(amount)
    lock_wallet(db, wallet_id)
    if external_ref:
        dup = db.scalar(select(Topup).where(Topup.external_ref == external_ref))
        if dup:
            if dup.wallet_id != wallet_id or dup.amount != amount:
                raise Conflict("external_ref already used for a different top-up")
            return dup
    t = Topup(
        wallet_id=wallet_id,
        amount=amount,
        method=method,
        external_ref=external_ref,
        created_by=created_by,
    )
    db.add(t)
    db.flush()
    return t


def confirm_topup(db: Session, topup_id: int) -> Topup:
    t = db.get(Topup, topup_id)
    if t is None:
        raise NotFound("top-up not found")
    lock_wallet(db, t.wallet_id)
    db.refresh(t)  # gjendja e re pasi u mor kyçi
    if t.status == TopupStatus.CONFIRMED:
        return t
    if t.status == TopupStatus.FAILED:
        raise Conflict("top-up already failed")
    _post(db, t.wallet_id, EntryType.TOPUP, t.amount, ZERO, f"topup:{t.id}", "topup", str(t.id))
    t.status = TopupStatus.CONFIRMED
    t.confirmed_at = datetime.now(UTC)
    return t


def fail_topup(db: Session, topup_id: int) -> Topup:
    t = db.get(Topup, topup_id)
    if t is None:
        raise NotFound("top-up not found")
    lock_wallet(db, t.wallet_id)
    db.refresh(t)
    if t.status == TopupStatus.CONFIRMED:
        raise Conflict("top-up already confirmed")
    t.status = TopupStatus.FAILED
    return t


# --- Hold / capture / release / refund -------------------------------------


def reserve(db: Session, wallet_id: int, amount, reference: str) -> Hold:
    amount = positive(amount)
    lock_wallet(db, wallet_id)
    hold = db.scalar(select(Hold).where(Hold.wallet_id == wallet_id, Hold.reference == reference))
    if hold:
        if hold.amount != amount:
            raise Conflict("reference reused with a different amount")
        return hold
    _post(db, wallet_id, EntryType.HOLD, -amount, amount, f"hold:{reference}", "hold", reference)
    hold = Hold(wallet_id=wallet_id, amount=amount, reference=reference)
    db.add(hold)
    db.flush()
    return hold


def _locked_hold(db: Session, hold_id: int) -> Hold:
    hold = db.get(Hold, hold_id)
    if hold is None:
        raise NotFound("hold not found")
    lock_wallet(db, hold.wallet_id)
    db.refresh(hold)
    return hold


def capture(db: Session, hold_id: int, amount=None) -> Hold:
    """Kap shumën përfundimtare (<= rezervimit); ndryshimi lirohet automatikisht."""
    hold = _locked_hold(db, hold_id)
    if hold.status == HoldStatus.CAPTURED:
        if amount is not None and money(amount) != hold.captured_amount:
            raise Conflict("hold already captured with a different amount")
        return hold
    if hold.status == HoldStatus.RELEASED:
        raise Conflict("hold already released")
    final = hold.amount if amount is None else positive(amount)
    if final > hold.amount:
        raise InvalidAmount("capture exceeds held amount")
    ref = str(hold.id)
    _post(db, hold.wallet_id, EntryType.CAPTURE, ZERO, -final, f"capture:{ref}", "hold", ref)
    if final < hold.amount:
        rest = hold.amount - final
        _post(db, hold.wallet_id, EntryType.RELEASE, rest, -rest, f"release:{ref}", "hold", ref)
    hold.status = HoldStatus.CAPTURED
    hold.captured_amount = final
    return hold


def release(db: Session, hold_id: int) -> Hold:
    hold = _locked_hold(db, hold_id)
    if hold.status == HoldStatus.RELEASED:
        return hold
    if hold.status == HoldStatus.CAPTURED:
        raise Conflict("hold already captured; use refund")
    ref = str(hold.id)
    _post(
        db, hold.wallet_id, EntryType.RELEASE, hold.amount, -hold.amount,
        f"release:{ref}", "hold", ref,
    )  # fmt: skip
    hold.status = HoldStatus.RELEASED
    return hold


def refund(db: Session, wallet_id: int, amount, key: str, note: str | None = None) -> LedgerEntry:
    """Rimbursim pas capture (p.sh. DLR 'failed' i vonuar). Idempotent sipas key."""
    amount = positive(amount)
    lock_wallet(db, wallet_id)
    return _post(
        db, wallet_id, EntryType.REFUND, amount, ZERO, f"refund:{key}", "refund", key, note
    )


def adjustment(db: Session, wallet_id: int, delta, key: str, note: str) -> LedgerEntry:
    """Korrigjim manual nga admin (audit-uar): delta mund të jetë negative."""
    delta = money(delta)
    if delta == 0:
        raise InvalidAmount("adjustment must be non-zero")
    lock_wallet(db, wallet_id)
    return _post(
        db, wallet_id, EntryType.ADJUSTMENT, delta, ZERO, f"adj:{key}", "adjustment", key, note
    )


def verify_wallet(db: Session, wallet_id: int) -> bool:
    """Kontroll integriteti: balanca e ruajtur në rreshtin e fundit = SUM(delta)."""
    sums = db.execute(
        select(
            func.coalesce(func.sum(LedgerEntry.available_delta), 0),
            func.coalesce(func.sum(LedgerEntry.held_delta), 0),
        ).where(LedgerEntry.wallet_id == wallet_id)
    ).one()
    return balances(db, wallet_id) == (Decimal(sums[0]), Decimal(sums[1]))


def charge(
    db: Session,
    wallet_id: int,
    amount,
    key: str,
    ref_type: str,
    ref_id: str,
    note: str | None = None,
) -> LedgerEntry:
    """Debit i drejtpërdrejtë (pagesë fature). Idempotent sipas key; ngre InsufficientFunds."""
    amount = positive(amount)
    lock_wallet(db, wallet_id)
    return _post(
        db, wallet_id, EntryType.INVOICE, -amount, ZERO, f"charge:{key}", ref_type, ref_id, note
    )
