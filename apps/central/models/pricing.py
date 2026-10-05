"""M9-e: autoriteti i çmimeve të klientit në Central — libra çmimesh, versione të pandryshueshme, rregulla, caktime.

Modeli i ri riperdor konceptet e Enterprise (RateCard → version → rate) por me identitete UUID të qëndrueshme:
- `PriceBook` (kod + monedhë e vetme; pa FX) ↔ `RateCard`;
- `PriceVersion`: draft → active → retired; aktivizimi fikson `effective_from` + `content_hash`; një version i aktivizuar
  ose i tërhequr është i pandryshueshëm (korrigjim = version i ri). `retired` s'zgjidhet kurrë më;
- `PriceRule`: (channel, prefix, operator) UNIQUE per version ⇒ asnjë precedencë e paqartë; kanali email ka vetëm
  një rregull (çmimi i overage per email);
- `PriceAssignment`: (enterprise, product) → libër, me `effective_from` (histori e pandryshueshme);
- `PricingSequence`: numëruesi `revision` (rritet në çdo aktivizim/tërheqje/caktim) që sinkronizimi përdor si version snapshot-i.
Pa çmim kosto të provider-it (customer_price ≠ provider_cost; s'modelohet në M9-e)."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
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

PRICE = Numeric(20, 6)
V_DRAFT, V_ACTIVE, V_RETIRED = "draft", "active", "retired"


class PricingImmutableError(ImmutableError):
    pass


class PricingSequence(Base):
    __tablename__ = "pricing_sequence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    epoch: Mapped[uuid.UUID] = mapped_column(Uuid, default=uuid.uuid4)
    revision: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")

    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)


class PriceBook(Base):
    __tablename__ = "price_books"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    currency: Mapped[str] = mapped_column(String(3))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint("length(currency) = 3 AND currency = upper(currency)", name="currency"),
    )


class PriceVersion(Base):
    __tablename__ = "price_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    price_book_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("price_books.id", ondelete="RESTRICT")
    )
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(8), default=V_DRAFT)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    imported: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    activated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    retired_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    retire_reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("price_book_id", "version", name="uq_price_versions_book_version"),
        UniqueConstraint(
            "price_book_id", "effective_from", name="uq_price_versions_book_effective"
        ),
        Index(
            "uq_price_versions_one_draft",
            "price_book_id",
            unique=True,
            postgresql_where=text("status = 'draft'"),
            sqlite_where=text("status = 'draft'"),
        ),  # fmt: skip
        CheckConstraint("status in ('draft', 'active', 'retired')", name="status"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "(status = 'draft' AND effective_from IS NULL AND content_hash IS NULL AND activated_at IS NULL) OR "
            "(status <> 'draft' AND effective_from IS NOT NULL AND content_hash IS NOT NULL AND activated_at IS NOT NULL)",
            name="status_consistency",
        ),
        CheckConstraint(
            "(status = 'retired' AND retired_at IS NOT NULL AND retire_reason IS NOT NULL) OR "
            "(status <> 'retired' AND retired_at IS NULL AND retire_reason IS NULL)",
            name="retire_consistency",
        ),
    )


class PriceRule(Base):
    __tablename__ = "price_rules"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("price_versions.id", ondelete="RESTRICT")
    )
    channel: Mapped[str] = mapped_column(String(8))
    prefix: Mapped[str] = mapped_column(String(16), default="")
    operator: Mapped[str] = mapped_column(String(8), default="")
    unit_price: Mapped[Decimal] = mapped_column(PRICE)

    __table_args__ = (
        UniqueConstraint(
            "version_id", "channel", "prefix", "operator", name="uq_price_rules_scope"
        ),
        Index("ix_price_rules_version_prefix", "version_id", "channel", "prefix"),
        CheckConstraint("unit_price >= 0", name="price_non_negative"),
        CheckConstraint("channel in ('sms', 'email')", name="channel"),
        CheckConstraint(
            "(channel = 'sms' AND length(prefix) >= 1) OR (channel = 'email' AND prefix = '' AND operator = '')",
            name="scope",
        ),
    )


class PriceAssignment(Base):
    __tablename__ = "price_assignments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("enterprises.id", ondelete="RESTRICT")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("products.id", ondelete="RESTRICT")
    )
    price_book_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("price_books.id", ondelete="RESTRICT")
    )
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "enterprise_id", "product_id", "effective_from", name="uq_price_assignments_scope"
        ),
        Index("ix_price_assignments_enterprise", "enterprise_id", "product_id", "effective_from"),
    )


# --- mbrojtjet ORM (shtresa e dytë; e para është triggeri PG) -----------------------------------------------------


def _frozen(target, fields: tuple[str, ...], what: str) -> None:
    attrs = inspect(target).attrs
    changed = [f for f in fields if getattr(attrs, f).history.has_changes()]
    if changed:
        raise PricingImmutableError(f"{what} fields are immutable: {changed}")


@event.listens_for(PriceBook, "before_update")
def _book_frozen(_m, _c, t) -> None:
    _frozen(t, ("id", "code", "name", "currency", "created_by_id", "created_at"), "price book")


@event.listens_for(PriceBook, "before_delete")
@event.listens_for(PriceVersion, "before_delete")
@event.listens_for(PriceAssignment, "before_delete")
@event.listens_for(PriceAssignment, "before_update")
def _never(*_) -> None:
    raise PricingImmutableError("pricing rows are never deleted; assignments are immutable history")


_VERSION_ALWAYS = ("id", "price_book_id", "version", "created_by_id", "created_at")


@event.listens_for(PriceVersion, "before_update")
def _version_guard(_m, _c, t) -> None:
    _frozen(t, _VERSION_ALWAYS, "price version")
    attrs = inspect(t).attrs
    old_status = attrs.status.history.deleted[0] if attrs.status.history.deleted else t.status
    if (
        old_status != V_DRAFT
    ):  # active/retired: asnjë fushë financiare/identiteti s'ndryshon; vetëm active→retired
        _frozen(
            t,
            ("effective_from", "content_hash", "activated_at", "activated_by_id", "imported"),
            "activated price version",
        )
        if old_status == V_RETIRED or (
            old_status == V_ACTIVE and t.status not in (V_ACTIVE, V_RETIRED)
        ):
            raise PricingImmutableError(
                f"price version status {old_status} is final or cannot go to {t.status}"
            )
    elif t.status not in (V_DRAFT, V_ACTIVE):
        raise PricingImmutableError("a draft can only be activated")


def _version_status(conn, version_id) -> str | None:
    return conn.execute(
        PriceVersion.__table__.select().with_only_columns(PriceVersion.__table__.c.status)
        .where(PriceVersion.__table__.c.id == version_id)
    ).scalar()  # fmt: skip


@event.listens_for(PriceRule, "before_insert")
@event.listens_for(PriceRule, "before_update")
@event.listens_for(PriceRule, "before_delete")
def _rule_guard(_m, conn, t) -> None:
    if _version_status(conn, t.version_id) != V_DRAFT:
        raise PricingImmutableError("rules of an activated/retired price version are immutable")
