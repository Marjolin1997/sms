"""Autoriteti tregtar i parave në Central (M9-b). VETËM Central: asnjë sinkronizim drejt Enterprise ende
(M9-c); asnjë gateway pagese; asnjë çmim/faturim.

KONTABILITETI (një formulë kanonike, `services/credit_accounts.totals`):
    funds              = Σ payment_credit + Σ manual_credit_adjustment − Σ manual_debit_adjustment
    outstanding_grants = Σ grant_issued − Σ grant_reversal
    available_to_grant = funds − outstanding_grants           (invariant: ≥ 0 gjithmonë)
Pagesa e miratuar krijon fonde TREGTARE një herë; granti NUK krijon para: vetëm i alokon (lëviz nga
"grantable" te "granted"). Pra pagesë 100 + grant 40 ⇒ funds=100, granted=40, available=60 (jo 140).
Ledger-i është burimi i së vërtetës; asnjë kolonë balance e ndryshueshme ekziston. Shumat janë
pozitive (`amount > 0`); drejtimi vjen nga `entry_type`. NUMERIC(20,6), kurrë float.

V1: një monedhë për (enterprise, produkt): UNIQUE(enterprise_id, product_id) në DB; monedha e llogarisë
është e pandryshueshme (FK të përbëra (id, currency) nga fëmijët e bëjnë të pamundur edhe me SQL).
Immutability: ledger/events pa UPDATE/DELETE (ORM + trigger PG); fushat e parave të pagesës/grantit/
llogarisë të ngrira (ORM + trigger PG); asnjë fshirje fizike.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from apps.central.core.db import Base
from apps.central.core.timeutil import utcnow
from apps.central.models.product import ImmutableError

MONEY = Numeric(20, 6)

# --- llogaria ---------------------------------------------------------------------------------------------
ACCOUNT_ACTIVE, ACCOUNT_SUSPENDED = "active", "suspended"
ACCOUNT_STATUSES = (ACCOUNT_ACTIVE, ACCOUNT_SUSPENDED)

# --- ledger -------------------------------------------------------------------------------------------------
PAYMENT_CREDIT = "payment_credit"
MANUAL_CREDIT = "manual_credit_adjustment"
MANUAL_DEBIT = "manual_debit_adjustment"
GRANT_ISSUED = "grant_issued"
GRANT_REVERSAL = "grant_reversal"
ENTRY_TYPES = (PAYMENT_CREDIT, MANUAL_CREDIT, MANUAL_DEBIT, GRANT_ISSUED, GRANT_REVERSAL)
FUNDS_UP = (PAYMENT_CREDIT, MANUAL_CREDIT)  # rrisin fondet tregtare
FUNDS_DOWN = (MANUAL_DEBIT,)  # ulin fondet tregtare
GRANTED_UP = (GRANT_ISSUED,)  # rrisin të alokuarat (outstanding)
GRANTED_DOWN = (GRANT_REVERSAL,)  # ulin të alokuarat

# --- pagesat -----------------------------------------------------------------------------------------------
PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"

# --- grant-et ----------------------------------------------------------------------------------------------
GRANT_ACTIVE, GRANT_REVERSED = "active", "reversed"

# --- ngjarjet ----------------------------------------------------------------------------------------------
EVENT_GRANT_ISSUED = "credit_grant.issued"
EVENT_GRANT_REVERSED = "credit_grant.reversed"
EVENT_TYPES = (EVENT_GRANT_ISSUED, EVENT_GRANT_REVERSED)

_CURRENCY_OK = "length(currency) = 3 AND currency = upper(currency)"


class MoneyImmutableError(ImmutableError):
    pass


class MoneySequence(Base):
    """Numërues singleton TRANSAKSIONAL (si `sync_sequence`): rresht i kyçur deri në commit ⇒ `seq` N
    është i dukshëm para N+1 (kursor i sigurt për feed-in e M9-c). Rollback heq edhe rritjen."""

    __tablename__ = "money_sequence"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    epoch: Mapped[uuid.UUID] = mapped_column(Uuid, default=uuid.uuid4)
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")

    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("last_seq >= 0", name="last_seq_non_negative"),
    )


class CreditAccount(Base):
    __tablename__ = "credit_accounts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("enterprises.id", ondelete="RESTRICT")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("products.id", ondelete="RESTRICT")
    )
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(16), default=ACCOUNT_ACTIVE, server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        # V1: një monedhë për enterprise/produkt (DB invariant, jo vetëm shërbim)
        UniqueConstraint(
            "enterprise_id", "product_id", name="uq_credit_accounts_enterprise_product"
        ),
        UniqueConstraint("id", "currency", name="uq_credit_accounts_id_currency"),
        UniqueConstraint(
            "id", "enterprise_id", "currency", name="uq_credit_accounts_id_enterprise_currency"
        ),
        UniqueConstraint(
            "id", "enterprise_id", "product_id", "currency", name="uq_credit_accounts_scope"
        ),
        CheckConstraint(_CURRENCY_OK, name="currency_format"),
        CheckConstraint("status in ('active', 'suspended')", name="status"),
    )


class CommercialLedgerEntry(Base):
    __tablename__ = "commercial_ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    seq: Mapped[int] = mapped_column(BigInteger, unique=True)  # nga `money_sequence`
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    currency: Mapped[str] = mapped_column(String(3))
    entry_type: Mapped[str] = mapped_column(String(32))
    amount: Mapped[Decimal] = mapped_column(MONEY)  # gjithmonë > 0; drejtimi = entry_type
    source_type: Mapped[str] = mapped_column(String(32))  # payment | credit_grant | adjustment
    source_id: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # p.sh. grant_id/payment_id
    reason: Mapped[str | None] = mapped_column(String(500))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    actor_label: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "currency"],
            ["credit_accounts.id", "credit_accounts.currency"],
            name="fk_commercial_ledger_entries_account_currency",
            ondelete="RESTRICT",
        ),
        # një efekt për burim semantik: një pagesë ⇒ një kredit; një grant ⇒ një issue/një reversal
        UniqueConstraint(
            "entry_type", "source_type", "source_id", name="uq_commercial_ledger_entries_source"
        ),
        Index("ix_commercial_ledger_entries_account_seq", "account_id", "seq"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(
            "entry_type in ('payment_credit', 'manual_credit_adjustment', "
            "'manual_debit_adjustment', 'grant_issued', 'grant_reversal')",
            name="entry_type",
        ),
        CheckConstraint(
            "(actor_user_id IS NOT NULL AND actor_label IS NULL) OR "
            "(actor_user_id IS NULL AND actor_label IS NOT NULL)",
            name="actor_semantics",
        ),
        # rregullimet manuale dhe reversal-i kërkojnë arsye
        CheckConstraint(
            "entry_type NOT IN ('manual_credit_adjustment', 'manual_debit_adjustment', "
            "'grant_reversal') OR (reason IS NOT NULL AND length(trim(reason)) > 0)",
            name="reason_required",
        ),
    )


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    currency: Mapped[str] = mapped_column(String(3))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    # burimi i pagesës (jo vendor): `manual` | `import` | ...; (source, external_reference) i skopuar
    source: Mapped[str] = mapped_column(String(32), default="manual", server_default="manual")
    external_reference: Mapped[str | None] = mapped_column(String(128))
    note: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(16), default=PENDING, server_default=PENDING)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_by_label: Mapped[str | None] = mapped_column(String(64))  # aktor sistemi (import)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    rejection_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "enterprise_id", "currency"],
            ["credit_accounts.id", "credit_accounts.enterprise_id", "credit_accounts.currency"],
            name="fk_payments_account_scope",
            ondelete="RESTRICT",
        ),
        Index(
            "uq_payments_source_external_reference",
            "source",
            "external_reference",
            unique=True,
            postgresql_where=text("external_reference IS NOT NULL"),
            sqlite_where=text("external_reference IS NOT NULL"),
        ),
        Index("ix_payments_account_status", "account_id", "status"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("status in ('pending', 'approved', 'rejected')", name="status"),
        CheckConstraint(
            "(created_by_id IS NOT NULL AND created_by_label IS NULL) OR "
            "(created_by_id IS NULL AND created_by_label IS NOT NULL)",
            name="creator_semantics",
        ),
        CheckConstraint(
            "(status = 'pending' AND approved_at IS NULL AND approved_by_id IS NULL AND "
            "rejected_at IS NULL AND rejected_by_id IS NULL AND rejection_reason IS NULL) OR "
            "(status = 'approved' AND approved_at IS NOT NULL AND approved_by_id IS NOT NULL AND "
            "rejected_at IS NULL AND rejected_by_id IS NULL AND rejection_reason IS NULL) OR "
            "(status = 'rejected' AND rejected_at IS NOT NULL AND rejected_by_id IS NOT NULL AND "
            "approved_at IS NULL AND approved_by_id IS NULL AND rejection_reason IS NOT NULL "
            "AND length(trim(rejection_reason)) > 0)",
            name="status_consistency",
        ),
        # maker-checker: miratuesi ≠ krijuesi njeri (kur krijuesi është njeri)
        CheckConstraint(
            "approved_by_id IS NULL OR created_by_id IS NULL OR approved_by_id <> created_by_id",
            name="maker_checker",
        ),
    )


class CreditGrant(Base):
    """Alokim i pandryshueshëm i kredisë tregtare drejt planit operacional. `id` = `grant_id`
    kanonik. Korrigjim = reversal + grant i ri (shuma s'ndryshohet kurrë)."""

    __tablename__ = "credit_grants"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    currency: Mapped[str] = mapped_column(String(3))
    amount: Mapped[Decimal] = mapped_column(MONEY)
    status: Mapped[str] = mapped_column(String(16), default=GRANT_ACTIVE, server_default="active")
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    # lidhje informuese (jo kufizim): një pagesë mund të japë shumë grant-e
    source_payment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("payments.id", ondelete="RESTRICT")
    )
    note: Mapped[str | None] = mapped_column(String(500))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_by_label: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    reversal_reason: Mapped[str | None] = mapped_column(String(500))

    __table_args__ = (
        ForeignKeyConstraint(
            ["account_id", "enterprise_id", "product_id", "currency"],
            [
                "credit_accounts.id",
                "credit_accounts.enterprise_id",
                "credit_accounts.product_id",
                "credit_accounts.currency",
            ],
            name="fk_credit_grants_account_scope",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("account_id", "idempotency_key", name="uq_credit_grants_idempotency"),
        Index("ix_credit_grants_account_status", "account_id", "status"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("status in ('active', 'reversed')", name="status"),
        CheckConstraint(
            "(created_by_id IS NOT NULL AND created_by_label IS NULL) OR "
            "(created_by_id IS NULL AND created_by_label IS NOT NULL)",
            name="creator_semantics",
        ),
        CheckConstraint(
            "(status = 'active' AND reversed_at IS NULL AND reversed_by_id IS NULL AND "
            "reversal_reason IS NULL) OR "
            "(status = 'reversed' AND reversed_at IS NOT NULL AND reversed_by_id IS NOT NULL AND "
            "reversal_reason IS NOT NULL AND length(trim(reversal_reason)) > 0)",
            name="status_consistency",
        ),
    )


class MoneyEvent(Base):
    """Ditar i pandryshueshëm i ngjarjeve që do të bëhen feed `cp.money.v1` në M9-c. `payload` është
    snapshot i NGRIRË në çastin e ngjarjes (jo i rindërtuar nga tabela të ndryshueshme). Vetëm
    grant issued/reversed (pagesat mbeten të brendshme të Central)."""

    __tablename__ = "money_events"

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(32))
    enterprise_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("enterprises.id", ondelete="RESTRICT")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("credit_accounts.id", ondelete="RESTRICT")
    )
    entity_type: Mapped[str] = mapped_column(String(32), default="credit_grant")
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("event_type", "entity_id", name="uq_money_events_entity"),
        Index("ix_money_events_enterprise_seq", "enterprise_id", "seq"),
        CheckConstraint("seq > 0", name="seq_positive"),
        CheckConstraint(
            "event_type in ('credit_grant.issued', 'credit_grant.reversed')", name="event_type"
        ),
    )


