"""M9-e: motori i vetëm i çmimit të klientit (SMS + email overage) për submit, vlerësimin e fushatës, kuotën e konsolës dhe faturat.

Autoriteti (`SMS_PRICING_AUTHORITY`): **local** = tarifat lokale (rate card + AccountPlan; sjellja e sotme) · **shadow** = llogarit edhe
snapshot-in Central, krahason dhe regjistron `sms_pricing_comparisons`, por CHARGE me lokalin · **central** = snapshot-i Central është
autoritar; mungesa e snapshot-it/caktimit/versionit/rregullës ose mospërputhja e monedhës ⇒ FAIL-CLOSED (`NoRate`), kurrë parazgjedhje.
Asnjë thirrje drejt Central këtu: lexohet vetëm cache-i lokal i `cp.pricing.v1` (sinkronizimi bëhet nga roli worker `pricing_control_plane`).

Precedenca (e njëjta për legacy dhe Central; `cp.pricing.v1.pick_rule`): caktimi = ai me `effective_from` më të vonë ≤ t; versioni = ai me
`effective_from` më të vonë ≤ t (`retired` ⇒ asnjë çmim, pa rënie te version më i vjetër); rregulla = prefiksi më i gjatë, brenda tij operatori
specifik mbi të përgjithshmin. Totali = `line_total(çmim_njësie, segmente)` (6 shifra, ROUND_HALF_UP në kontekst eksplicit; me çmim ≤ 6 shifra
produkti është i saktë). Rezervimi, capture dhe DLR përdorin VETËM çmimin e ngrirë në mesazh: s'ka rikërkim çmimi te DLR/UNKNOWN."""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.errors import DomainError
from app.core.scope import Owner, owned
from app.core.timeutil import as_utc, utcnow
from app.models.control_plane import ENTITLEMENT_WITHDRAWN, Entitlement
from app.models.enterprise import Enterprise
from app.models.pricing import (
    PricingAssignment,
    PricingBook,
    PricingComparison,
    PricingRule,
    PricingState,
    PricingVersion,
)
from app.models.rates import Rate
from app.models.sending import AccountPlan
from app.services import rates
from app.services.sms_text import count_segments
from app.services.wallet import InvalidAmount
from packages.contracts.control_plane.pricing import v1 as pv

LOCAL, SHADOW, CENTRAL = "local", "shadow", "central"


class PricingFrozen(DomainError):
    """Nën SMS_PRICING_AUTHORITY=central çmimi komercial nuk ndryshohet lokalisht (vetëm aplikuesi i sinkronizimit)."""

    code = "pricing_authority_frozen"


