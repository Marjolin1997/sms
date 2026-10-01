"""Rate cards me versione. Një version i publikuar është i pandryshueshëm; çmimi i një
mesazhi ngrin (version_id, rate_id) në kohën e dërgimit, prandaj ndryshimi i tarifës
nuk prek mesazhet e kaluara."""

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.timeutil import utcnow
from app.models.wallet import MONEY

PK = BigInteger().with_variant(Integer, "sqlite")


class RateCard(Base):
    __tablename__ = "sms_rate_cards"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VersionStatus(enum.StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"


class RateCardVersion(Base):
    __tablename__ = "sms_rate_card_versions"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    rate_card_id: Mapped[int] = mapped_column(ForeignKey("sms_rate_cards.id"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[VersionStatus] = mapped_column(
        Enum(VersionStatus, native_enum=False, length=16), default=VersionStatus.DRAFT
    )
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("rate_card_id", "version"),
        Index("ix_sms_rcv_card_effective", "rate_card_id", "status", "effective_from"),
    )


class Rate(Base):
    __tablename__ = "sms_rates"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("sms_rate_card_versions.id"))
    prefix: Mapped[str] = mapped_column(String(16))  # shifra pa "+", p.sh. 35569
    # Operatori (MCCMNC) opsional: tarifa më specifike fiton mbi atë të përgjithshme.
    operator: Mapped[str] = mapped_column(String(8), default="")
    price_per_segment: Mapped[Decimal] = mapped_column(MONEY)

    __table_args__ = (
        UniqueConstraint("version_id", "prefix", "operator"),
        CheckConstraint("price_per_segment >= 0", name="price_non_negative"),
        Index("ix_sms_rates_version_prefix", "version_id", "prefix"),
    )


class RateFrozenError(RuntimeError):
    pass


@event.listens_for(Rate, "before_update")
@event.listens_for(Rate, "before_delete")
def _rate_immutable(_mapper, connection, target):
    status = connection.execute(
        RateCardVersion.__table__.select()
        .with_only_columns(RateCardVersion.__table__.c.status)
        .where(RateCardVersion.__table__.c.id == target.version_id)
    ).scalar()
    if status == VersionStatus.PUBLISHED or status == "PUBLISHED":
        raise RateFrozenError("rates of a published version are immutable")
