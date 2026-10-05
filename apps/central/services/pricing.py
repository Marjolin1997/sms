"""M9-e: domain-i i çmimeve të klientit në Central. Pa commit (tx i thirrësit); vetëm admin njeri (audit-uar).

Lifecycle: `create_book` → `new_draft` → `set_rule`* → `activate(effective_from)` → (opsionale) `retire`. Një version i
aktivizuar është i pandryshueshëm (ORM + trigger PG); korrigjim = draft i ri (kopjon rregullat e versionit të fundit) dhe
version i ri. Aktivizimi/tërheqja/caktimi rrisin `pricing_sequence.revision` (versioni i snapshot-it `cp.pricing.v1`).
Rendi i kyçjeve: (1) `pricing_sequence`, (2) libri, (3) versioni — aktivizime të njëkohshme serializohen; i dyti sheh draft-in
tashmë të aktivizuar dhe kthen no-op/Conflict, kurrë dy versione efektive me të njëjtin `effective_from`.
Pa efekt (no-op) ⇒ pa audit. Pa FX: një libër = një monedhë. Çmimi i klientit ≠ kostoja e provider-it (s'modelohet këtu)."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.central.core.errors import Conflict, Invalid, NotFound
from apps.central.core.timeutil import utcnow
from apps.central.models.enterprise import Enterprise
from apps.central.models.pricing import (
    V_ACTIVE,
    V_DRAFT,
    V_RETIRED,
    PriceAssignment,
    PriceBook,
    PriceRule,
    PriceVersion,
    PricingSequence,
)
from apps.central.models.product import Product
from apps.central.services import audit, money_common
from packages.contracts.control_plane.pricing import v1 as pv

_SEQ = PricingSequence.__table__
RESOURCE_BOOK, RESOURCE_VERSION, RESOURCE_ASSIGNMENT = (
    "price_book",
    "price_version",
    "price_assignment",
)


class NoPrice(Conflict):
    """Asnjë çmim i vlefshëm (pa version/rregull/caktim): fail-closed, kurrë parazgjedhje."""


@dataclass(frozen=True, slots=True)
class PriceQuote:
    book_id: uuid.UUID
    version_id: uuid.UUID
    rule_id: uuid.UUID
    currency: str
    unit_price: Decimal


# --- sekuenca -------------------------------------------------------------------------------------------------


def lock_sequence(db: Session) -> None:
    row = db.execute(select(_SEQ.c.revision).where(_SEQ.c.id == 1).with_for_update()).first()
    if row is None:
        raise RuntimeError("pricing_sequence singleton row is missing (run migrations)")


def bump_revision(db: Session) -> int:
    return int(db.execute(_SEQ.update().where(_SEQ.c.id == 1).values(revision=_SEQ.c.revision + 1)
                          .returning(_SEQ.c.revision)).scalar_one())  # fmt: skip


def read_state(db: Session) -> tuple[uuid.UUID, int]:
    r = db.execute(select(_SEQ.c.epoch, _SEQ.c.revision).where(_SEQ.c.id == 1)).one()
    return r.epoch, int(r.revision)


def _uid(v, what) -> uuid.UUID:
    return money_common.uid(v, what)


def price(value) -> Decimal:
    """Çmim njësie: Decimal/str/int, ≥ 0, ≤ 6 shifra (kurrë float)."""
    if (
        isinstance(value, bool)
        or isinstance(value, float)
        or not isinstance(value, Decimal | str | int)
    ):
        raise Invalid("price must be a Decimal, string or integer (never float)")
    try:
        d = Decimal(value)
    except Exception:  # noqa: BLE001
        raise Invalid("invalid price") from None
    if (
        not d.is_finite()
        or d < 0
        or d != d.quantize(pv.PRICE_QUANT)
        or d > Decimal("9999999999999.999999")
    ):
        raise Invalid("price must be finite, >= 0 and have at most 6 decimal places")
    return d.quantize(pv.PRICE_QUANT)


# --- libra / versione / rregulla ---------------------------------------------------------------------------------


def get_book(db: Session, book_id, *, lock: bool = False) -> PriceBook:
    q = select(PriceBook).where(PriceBook.id == _uid(book_id, "book id"))
    row = db.scalar(q.with_for_update() if lock else q)
    if row is None:
        raise NotFound("price book not found")
    return row


def get_version(db: Session, version_id, *, lock: bool = False) -> PriceVersion:
    q = select(PriceVersion).where(PriceVersion.id == _uid(version_id, "version id"))
    if lock:
        q = q.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(q)
    if row is None:
        raise NotFound("price version not found")
    return row


def create_book(
    db: Session, actor, code: str, name: str, currency: str, *, now: datetime | None = None
) -> PriceBook:
    actor = money_common.admin(actor)
    if not isinstance(code, str) or not pv._CODE.match(code):
        raise Invalid("code must be 2-64 chars of a-z, 0-9, _ or -")
    cur = money_common.currency(currency)
    nm = money_common.optional_text(name, "name") or code
    prior = db.scalar(select(PriceBook).where(PriceBook.code == code))
    if prior is not None:
        if (prior.currency, prior.name) != (cur, nm):
            raise Conflict("a price book with this code already exists with different attributes")
        return prior
    b = PriceBook(
        code=code, name=nm, currency=cur, created_by_id=actor.id, created_at=now or utcnow()
    )
    db.add(b)
    db.flush()
    audit.record(
        db,
        actor,
        "price_book.create",
        RESOURCE_BOOK,
        b.id,
        {"code": code, "currency": cur},
        now=now,
    )
    return b


def new_draft(db: Session, actor, book_id, *, now: datetime | None = None) -> PriceVersion:
    actor = money_common.admin(actor)
    lock_sequence(db)
    book = get_book(db, book_id, lock=True)
    if db.scalar(
        select(PriceVersion.id).where(
            PriceVersion.price_book_id == book.id, PriceVersion.status == V_DRAFT
        )
    ):
        raise Conflict("a draft already exists for this price book")
    last = db.scalar(select(PriceVersion).where(PriceVersion.price_book_id == book.id)
                     .order_by(PriceVersion.version.desc()).limit(1))  # fmt: skip
    v = PriceVersion(price_book_id=book.id, version=(last.version + 1) if last else 1, status=V_DRAFT,
                     created_by_id=actor.id, created_at=now or utcnow())  # fmt: skip
    db.add(v)
    db.flush()
    copied = 0
    if (
        last is not None
    ):  # delta mbi versionin e fundit (kopjohen rregullat; ndryshohet vetëm ndryshimi)
        for r in db.scalars(select(PriceRule).where(PriceRule.version_id == last.id)):
            db.add(
                PriceRule(
                    version_id=v.id,
                    channel=r.channel,
                    prefix=r.prefix,
                    operator=r.operator,
                    unit_price=r.unit_price,
                )
            )
            copied += 1
        db.flush()
    audit.record(db, actor, "price_version.create", RESOURCE_VERSION, v.id,
                 {"book_id": str(book.id), "version": v.version, "copied_rules": copied}, now=now)  # fmt: skip
    return v


def _draft(db: Session, version_id) -> PriceVersion:
    v = get_version(db, version_id, lock=True)
    if v.status != V_DRAFT:
        raise Conflict(
            "price version is not a draft: financial fields are immutable (create a new version)"
        )
    return v


def set_rule(db: Session, actor, version_id, channel: str, unit_price, *, prefix: str = "", operator: str = "",
             now: datetime | None = None) -> tuple[PriceRule, bool]:  # fmt: skip
    """Shton/ndryshon rregull në DRAFT. Pa ndryshim real ⇒ no-op (pa audit). → (rregulla, ndryshoi?)."""
    actor = money_common.admin(actor)
    if channel not in pv.CHANNELS:
        raise Invalid("channel must be sms or email")
    if channel == "sms":
        if not isinstance(prefix, str) or not pv._PREFIX.match(prefix):
            raise Invalid("prefix must be digits without '+' or leading zero")
        if not isinstance(operator, str) or not pv._OPERATOR.match(operator):
            raise Invalid("operator must be digits (MCCMNC) or empty")
    elif prefix or operator:
        raise Invalid("email rules have empty prefix and operator")
    p = price(unit_price)
    _draft(db, version_id)
    vid = _uid(version_id, "version id")
    r = db.scalar(select(PriceRule).where(PriceRule.version_id == vid, PriceRule.channel == channel,
                                          PriceRule.prefix == prefix, PriceRule.operator == operator))  # fmt: skip
    if r is not None and r.unit_price == p:
        return r, False
    old = None if r is None else str(r.unit_price)
    if r is None:
        r = PriceRule(
            version_id=vid, channel=channel, prefix=prefix, operator=operator, unit_price=p
        )
        db.add(r)
    else:
        r.unit_price = p
    db.flush()
    audit.record(db, actor, "price_rule.set", RESOURCE_VERSION, vid,
                 {"channel": channel, "prefix": prefix, "operator": operator, "old": old, "new": str(p)}, now=now)  # fmt: skip
    return r, True


def remove_rule(db: Session, actor, version_id, channel: str, *, prefix: str = "", operator: str = "",
                now: datetime | None = None) -> bool:  # fmt: skip
    actor = money_common.admin(actor)
    _draft(db, version_id)
    vid = _uid(version_id, "version id")
    r = db.scalar(select(PriceRule).where(PriceRule.version_id == vid, PriceRule.channel == channel,
                                          PriceRule.prefix == prefix, PriceRule.operator == operator))  # fmt: skip
    if r is None:
        return False
    db.delete(r)
    db.flush()
    audit.record(db, actor, "price_rule.remove", RESOURCE_VERSION, vid,
                 {"channel": channel, "prefix": prefix, "operator": operator}, now=now)  # fmt: skip
    return True


def rules_of(db: Session, version_id) -> list[dict]:
    rows = db.scalars(
        select(PriceRule).where(PriceRule.version_id == _uid(version_id, "version id"))
    )
    return [{"rule_id": str(r.id), "channel": r.channel, "prefix": r.prefix, "operator": r.operator,
             "unit_price": pv.format_price(r.unit_price)} for r in rows]  # fmt: skip


def activate(db: Session, actor, version_id, effective_from: datetime, *, now: datetime | None = None,
             imported: bool = False) -> PriceVersion:  # fmt: skip
    """draft → active. `effective_from` ≥ tani (përveç `imported`) dhe pas çdo versioni tjetër të librit (renditje e rreptë: asnjë
    version efektiv i dyfishtë). Fikson `content_hash`. Rizbatimi i të njëjtit version me të njëjtin `effective_from` = no-op."""
    actor = money_common.admin(actor)
    now = now or utcnow()
    eff = effective_from if effective_from.tzinfo else effective_from.replace(tzinfo=now.tzinfo)
    lock_sequence(db)
    pre = get_version(db, version_id)
    get_book(db, pre.price_book_id, lock=True)
    v = get_version(db, version_id, lock=True)
    if v.status != V_DRAFT:
        if (
            v.status == V_ACTIVE
            and v.effective_from is not None
            and _same_instant(v.effective_from, eff)
        ):
            return v  # idempotent
        raise Conflict(
            f"price version is already {v.status}; create a new version to change prices"
        )
    rules = rules_of(db, v.id)
    if not rules:
        raise Conflict("cannot activate an empty price version")
    if not imported and eff < now:
        raise Conflict("effective_from must not be in the past")
    top = db.scalar(select(func.max(PriceVersion.effective_from)).where(PriceVersion.price_book_id == v.price_book_id,
                                                                        PriceVersion.status != V_DRAFT))  # fmt: skip
    if top is not None and _utc(eff) <= _utc(top):
        raise Conflict("effective_from must be after the previous version of this price book")
    v.status, v.effective_from, v.content_hash = V_ACTIVE, eff, pv.rules_hash(rules)
    v.activated_at, v.activated_by_id, v.imported = now, actor.id, imported
    db.flush()
    rev = bump_revision(db)
    audit.record(db, actor, "price_version.import" if imported else "price_version.activate", RESOURCE_VERSION, v.id,
                 {"book_id": str(v.price_book_id), "version": v.version, "effective_from": pv.format_ts(eff),
                  "content_hash": v.content_hash, "rules": len(rules), "revision": rev}, now=now)  # fmt: skip
    return v


def retire(db: Session, actor, version_id, reason, *, now: datetime | None = None) -> PriceVersion:
    """active → retired (nuk zgjidhet më për mesazhe të reja; histori mbetet). retired ⇒ no-op. Një draft s'tërhiqet."""
    actor = money_common.admin(actor)
    why = money_common.reason(reason)
    now = now or utcnow()
    lock_sequence(db)
    v = get_version(db, version_id, lock=True)
    if v.status == V_RETIRED:
        return v
    if v.status != V_ACTIVE:
        raise Conflict("only an active version can be retired")
    v.status, v.retired_at, v.retired_by_id, v.retire_reason = V_RETIRED, now, actor.id, why
    db.flush()
    rev = bump_revision(db)
    audit.record(
        db,
        actor,
        "price_version.retire",
        RESOURCE_VERSION,
        v.id,
        {"reason": why, "revision": rev},
        now=now,
    )
    return v