# --- mbrojtjet ORM (shtresa e dytë; e para është triggeri PG) ---------------------------------------


@event.listens_for(CommercialLedgerEntry, "before_update")
@event.listens_for(CommercialLedgerEntry, "before_delete")
@event.listens_for(MoneyEvent, "before_update")
@event.listens_for(MoneyEvent, "before_delete")
def _append_only(*_) -> None:
    raise MoneyImmutableError("commercial ledger and money events are append-only")


@event.listens_for(CreditAccount, "before_delete")
@event.listens_for(Payment, "before_delete")
@event.listens_for(CreditGrant, "before_delete")
def _no_delete(*_) -> None:
    raise MoneyImmutableError("money rows are never deleted; use state transitions")


def _frozen(target, fields: tuple[str, ...], what: str) -> None:
    attrs = inspect(target).attrs
    changed = [f for f in fields if getattr(attrs, f).history.has_changes()]
    if changed:
        raise MoneyImmutableError(f"{what} fields are immutable: {changed}")


@event.listens_for(CreditAccount, "before_update")
def _account_frozen(_m, _c, target) -> None:
    _frozen(target, ("id", "enterprise_id", "product_id", "currency", "created_at"), "account")


PAYMENT_FROZEN = ("id", "enterprise_id", "account_id", "currency", "amount", "source",
                  "external_reference", "note", "created_by_id", "created_by_label",
                  "created_at")  # fmt: skip
GRANT_FROZEN = ("id", "account_id", "enterprise_id", "product_id", "currency", "amount",
                "idempotency_key", "request_hash", "source_payment_id", "note",
                "created_by_id", "created_by_label", "created_at")  # fmt: skip


@event.listens_for(Payment, "before_update")
def _payment_frozen(_m, _c, target) -> None:
    _frozen(target, PAYMENT_FROZEN, "payment")


@event.listens_for(CreditGrant, "before_update")
def _grant_frozen(_m, _c, target) -> None:
    _frozen(target, GRANT_FROZEN, "grant")
