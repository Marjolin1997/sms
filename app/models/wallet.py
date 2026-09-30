"""Wallet + ledger.

Rregulla të hekurta:
- sms_ledger_entries është append-only (asnjë UPDATE/DELETE; ruhet nga ORM guard dhe,
  në MySQL/MariaDB, nga triggers në migrim).
- Shumat janë Decimal/NUMERIC, kurrë float.
- Bilanci = balance_after i rreshtit të fundit; verifikohet me SUM(delta).
"""

import enum
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy import false as sa_false
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.tenant import TenantOwned

MONEY = Numeric(20, 6)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Wallet(TenantOwned, Base):
    """Rreshti që kyçet (SELECT ... FOR UPDATE) për të serializuar lëvizjet e parave.
    Nuk mban balancë; balanca jeton vetëm në ledger."""

    __tablename__ = "sms_wallets"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    # Lidhja me llogarinë/klientin ekzistues të omnichannel (pa FK, vetëm lexim).
    owner_ref: Mapped[str] = mapped_column(String(64), index=True)
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Njoftim kur balanca e disponueshme bie nën këtë prag (event wallet.low_balance, një herë
    # për çdo rënie; rifutet kur balanca ngrihet sërish mbi prag).
    low_balance_threshold: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    low_balance_notified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa_false()
    )

    __table_args__ = (UniqueConstraint("owner_ref", "currency"),)


class EntryType(str, enum.Enum):
    TOPUP = "topup"
    HOLD = "hold"
    CAPTURE = "capture"
    RELEASE = "release"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"
    INVOICE = "invoice"  # pagesë fature nga wallet (debit)


class LedgerEntry(Base):
    __tablename__ = "sms_ledger_entries"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    wallet_id: Mapped[int] = mapped_column(ForeignKey("sms_wallets.id"))
    entry_type: Mapped[EntryType] = mapped_column(Enum(EntryType, native_enum=False, length=16))
    available_delta: Mapped[Decimal] = mapped_column(MONEY)
    held_delta: Mapped[Decimal] = mapped_column(MONEY)
    available_after: Mapped[Decimal] = mapped_column(MONEY)
    held_after: Mapped[Decimal] = mapped_column(MONEY)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    ref_type: Mapped[str | None] = mapped_column(String(32))
    ref_id: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("wallet_id", "idempotency_key"),
        Index("ix_sms_ledger_entries_wallet_id_id", "wallet_id", "id"),
        CheckConstraint("available_after >= 0", name="available_non_negative"),
        CheckConstraint("held_after >= 0", name="held_non_negative"),
    )


class HoldStatus(str, enum.Enum):
    ACTIVE = "active"
    CAPTURED = "captured"
    RELEASED = "released"


class Hold(Base):
    """Rezervim i shumës për një mesazh (ose grup). Gjendja ndryshon një herë:
    ACTIVE -> CAPTURED | RELEASED (kjo tabelë nuk është ledger)."""

    __tablename__ = "sms_holds"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    wallet_id: Mapped[int] = mapped_column(ForeignKey("sms_wallets.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    captured_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal(0))
    status: Mapped[HoldStatus] = mapped_column(
        Enum(HoldStatus, native_enum=False, length=16), default=HoldStatus.ACTIVE
    )
    reference: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("wallet_id", "reference"),
        CheckConstraint("amount > 0", name="amount_positive"),
    )


class TopupMethod(str, enum.Enum):
    CASH = "cash"
    ELECTRONIC = "electronic"


class TopupStatus(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class Topup(Base):
    __tablename__ = "sms_topups"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    wallet_id: Mapped[int] = mapped_column(ForeignKey("sms_wallets.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY)
    method: Mapped[TopupMethod] = mapped_column(Enum(TopupMethod, native_enum=False, length=16))
    status: Mapped[TopupStatus] = mapped_column(
        Enum(TopupStatus, native_enum=False, length=16), default=TopupStatus.PENDING
    )
    # Reference e jashtme (fatura/transaksioni i gateway-t); unike që të mos numërohet dy herë.
    external_ref: Mapped[str | None] = mapped_column(String(128), unique=True)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (CheckConstraint("amount > 0", name="amount_positive"),)


class LedgerImmutableError(RuntimeError):
    pass


@event.listens_for(LedgerEntry, "before_update")
def _no_update(*_):
    raise LedgerImmutableError("ledger entries are append-only")


@event.listens_for(LedgerEntry, "before_delete")
def _no_delete(*_):
    raise LedgerImmutableError("ledger entries are append-only")