def assign(db: Session, actor, enterprise_id, product_id, book_id, effective_from: datetime, *,
           now: datetime | None = None, imported: bool = False) -> PriceAssignment:  # fmt: skip
    """Cakton librin e çmimeve të (enterprise, product) nga `effective_from`. Histori e pandryshueshme; i njëjti caktim = no-op."""
    actor = money_common.admin(actor)
    now = now or utcnow()
    eid, pid = _uid(enterprise_id, "enterprise id"), _uid(product_id, "product id")
    eff = effective_from if effective_from.tzinfo else effective_from.replace(tzinfo=now.tzinfo)
    lock_sequence(db)
    book = get_book(db, book_id, lock=True)
    if db.get(Enterprise, eid) is None:
        raise NotFound("enterprise not found")
    if db.get(Product, pid) is None:
        raise NotFound("product not found")
    same = db.scalar(select(PriceAssignment).where(PriceAssignment.enterprise_id == eid, PriceAssignment.product_id == pid,
                                                   PriceAssignment.effective_from == eff))  # fmt: skip
    if same is not None:
        if same.price_book_id != book.id:
            raise Conflict("a different price book is already assigned from this effective_from")
        return same
    if not imported and _utc(eff) < _utc(now):
        raise Conflict("effective_from must not be in the past")
    top = db.scalar(select(func.max(PriceAssignment.effective_from)).where(PriceAssignment.enterprise_id == eid,
                                                                           PriceAssignment.product_id == pid))  # fmt: skip
    if top is not None and _utc(eff) <= _utc(top):
        raise Conflict(
            "effective_from must be after the previous assignment of this enterprise/product"
        )
    a = PriceAssignment(enterprise_id=eid, product_id=pid, price_book_id=book.id, effective_from=eff,
                        created_by_id=actor.id, created_at=now)  # fmt: skip
    db.add(a)
    db.flush()
    rev = bump_revision(db)
    audit.record(db, actor, "price_assignment.create", RESOURCE_ASSIGNMENT, a.id,
                 {"enterprise_id": str(eid), "product_id": str(pid), "book_id": str(book.id),
                  "effective_from": pv.format_ts(eff), "revision": rev}, now=now)  # fmt: skip
    return a


