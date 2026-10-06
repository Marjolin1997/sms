"""M9-c: baseline-i i cutover-it, mapimi wallet↔produkt SMS dhe provat e mint-it lokal. Pa HTTP, pa Central.

**Mapimi (eksplicit, pa emra):** një grant `(enterprise_id, product_id, currency)` financon wallet-in
`(owner_ref i enterprise-it, currency)` VETËM nëse `product_id` është produkti i vetëm me kanal `sms` i
enterprise-it sipas entitlement-eve të sinkronizuara (cp.v1; jo `withdrawn`). Zero ose shumë produkte SMS,
produkt email, ose enterprise i panjohur ⇒ `Unmapped` (fail-closed). Wallet-i është SMS-only: faturat
(tarifë plani + email overage) nuk paguhen prej tij nën autoritet ≠ local (shih `billing`).

**Baseline:** `gross_at_cutover = available + held` në çastin e snapshot-it, me `ledger_max_id` si provë
durabël. `baseline_ref` = SHA-256 i JSON-it kanonik të fushave financiare (+ `created_at`): çdokush e
rillogarit; ndryshimi i çdo fushe e prish. Mint-i lokal pas baseline-it provohet nga ledger-i
(`id > ledger_max_id` me rritje neto > 0 dhe tip ≠ GRANT), jo nga deklarata e operatorit.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import DomainError
from app.core.timeutil import utcnow
from app.models.control_plane import ENTITLEMENT_WITHDRAWN, Entitlement
from app.models.enterprise import Enterprise
from app.models.money_authority import (
    BASELINE_ACTIVE,
    BASELINE_SUPERSEDED,
    G_MATCHED,
    MoneyBaseline,
    MoneyGrant,
)
from app.models.wallet import EntryType, Hold, HoldStatus, LedgerEntry, Wallet
from app.services import audit
from app.services import wallet as wallets

ZERO = Decimal("0")
BASELINE_VERSION = 1


class Unmapped(DomainError):
    code = "money_unmapped"


class BaselineError(DomainError):
    code = "baseline_error"


def _fmt(d: Decimal) -> str:
    return format(Decimal(d).quantize(Decimal("0.000001")), "f")


# --- mapimi --------------------------------------------------------------------------------------


def sms_product_for(db: Session, enterprise_id: uuid.UUID) -> uuid.UUID:
    """Produkti i vetëm SMS i enterprise-it ose `Unmapped`."""
    rows = list(
        db.scalars(
            select(Entitlement.product_id).where(
                Entitlement.enterprise_id == enterprise_id,
                Entitlement.channel == "sms",
                Entitlement.status != ENTITLEMENT_WITHDRAWN,
            )
        )
    )
    products = set(rows)
    if not products:
        raise Unmapped("enterprise has no sms product entitlement")
    if len(products) > 1:
        raise Unmapped("enterprise has more than one sms product: the wallet mapping is ambiguous")
    return next(iter(products))


def check_product(db: Session, enterprise_id: uuid.UUID, product_id: uuid.UUID) -> None:
    """Mapimi i produktit pa wallet: `product_id` duhet të jetë produkti i vetëm SMS i enterprise-it."""
    if sms_product_for(db, enterprise_id) != product_id:
        raise Unmapped("grant product is not the enterprise's sms product")


def resolve_wallet(
    db: Session, enterprise_id: uuid.UUID, product_id: uuid.UUID, currency: str, *, create: bool
) -> Wallet:
    """Wallet-i operacional i një grant-i (ose `Unmapped`). `create=True` (vetëm për grant normal nën
    central) krijon wallet bosh; baseline/bootstrap kërkon wallet ekzistues."""
    check_product(db, enterprise_id, product_id)
    ent = db.get(Enterprise, enterprise_id)
    if ent is None:
        raise Unmapped("enterprise is unknown locally")
    w = db.scalar(
        select(Wallet).where(Wallet.owner_ref == ent.owner_ref, Wallet.currency == currency)
    )
    if w is not None:
        if w.enterprise_id not in (None, enterprise_id):
            raise Unmapped("wallet belongs to a different enterprise identity")
        return w
    if not create:
        raise Unmapped(f"no {currency} wallet for the enterprise")
    w = Wallet(owner_ref=ent.owner_ref, currency=currency, enterprise_id=enterprise_id)
    db.add(w)
    db.flush()
    return w


# --- baseline ------------------------------------------------------------------------------------


def compute_ref(*, wallet_id, enterprise_id, currency, product_id, available, held, gross,
                ledger_max_id, created_at: datetime) -> str:  # fmt: skip
    doc = {
        "v": BASELINE_VERSION, "wallet_id": int(wallet_id), "enterprise_id": str(enterprise_id),
        "currency": currency, "product_id": str(product_id), "available": _fmt(available),
        "held": _fmt(held), "gross": _fmt(gross), "ledger_max_id": int(ledger_max_id),
        "created_at": _utc(created_at).isoformat(timespec="microseconds"),
    }  # fmt: skip
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def baseline_ref_of(b: MoneyBaseline) -> str:
    return compute_ref(
        wallet_id=b.wallet_id, enterprise_id=b.enterprise_id, currency=b.currency,
        product_id=b.product_id, available=b.available_at_cutover, held=b.held_at_cutover,
        gross=b.gross_at_cutover, ledger_max_id=b.ledger_max_id, created_at=_utc(b.created_at),
    )  # fmt: skip


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def baseline_valid(b: MoneyBaseline) -> bool:
    return (
        b.baseline_ref == baseline_ref_of(b)
        and b.gross_at_cutover == b.available_at_cutover + b.held_at_cutover
    )


def active_holds_sum(db: Session, wallet_id: int) -> Decimal:
    return Decimal(
        db.scalar(
            select(func.coalesce(func.sum(Hold.amount), 0)).where(
                Hold.wallet_id == wallet_id, Hold.status == HoldStatus.ACTIVE
            )
        )
    )


def create_baseline(
    db: Session, wallet_id: int, created_by: str, *, now: datetime | None = None
) -> MoneyBaseline:
    """Snapshot i pandryshueshëm i bilancit ekzistues. Kërkon SMS_MONEY_AUTHORITY=shadow në këtë proces
    (mint-i lokal është i ngrirë PARA snapshot-it) dhe wallet-in të kyçur gjatë leximit. Trafiku
    operacional (reserve/capture/release) mund të vazhdojë pas kësaj. Nuk krijon para."""
    if settings.money_authority != "shadow":
        raise BaselineError("a baseline is created only with SMS_MONEY_AUTHORITY=shadow")
    if not created_by or len(created_by) > 64:
        raise BaselineError("created_by is required (≤ 64 chars)")
    w = wallets.lock_wallet(db, wallet_id)
    ent = db.scalar(select(Enterprise).where(Enterprise.owner_ref == w.owner_ref))
    if ent is None:
        raise BaselineError("wallet owner has no enterprise identity")
    try:
        product_id = sms_product_for(db, ent.id)
    except Unmapped as e:
        raise BaselineError(f"product mapping is not unambiguous: {e}") from e
    avail, held = wallets.balances(db, w.id)
    if held != active_holds_sum(db, w.id):
        raise BaselineError("held balance does not equal the sum of ACTIVE holds; fix first")
    gross = avail + held
    if gross <= 0:
        raise BaselineError("nothing to baseline: gross balance is 0 (no bootstrap needed)")
    max_id = db.scalar(
        select(func.coalesce(func.max(LedgerEntry.id), 0)).where(LedgerEntry.wallet_id == w.id)
    )
    prior = db.scalar(
        select(MoneyBaseline).where(
            MoneyBaseline.wallet_id == w.id, MoneyBaseline.status == BASELINE_ACTIVE
        ).with_for_update()
    )  # fmt: skip
    now = now or utcnow()
    if prior is not None:
        if db.scalar(select(MoneyGrant.grant_id).where(MoneyGrant.baseline_ref == prior.baseline_ref,
                                                       MoneyGrant.status == G_MATCHED)):  # fmt: skip
            raise BaselineError("the active baseline already has a matched bootstrap grant")
        prior.status, prior.superseded_at = BASELINE_SUPERSEDED, now
        db.flush()
    ref = compute_ref(
        wallet_id=w.id, enterprise_id=ent.id, currency=w.currency, product_id=product_id,
        available=avail, held=held, gross=gross, ledger_max_id=max_id, created_at=_utc(now),
    )  # fmt: skip
    b = MoneyBaseline(
        baseline_ref=ref, wallet_id=w.id, enterprise_id=ent.id, currency=w.currency,
        product_id=product_id, available_at_cutover=avail, held_at_cutover=held,
        gross_at_cutover=gross, ledger_max_id=max_id, created_at=now, created_by=created_by,
        status=BASELINE_ACTIVE,
    )  # fmt: skip
    db.add(b)
    db.flush()
    audit._append(db, actor=created_by, role="operator", action="money.baseline_create", target_type="money_baseline",
                  target_id=ref, detail={"wallet_id": w.id, "currency": w.currency, "available": str(avail), "held": str(held),
                                         "gross": str(gross), "superseded": prior.baseline_ref if prior else None})  # fmt: skip
    return b


def get_baseline_by_ref(db: Session, ref: str) -> MoneyBaseline | None:
    return db.scalar(select(MoneyBaseline).where(MoneyBaseline.baseline_ref == ref))


def active_baseline(db: Session, wallet_id: int) -> MoneyBaseline | None:
    return db.scalar(
        select(MoneyBaseline).where(
            MoneyBaseline.wallet_id == wallet_id, MoneyBaseline.status == BASELINE_ACTIVE
        )
    )


def positive_mints_after(db: Session, wallet_id: int, ledger_max_id: int) -> list[LedgerEntry]:
    """Prova durabël: rreshta ledger pas baseline-it që rritin paranë dhe NUK janë GRANT autoritativ."""
    rows = db.scalars(
        select(LedgerEntry).where(LedgerEntry.wallet_id == wallet_id, LedgerEntry.id > ledger_max_id)
        .order_by(LedgerEntry.id)
    )  # fmt: skip
    return [
        e for e in rows
        if e.available_delta + e.held_delta > 0 and e.entry_type != EntryType.GRANT
    ]  # fmt: skip


def orphan_grant_entries(db: Session) -> list[int]:
    """Rreshta GRANT/GRANT_REVERSAL në ledger që s'i referohet asnjë rresht `sms_money_grants`."""
    referenced = set(
        db.scalars(select(MoneyGrant.ledger_entry_id).where(MoneyGrant.ledger_entry_id.isnot(None)))
    )
    referenced |= set(
        db.scalars(
            select(MoneyGrant.reversal_entry_id).where(MoneyGrant.reversal_entry_id.isnot(None))
        )
    )
    ids = db.scalars(
        select(LedgerEntry.id).where(LedgerEntry.entry_type.in_(wallets.AUTHORITATIVE_TYPES))
    )  # fmt: skip
    return [i for i in ids if i not in referenced]