class CentralPriceError(rates.NoRate):
    """Çmimi Central i padisponueshëm për këtë vendim (fail-closed). `reason` për klasifikim/monitorim."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def authority() -> str:
    return settings.pricing_authority


def assert_local_pricing_mutable() -> None:
    if settings.pricing_authority == CENTRAL:
        raise PricingFrozen(
            "commercial pricing is owned by Central (SMS_PRICING_AUTHORITY=central): local edits are blocked"
        )


# --- vendimi ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Comparison:
    classification: str
    legacy: "Decision | None"
    central: "Decision | None"
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.classification == "match"


@dataclass(frozen=True)
class Decision:
    source: str  # legacy | central
    currency: str
    encoding: str
    segments: int
    unit_price: Decimal
    total: Decimal
    rule_prefix: str = ""
    rule_operator: str = ""
    # legacy
    card_id: int | None = None
    version_id: int | None = None
    rate_id: int | None = None
    # central
    book_ref: uuid.UUID | None = None
    version_ref: uuid.UUID | None = None
    rule_ref: uuid.UUID | None = None
    shadow: Comparison | None = None


def message_fields(d: Decision) -> dict:
    """Fushat e snapshot-it të çmimit që ngrihen në `Message` (një vend: submit)."""
    return {
        "encoding": d.encoding, "segments": d.segments, "currency": d.currency, "unit_price": d.unit_price,
        "total_price": d.total, "rate_version_id": d.version_id, "rate_id": d.rate_id, "price_source": d.source,
        "pricing_book_ref": d.book_ref, "pricing_version_ref": d.version_ref, "pricing_rule_ref": d.rule_ref,
    }  # fmt: skip


def _legacy(
    db: Session, plan: AccountPlan | None, number: str, text: str, at: datetime, operator: str
) -> Decision:
    if plan is None:
        raise rates.NoRate("account has no rate card")
    q = rates.quote(db, plan.rate_card_id, number, text, at, operator)
    r = db.get(Rate, q.rate_id)
    return Decision(
        "legacy", q.currency, q.encoding, q.segments, q.unit_price, q.total, r.prefix if r else "",
        r.operator if r else "", card_id=q.card_id, version_id=q.version_id, rate_id=q.rate_id,
    )  # fmt: skip


def _enterprise_id(db: Session, owner: Owner | uuid.UUID) -> uuid.UUID:
    if isinstance(owner, uuid.UUID):
        return owner
    eid = getattr(owner, "enterprise_id", None)
    if eid is not None:
        return eid
    owner_ref = getattr(owner, "owner_ref", owner)
    ent = db.scalar(select(Enterprise).where(Enterprise.owner_ref == owner_ref))
    if ent is None:
        raise CentralPriceError(
            "no_enterprise", "owner has no Enterprise identity for Central pricing"
        )
    return ent.id


def _product_for(db: Session, enterprise_id: uuid.UUID, channel: str) -> uuid.UUID:
    ids = set(db.scalars(select(Entitlement.product_id).where(
        Entitlement.enterprise_id == enterprise_id, Entitlement.channel == channel,
        Entitlement.status != ENTITLEMENT_WITHDRAWN)))  # fmt: skip
    if len(ids) != 1:
        raise CentralPriceError(
            "no_product",
            f"enterprise has {len(ids)} {channel} products: pricing mapping is not unambiguous",
        )
    return next(iter(ids))


def _utc_iso(dt: datetime) -> str:
    return pv.format_ts(as_utc(dt))


def _resolve_version(
    db: Session, owner: Owner, channel: str, at: datetime
) -> tuple[PricingBook, PricingVersion, uuid.UUID]:
    """NJË version koherent: gjendja → caktimi → libri → versioni (të gjitha nga i njëjti snapshot aktiv)."""
    state = db.get(PricingState, 1)
    if state is None or state.active_snapshot_id is None:
        raise CentralPriceError(
            "no_snapshot", "no complete Central pricing snapshot has been applied yet"
        )
    eid = _enterprise_id(db, owner)
    pid = _product_for(db, eid, channel)
    rows = [{"assignment_id": str(a.assignment_id), "product_id": str(a.product_id), "book_id": a.book_id,
             "effective_from": _utc_iso(a.effective_from)}
            for a in db.scalars(select(PricingAssignment).where(
                PricingAssignment.snapshot_id == state.active_snapshot_id, PricingAssignment.enterprise_id == eid,
                PricingAssignment.product_id == pid))]  # fmt: skip
    asg = pv.select_assignment(rows, str(pid), at)
    if asg is None:
        raise CentralPriceError(
            "no_assignment", "no price book is assigned to this enterprise/product at this time"
        )
    book = db.get(PricingBook, asg["book_id"])
    versions = list(db.scalars(select(PricingVersion).where(PricingVersion.book_id == book.id)))
    chosen, why = pv.select_version(
        [
            {"id": v.id, "status": v.status, "effective_from": _utc_iso(v.effective_from)}
            for v in versions
        ],
        at,
    )
    if chosen is None:
        raise CentralPriceError("version_missing", f"no effective price version ({why})")
    return book, next(v for v in versions if v.id == chosen["id"]), pid


def _central(
    db: Session, owner: Owner, number: str, text: str, at: datetime, operator: str
) -> Decision:
    at = as_utc(at)
    try:
        prefixes = pv.candidate_prefixes(number)
    except pv.ContractError as e:
        raise rates.InvalidNumber(str(e)) from e
    book, ver, _pid = _resolve_version(db, owner, "sms", at)
    cands = [{"prefix": r.prefix, "operator": r.operator, "rule": r} for r in db.scalars(select(PricingRule).where(
        PricingRule.version_id == ver.id, PricingRule.channel == "sms", PricingRule.prefix.in_(prefixes)))]  # fmt: skip
    best = pv.pick_rule(cands, operator)
    if best is None:
        raise CentralPriceError("missing_rule", "no Central price rule for this destination")
    try:
        enc, segments = count_segments(text)
    except ValueError as e:
        raise InvalidAmount(str(e)) from e
    rule: PricingRule = best["rule"]
    return Decision("central", book.currency, enc, segments, rule.unit_price, pv.line_total(rule.unit_price, segments),
                    rule.prefix, rule.operator, book_ref=book.id, version_ref=ver.id, rule_ref=rule.id)  # fmt: skip


def compare(
    legacy: Decision, central: Decision | None, error: CentralPriceError | None = None
) -> Comparison:
    if central is None:
        reason = getattr(error, "reason", "version_missing")
        cls = "missing_rule" if reason == "missing_rule" else "version_missing"
        return Comparison(cls, legacy, None, str(error)[:200] if error else "")
    if central.currency != legacy.currency:
        return Comparison(
            "currency_mismatch", legacy, central, f"{legacy.currency} vs {central.currency}"
        )
    if central.segments != legacy.segments:
        return Comparison("segments_mismatch", legacy, central)
    if (len(central.rule_prefix), central.rule_operator != "") != (
        len(legacy.rule_prefix),
        legacy.rule_operator != "",
    ):
        return Comparison("precedence_mismatch", legacy, central, f"legacy rule {legacy.rule_prefix}/{legacy.rule_operator} vs {central.rule_prefix}/{central.rule_operator}")  # fmt: skip
    if central.unit_price != legacy.unit_price:
        return Comparison(
            "unit_price_mismatch", legacy, central, f"{legacy.unit_price} vs {central.unit_price}"
        )
    if central.total != legacy.total:
        return Comparison("total_mismatch", legacy, central, f"{legacy.total} vs {central.total}")
    return Comparison("match", legacy, central)


def quote(db: Session, owner: Owner, number: str, text: str, at: datetime | None = None, operator: str = "",
          plan: AccountPlan | None = None) -> Decision:  # fmt: skip
    """Vendimi i çmimit SMS sipas autoritetit. Fushat e ngrira të mesazhit: `message_fields`. Pa efekt shkrimi (as shadow-i)."""
    at = as_utc(at or utcnow())
    mode = authority()
    if mode == CENTRAL:
        return _central(db, owner, number, text, at, operator)
    if plan is None:
        plan = db.scalar(select(AccountPlan).where(owned(AccountPlan, owner)))
    legacy = _legacy(db, plan, number, text, at, operator)
    if mode == LOCAL:
        return legacy
    try:  # shadow: dështimet e Central NUK bllokojnë; klasifikohen
        central = _central(db, owner, number, text, at, operator)
        cmp = compare(legacy, central)
    except CentralPriceError as e:
        cmp = compare(legacy, None, e)
    except (
        rates.NoRate
    ) as e:  # InvalidNumber etj. nga legacy janë ngritur më lart; këtu vetëm rrugë Central
        cmp = Comparison("missing_rule", legacy, None, str(e)[:200])
    return Decision(**{**legacy.__dict__, "shadow": cmp})


def _sampled(ref: str) -> bool:
    s = settings.pricing_shadow_sample
    if s >= 1.0:
        return True
    return int(hashlib.sha256(ref.encode()).hexdigest()[:8], 16) / 2**32 < s


def record_comparison(db: Session, cmp: Comparison | None, ref: str, kind: str = "sms") -> None:
    """Regjistron krahasimin shadow (brenda tx të thirrësit; pa efekt parash). Mostrim deterministik sipas `ref`."""
    if cmp is None or not _sampled(ref):
        return
    lg, ce = cmp.legacy, cmp.central
    db.add(PricingComparison(
        kind=kind, ref=ref[:64], classification=cmp.classification, ok=cmp.ok,
        legacy_currency=lg.currency if lg else None, legacy_unit_price=lg.unit_price if lg else None,
        legacy_segments=lg.segments if lg else None, legacy_total=lg.total if lg else None,
        central_currency=ce.currency if ce else None, central_unit_price=ce.unit_price if ce else None,
        central_segments=ce.segments if ce else None, central_total=ce.total if ce else None,
        central_version_id=ce.version_ref if ce else None, detail=cmp.detail[:200] or None,
    ))  # fmt: skip


# --- email overage -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EmailPrice:
    unit_price: Decimal
    source: str  # legacy_plan | central
    currency: str
    version_ref: uuid.UUID | None = None
    shadow_ok: bool | None = None
    shadow_detail: str = ""


def email_overage(db: Session, owner: Owner, plan, at: datetime | None = None) -> EmailPrice:
    """Çmimi i overage për një email mbi kuotën e përfshirë (postpaid; jashtë wallet-it SMS). `plan` = `Plan` i faturimit."""
    at = as_utc(at or utcnow())
    mode = authority()
    legacy = EmailPrice(plan.email_overage_price, "legacy_plan", plan.currency)
    if mode == LOCAL:
        return legacy

    def central() -> EmailPrice:
        book, ver, _ = _resolve_version(db, owner, "email", at)
        if book.currency != plan.currency:
            raise CentralPriceError(
                "currency",
                f"price currency {book.currency} != invoice currency {plan.currency} (no FX)",
            )
        rule = db.scalar(
            select(PricingRule).where(
                PricingRule.version_id == ver.id, PricingRule.channel == "email"
            )
        )
        if rule is None:
            raise CentralPriceError("missing_rule", "no Central email price rule")
        return EmailPrice(rule.unit_price, "central", book.currency, ver.id)

    if mode == CENTRAL:
        return central()
    try:
        c = central()
        ok = c.unit_price == legacy.unit_price
        return EmailPrice(legacy.unit_price, "legacy_plan", legacy.currency, None, ok,
                          "" if ok else f"{legacy.unit_price} vs {c.unit_price}")  # fmt: skip
    except CentralPriceError as e:
        return EmailPrice(
            legacy.unit_price, "legacy_plan", legacy.currency, None, False, f"{e.reason}: {e}"[:200]
        )


def record_email_comparison(db: Session, price: EmailPrice, ref: str) -> None:
    if price.shadow_ok is None or not _sampled(ref):
        return
    cls = (
        "match"
        if price.shadow_ok
        else (
            "version_missing"
            if "unit_price" not in price.shadow_detail and " vs " not in price.shadow_detail
            else "unit_price_mismatch"
        )
    )
    db.add(PricingComparison(kind="email", ref=ref[:64], classification=cls, ok=price.shadow_ok,
                             legacy_currency=price.currency, legacy_unit_price=price.unit_price, detail=price.shadow_detail[:200] or None))  # fmt: skip
