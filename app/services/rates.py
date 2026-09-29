import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.rates import Rate, RateCard, RateCardVersion, VersionStatus
from app.services.sms_text import count_segments
from app.services.wallet import Conflict, InvalidAmount, NotFound, WalletError, money

E164 = re.compile(r"^\+?[1-9]\d{6,14}$")


class NoRate(WalletError):
    code = "no_rate"


class InvalidNumber(WalletError):
    code = "invalid_number"


def as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def create_card(db: Session, name: str, currency: str) -> RateCard:
    if db.scalar(select(RateCard).where(RateCard.name == name)):
        raise Conflict("rate card name already exists")
    card = RateCard(name=name, currency=currency.upper())
    db.add(card)
    db.flush()
    return card


def new_draft(db: Session, card_id: int) -> RateCardVersion:
    """Draft i ri; kopjon tarifat e versionit të fundit që të ndryshohet vetëm delta."""
    if db.get(RateCard, card_id) is None:
        raise NotFound("rate card not found")
    last = db.scalar(
        select(RateCardVersion)
        .where(RateCardVersion.rate_card_id == card_id)
        .order_by(RateCardVersion.version.desc())
        .limit(1)
    )
    if last and last.status == VersionStatus.DRAFT:
        raise Conflict("a draft already exists")
    v = RateCardVersion(rate_card_id=card_id, version=(last.version + 1) if last else 1)
    db.add(v)
    db.flush()
    if last:
        for r in db.scalars(select(Rate).where(Rate.version_id == last.id)):
            db.add(
                Rate(
                    version_id=v.id,
                    prefix=r.prefix,
                    operator=r.operator,
                    price_per_segment=r.price_per_segment,
                )
            )
        db.flush()
    return v


def _draft(db: Session, version_id: int) -> RateCardVersion:
    v = db.get(RateCardVersion, version_id)
    if v is None:
        raise NotFound("version not found")
    if v.status != VersionStatus.DRAFT:
        raise Conflict("version is published and immutable")
    return v


def set_rate(db: Session, version_id: int, prefix: str, price, operator: str = "") -> Rate:
    _draft(db, version_id)
    if not re.fullmatch(r"[1-9]\d{0,15}", prefix):
        raise InvalidNumber("prefix must be digits without '+' or leading zero")
    price = money(price)
    if price < 0:
        raise InvalidAmount("price must be >= 0")
    r = db.scalar(
        select(Rate).where(
            Rate.version_id == version_id, Rate.prefix == prefix, Rate.operator == operator
        )
    )
    if r:
        r.price_per_segment = price
    else:
        r = Rate(version_id=version_id, prefix=prefix, operator=operator, price_per_segment=price)
        db.add(r)
    db.flush()
    return r


def publish(
    db: Session, version_id: int, effective_from: datetime, now: datetime | None = None
) -> RateCardVersion:
    """effective_from duhet të jetë në të ardhmen dhe pas versionit të mëparshëm, që
    asnjë çmim i kaluar të mos rishkruhet."""
    v = _draft(db, version_id)
    now = as_utc(now or datetime.now(UTC))
    eff = as_utc(effective_from)
    if eff < now:
        raise Conflict("effective_from must not be in the past")
    if not db.scalar(select(func.count()).select_from(Rate).where(Rate.version_id == v.id)):
        raise Conflict("cannot publish an empty version")
    prev = db.scalar(
        select(func.max(RateCardVersion.effective_from)).where(
            RateCardVersion.rate_card_id == v.rate_card_id,
            RateCardVersion.status == VersionStatus.PUBLISHED,
        )
    )
    if prev is not None and eff <= as_utc(prev):
        raise Conflict("effective_from must be after the previous published version")
    v.status = VersionStatus.PUBLISHED
    v.effective_from = eff
    db.flush()
    return v


def active_version(db: Session, card_id: int, at: datetime) -> RateCardVersion:
    v = db.scalar(
        select(RateCardVersion)
        .where(
            RateCardVersion.rate_card_id == card_id,
            RateCardVersion.status == VersionStatus.PUBLISHED,
            RateCardVersion.effective_from <= as_utc(at),
        )
        .order_by(RateCardVersion.effective_from.desc())
        .limit(1)
    )
    if v is None:
        raise NoRate("no published rate card version effective at that time")
    return v


def find_rate(db: Session, version_id: int, number: str, operator: str = "") -> Rate:
    if not E164.match(number):
        raise InvalidNumber("number must be E.164")
    digits = number.lstrip("+")
    prefixes = [digits[:i] for i in range(1, len(digits) + 1)]
    ops = {""} | ({operator} if operator else set())
    candidates = db.scalars(
        select(Rate).where(
            Rate.version_id == version_id, Rate.prefix.in_(prefixes), Rate.operator.in_(ops)
        )
    ).all()
    if not candidates:
        raise NoRate("no rate for destination")
    # prefiksi më i gjatë fiton; brenda tij, tarifa e operatorit fiton mbi të përgjithshmen
    return max(candidates, key=lambda r: (len(r.prefix), r.operator != ""))


@dataclass(frozen=True)
class Quote:
    card_id: int
    version_id: int
    rate_id: int
    currency: str
    encoding: str
    segments: int
    unit_price: Decimal
    total: Decimal


def quote(
    db: Session,
    card_id: int,
    number: str,
    text: str,
    at: datetime | None = None,
    operator: str = "",
) -> Quote:
    card = db.get(RateCard, card_id)
    if card is None:
        raise NotFound("rate card not found")
    v = active_version(db, card_id, at or datetime.now(UTC))
    rate = find_rate(db, v.id, number, operator)
    try:
        enc, segments = count_segments(text)
    except ValueError as e:
        raise InvalidAmount(str(e)) from e
    total = money(rate.price_per_segment * segments)
    return Quote(
        card.id, v.id, rate.id, card.currency, enc, segments, rate.price_per_segment, total
    )
