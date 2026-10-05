"""M9-c: gjendja lokale e autoritetit të parave. Vetëm skema; logjika te `app.services.money_authority`
(baseline, mapim, porta) dhe `app.services.money_sync` (aplikues i `cp.money.v1`).

- `sms_money_cursor`: kursori singleton i feed-it të parave (SEPARAT nga `sms_cp_cursor` i cp.v1).
- `sms_money_baselines`: PROVË e pandryshueshme e bilancit ekzistues në çastin e cutover-it
  (gross = available + held). Vetëm `status`/`superseded_at` mund të ndryshojnë; PostgreSQL: trigger.
- `sms_money_grants`: çdo grant i marrë nga Central, një rresht per `grant_id` (idempotencë), me statusin
  e aplikimit. NUK është ledger: ledger-i mbetet `sms_ledger_entries`.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.timeutil import utcnow

MONEY = Numeric(20, 6)

BASELINE_ACTIVE, BASELINE_SUPERSEDED = "active", "superseded"

# statuset e një grant-i të marrë
G_APPLIED = "applied"  # kredia u postua (GRANT)
G_MATCHED = "matched_to_existing_balance"  # bootstrap: përputhje provenance, delta = 0
G_DEFERRED = "deferred_shadow"  # marrë në SHADOW: e regjistruar, NUK kreditohet ende
G_MISMATCH = "baseline_mismatch"  # bootstrap që s'përputhet: fail-closed, pa mutacion
G_UNMAPPED = "unmapped"  # produkt/wallet pa mapim të vetëm e të qartë: pa mutacion
G_REVERSED = "reversed"  # GRANT_REVERSAL u postua
G_VOIDED = "voided_before_apply"  # reversal para se kredia të ketë ekzistuar operacionalisht
G_RECON = "reconciliation_required"  # reversal i pazbatueshëm në mënyrë të sigurt: pa mutacion
GRANT_STATUSES = (
    G_APPLIED, G_MATCHED, G_DEFERRED, G_MISMATCH, G_UNMAPPED, G_REVERSED, G_VOIDED, G_RECON,
)  # fmt: skip
UNRESOLVED = (G_MISMATCH, G_UNMAPPED, G_RECON)  # bllokon readiness


class MoneyCursor(Base):
    __tablename__ = "sms_money_cursor"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    epoch: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # NULL = ende pa inicializim
    authorization_generation: Mapped[int | None] = mapped_column(BigInteger)
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ngjarje e keqe/konflikt: kursori NUK përparon; operatori e sheh këtu dhe në readiness
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)


class MoneyBaseline(Base):
    __tablename__ = "sms_money_baselines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    baseline_ref: Mapped[str] = mapped_column(
        String(64), unique=True
    )  # sha256 hex, i riproduktueshëm
    wallet_id: Mapped[int] = mapped_column(ForeignKey("sms_wallets.id"), index=True)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    currency: Mapped[str] = mapped_column(String(3))
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid)  # produkti SMS i Central (nga entitlement)
    available_at_cutover: Mapped[Decimal] = mapped_column(MONEY)
    held_at_cutover: Mapped[Decimal] = mapped_column(MONEY)
    gross_at_cutover: Mapped[Decimal] = mapped_column(MONEY)
    ledger_max_id: Mapped[int] = mapped_column(BigInteger)  # 0 = ledger bosh
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(64))
    # i vetmi lifecycle: `superseded` kur zëvendësohet para se bootstrap-i të përputhet
    status: Mapped[str] = mapped_column(String(16), default=BASELINE_ACTIVE)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("gross_at_cutover = available_at_cutover + held_at_cutover", name="gross"),
        CheckConstraint("available_at_cutover >= 0 AND held_at_cutover >= 0", name="non_negative"),
        CheckConstraint("ledger_max_id >= 0", name="ledger_max_id"),
        CheckConstraint("status in ('active', 'superseded')", name="status"),
        CheckConstraint(
            "(status = 'active' AND superseded_at IS NULL) OR "
            "(status = 'superseded' AND superseded_at IS NOT NULL)",
            name="status_consistency",
        ),
        Index(
            "uq_sms_money_baselines_active_wallet", "wallet_id", unique=True,
            postgresql_where=text("status = 'active'"), sqlite_where=text("status = 'active'"),
        ),
    )  # fmt: skip


BASELINE_FROZEN = (
    "baseline_ref", "wallet_id", "enterprise_id", "currency", "product_id", "available_at_cutover",
    "held_at_cutover", "gross_at_cutover", "ledger_max_id", "created_at", "created_by",
)  # fmt: skip


class MoneyGrant(Base):
    __tablename__ = "sms_money_grants"

    grant_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    currency: Mapped[str] = mapped_column(String(3))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    purpose: Mapped[str] = mapped_column(String(16))
    baseline_ref: Mapped[str | None] = mapped_column(String(64))
    wallet_id: Mapped[int | None] = mapped_column(ForeignKey("sms_wallets.id"))
    status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(String(500))  # arsyeja e statusit të pazgjidhur
    issued_seq: Mapped[int] = mapped_column(BigInteger)
    issued_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    issued_payload_hash: Mapped[str] = mapped_column(String(64))
    reversed_seq: Mapped[int | None] = mapped_column(BigInteger)
    reversed_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, unique=True)
    ledger_entry_id: Mapped[int | None] = mapped_column(ForeignKey("sms_ledger_entries.id"))
    reversal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("sms_ledger_entries.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("purpose in ('standard', 'bootstrap')", name="purpose"),
        CheckConstraint(
            "status in ('" + "', '".join(GRANT_STATUSES) + "')", name="status"
        ),
        Index("ix_sms_money_grants_status", "status"),
        # një baseline = një autorizim bootstrap i PËRPUTHUR (përsëritja/konflikti → mismatch)
        Index(
            "uq_sms_money_grants_matched_baseline", "baseline_ref", unique=True,
            postgresql_where=text("status = 'matched_to_existing_balance'"),
            sqlite_where=text("status = 'matched_to_existing_balance'"),
        ),
        UniqueConstraint("issued_seq", name="uq_sms_money_grants_issued_seq"),
    )  # fmt: skip


class MoneyAuthorityImmutableError(Exception):
    pass


@event.listens_for(MoneyBaseline, "before_delete")
@event.listens_for(MoneyGrant, "before_delete")
def _no_delete(*_) -> None:
    raise MoneyAuthorityImmutableError("money authority rows are never deleted")


@event.listens_for(MoneyBaseline, "before_update")
def _baseline_frozen(_m, _c, target) -> None:
    attrs = inspect(target).attrs
    changed = [f for f in BASELINE_FROZEN if getattr(attrs, f).history.has_changes()]
    if changed:
        raise MoneyAuthorityImmutableError(f"baseline snapshot fields are immutable: {changed}")


GRANT_FROZEN = (
    "grant_id", "enterprise_id", "account_id", "product_id", "currency", "amount", "purpose",
    "baseline_ref", "issued_seq", "issued_event_id", "issued_payload_hash", "created_at",
)  # fmt: skip


@event.listens_for(MoneyGrant, "before_update")
def _grant_frozen(_m, _c, target) -> None:
    attrs = inspect(target).attrs
    changed = [f for f in GRANT_FROZEN if getattr(attrs, f).history.has_changes()]
    if changed:
        raise MoneyAuthorityImmutableError(f"grant identity fields are immutable: {changed}")
