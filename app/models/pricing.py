"""M9-e: cache lokal i çmimeve Central (`cp.pricing.v1`) + krahasimet shadow. Vetëm skema; logjika te `app.services.pricing*`.

Cache = rreshta të pandryshueshëm me identitetet UUID të Central + një rresht gjendjeje (`sms_pricing_state`) që pikon te
snapshot-i aktiv. Aplikimi i një snapshot-i është NJË transaksion: rreshtat e rinj + kalimi i pointer-it commit-ohen bashkë
(lexuesit shohin ose snapshot-in e vjetër të plotë ose të riun të plotë; kurrë gjysmë version). Versioni/rregullat s'ndryshojnë
kurrë; i vetmi ndryshim i lejuar është `active → retired` i një versioni. Tabelat ligjëruese `sms_rate_*` mbeten (cache/rollback
deri te M13): kjo NUK është burim i dytë i së vërtetës nën `central`.
"""

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
    Text,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.timeutil import utcnow
from app.models.rates import PK

PRICE = Numeric(20, 6)
CLASSIFICATIONS = ("match", "missing_rule", "currency_mismatch", "unit_price_mismatch", "total_mismatch",
                   "precedence_mismatch", "version_missing", "segments_mismatch")  # fmt: skip


class PricingState(Base):
    __tablename__ = "sms_pricing_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    active_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    epoch: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    revision: Mapped[int | None] = mapped_column(BigInteger)
    authorization_generation: Mapped[int | None] = mapped_column(BigInteger)
    snapshot_hash: Mapped[str | None] = mapped_column(String(64))
    first_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)


class PricingSnapshot(Base):
    __tablename__ = "sms_pricing_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    epoch: Mapped[uuid.UUID] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(BigInteger)
    authorization_generation: Mapped[int] = mapped_column(BigInteger)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("epoch", "revision", "authorization_generation", "snapshot_hash",
                                       name="uq_sms_pricing_snapshots_identity"),)  # fmt: skip


class PricingBook(Base):
    __tablename__ = "sms_pricing_books"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)  # book_id i Central
    code: Mapped[str] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PricingVersion(Base):
    __tablename__ = "sms_pricing_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)  # version_id i Central
    book_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sms_pricing_books.id"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(8))  # active | retired
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64))
    rule_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("book_id", "version", name="uq_sms_pricing_versions_book_version"),
        Index("ix_sms_pricing_versions_book_effective", "book_id", "effective_from"),
        CheckConstraint("status in ('active', 'retired')", name="status"),
    )


class PricingRule(Base):
    __tablename__ = "sms_pricing_rules"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)  # rule_id i Central
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sms_pricing_versions.id"))
    channel: Mapped[str] = mapped_column(String(8))
    prefix: Mapped[str] = mapped_column(String(16))
    operator: Mapped[str] = mapped_column(String(8))
    unit_price: Mapped[Decimal] = mapped_column(PRICE)

    __table_args__ = (
        UniqueConstraint(
            "version_id", "channel", "prefix", "operator", name="uq_sms_pricing_rules_scope"
        ),
        Index("ix_sms_pricing_rules_lookup", "version_id", "channel", "prefix"),
        CheckConstraint("unit_price >= 0", name="price_non_negative"),
        CheckConstraint("channel in ('sms', 'email')", name="channel"),
    )


class PricingAssignment(Base):
    """Caktimet e një snapshot-i (rreshta per snapshot: një enterprise i hequr nga autorizimi s'çmohet më)."""

    __tablename__ = "sms_pricing_assignments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sms_pricing_snapshots.id"))
    assignment_id: Mapped[uuid.UUID] = mapped_column(Uuid)  # id e Central
    enterprise_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    product_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    book_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sms_pricing_books.id"))
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "snapshot_id", "assignment_id", name="uq_sms_pricing_assignments_snapshot"
        ),
        Index(
            "ix_sms_pricing_assignments_lookup",
            "snapshot_id",
            "enterprise_id",
            "product_id",
            "effective_from",
        ),
    )


class PricingComparison(Base):
    """Krahasim shadow (legacy ↔ Central) per vendim çmimi; pa efekt parash. `kind`: sms | email."""

    __tablename__ = "sms_pricing_comparisons"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(8))
    ref: Mapped[str] = mapped_column(
        String(64)
    )  # public_id i mesazhit / "invoice:<subscription>:<period>"
    classification: Mapped[str] = mapped_column(String(24))
    ok: Mapped[bool] = mapped_column(Boolean)
    legacy_currency: Mapped[str | None] = mapped_column(String(3))
    legacy_unit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    legacy_segments: Mapped[int | None] = mapped_column(Integer)
    legacy_total: Mapped[Decimal | None] = mapped_column(PRICE)
    central_currency: Mapped[str | None] = mapped_column(String(3))
    central_unit_price: Mapped[Decimal | None] = mapped_column(PRICE)
    central_segments: Mapped[int | None] = mapped_column(Integer)
    central_total: Mapped[Decimal | None] = mapped_column(PRICE)
    central_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    detail: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_sms_pricing_comparisons_created", "created_at"),
        CheckConstraint("kind in ('sms', 'email')", name="kind"),
    )


class PricingImmutableError(Exception):
    pass


@event.listens_for(PricingBook, "before_update")
@event.listens_for(PricingRule, "before_update")
@event.listens_for(PricingAssignment, "before_update")
@event.listens_for(PricingSnapshot, "before_update")
@event.listens_for(PricingComparison, "before_update")
@event.listens_for(PricingBook, "before_delete")
@event.listens_for(PricingVersion, "before_delete")
@event.listens_for(PricingRule, "before_delete")
@event.listens_for(PricingAssignment, "before_delete")
@event.listens_for(PricingSnapshot, "before_delete")
def _immutable(*_) -> None:
    raise PricingImmutableError(
        "pricing cache rows are immutable (a new snapshot adds rows; versions only retire)"
    )


@event.listens_for(PricingVersion, "before_update")
def _version_guard(_m, _c, t) -> None:
    attrs = inspect(t).attrs
    changed = [f for f in ("id", "book_id", "version", "effective_from", "content_hash", "rule_count")
               if getattr(attrs, f).history.has_changes()]  # fmt: skip
    old = attrs.status.history.deleted[0] if attrs.status.history.deleted else t.status
    if (
        changed
        or old == "retired"
        and t.status != "retired"
        or t.status not in ("active", "retired")
    ):
        raise PricingImmutableError(
            f"pricing version is immutable except active→retired: {changed}"
        )