def _utc(dt: datetime) -> datetime:
    from datetime import UTC

    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _same_instant(a: datetime, b: datetime) -> bool:
    return _utc(a) == _utc(b)


# --- kërkimi (referenca; e njëjta precedencë si Enterprise) ---------------------------------------------------------------


def versions_of(db: Session, book_id) -> list[dict]:
    out = []
    for v in db.scalars(select(PriceVersion).where(PriceVersion.price_book_id == _uid(book_id, "book id"),
                                                   PriceVersion.status != V_DRAFT)):  # fmt: skip
        out.append(
            {
                "version_id": str(v.id),
                "status": v.status,
                "effective_from": pv.format_ts(v.effective_from),
            }
        )
    return out


def lookup(
    db: Session, book_id, channel: str, number: str, at: datetime, operator: str = ""
) -> PriceQuote:
    book = get_book(db, book_id)
    ver, why = pv.select_version(versions_of(db, book.id), at)
    if ver is None:
        raise NoPrice(f"no effective price version ({why})")
    q = select(PriceRule).where(
        PriceRule.version_id == uuid.UUID(ver["version_id"]), PriceRule.channel == channel
    )
    if channel == "sms":
        try:
            prefixes = pv.candidate_prefixes(number)
        except pv.ContractError as e:
            raise Invalid(str(e)) from e
        q = q.where(PriceRule.prefix.in_(prefixes))
    cands = [{"prefix": r.prefix, "operator": r.operator, "rule": r} for r in db.scalars(q)]
    best = pv.pick_rule(cands, operator)
    if best is None:
        raise NoPrice("no price rule for this destination")
    r = best["rule"]
    return PriceQuote(book.id, uuid.UUID(ver["version_id"]), r.id, book.currency, r.unit_price)


def assignment_at(db: Session, enterprise_id, product_id, at: datetime) -> PriceAssignment | None:
    rows = [{"assignment_id": str(a.id), "product_id": str(a.product_id), "effective_from": pv.format_ts(a.effective_from)}
            for a in db.scalars(select(PriceAssignment).where(PriceAssignment.enterprise_id == _uid(enterprise_id, "enterprise id")))]  # fmt: skip
    best = pv.select_assignment(rows, str(_uid(product_id, "product id")), at)
    return None if best is None else db.get(PriceAssignment, uuid.UUID(best["assignment_id"]))
