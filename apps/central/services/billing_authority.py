"""M9-g4: autoriteti i faturimit periodik në Central (`local | shadow | central`) + gatishmëria e cutover-it. Pa rrjet; asnjë lexim i DB-së së Enterprise.

- `local` (parazgjedhja, edhe pa rresht): Enterprise lëshon; `billing_run` autoritar refuzon. `shadow`: Enterprise lëshon; Central vetëm krahason (shih `billing_shadow`).
  `central`: Central lëshon. Kalimi `central` kërkon ACK eksplicit DHE readiness pa FAIL; vetëm nga `shadow`.
- Rollback `central → shadow|local`: lejohet VETËM nëse Central s'ka lëshuar asnjë faturë autoritare (`provenance=central`) dhe me ACK + arsye. Pas faturës së parë Central:
  Conflict (forward-fix/rakordim manual — kurrë rikthim i verbër te Enterprise).
- `require_central` ruan çdo lëshim autoritar (CLI `billing_run`); `process_period` mbetet shërbim i brendshëm pa varësi nga ky gate."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.errors import Conflict, Invalid
from apps.central.core.timeutil import utcnow
from apps.central.models.billing import (
    PROV_CENTRAL,
    SUB_ACTIVE,
    BillingSubscription,
    Invoice,
    InvoiceNumberSequence,
)
from apps.central.models.billing_import import (
    BillingAuthorityState,
    BillingImportBatch,
    BillingImportItem,
    BillingShadowComparison,
)
from apps.central.services import (
    audit,
    billing,
    billing_import,
    billing_overage,
    billing_readiness,
    money_common,
)

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
# M9-g5 (politika e pranimit të shadow): FAIL = çdo mospërputhje e pashpjeguar që ndryshon fakturën; WARN = e shpjegueshme/e dokumentuar.
# `amount_mismatch` është FAIL mbi tolerancën `CENTRAL_BILLING_SHADOW_AMOUNT_TOLERANCE` (parazgjedhje 0), WARN brenda saj; asgjë nuk normalizohet.
HARD_CATEGORIES = (
    "period_mismatch",
    "currency_mismatch",
    "plan_mismatch",
    "tax_mismatch",
    "amount_mismatch",
    "central_only",
    "legacy_only",
)
SOFT_CATEGORIES = ("usage_mismatch", "pricing_mismatch", "insufficient_usage")


def _within_tolerance(r) -> bool:
    tol = Decimal(str(settings.billing_shadow_amount_tolerance))
    try:
        return tol > 0 and abs(Decimal(r.legacy["total"]) - Decimal(r.central["total"])) <= tol
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: str
    reason: str


def get_state(db: Session) -> BillingAuthorityState | None:
    return db.get(BillingAuthorityState, 1)


def mode(db: Session) -> str:
    st = get_state(db)
    return "local" if st is None else st.mode


def require_central(db: Session) -> None:
    if mode(db) != "central":
        raise Conflict(
            f"billing authority is '{mode(db)}': Central must not issue invoices (set it to central after the cutover readiness passes)"
        )


def central_invoice_count(db: Session) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(Invoice).where(Invoice.provenance == PROV_CENTRAL)
        )
        or 0
    )


def set_mode(
    db: Session, actor, target: str, *, ack: bool, reason, now: datetime | None = None
) -> BillingAuthorityState:
    actor = money_common.admin(actor)
    why = money_common.reason(reason)
    if target not in ("local", "shadow", "central"):
        raise Invalid("mode must be local, shadow or central")
    now = billing.utc(now or utcnow())
    st = db.get(BillingAuthorityState, 1)
    if st is None:
        st = BillingAuthorityState(id=1, mode="local", ack=False)
        db.add(st)
        db.flush()
    cur = st.mode
    if target == cur:
        return st
    if target == "central":
        if cur != "shadow":
            raise Conflict("central billing authority can only be enabled from shadow mode")
        if not ack:
            raise Conflict("enabling central billing authority requires an explicit ACK")
        bad = [c for c in readiness(db, now, prod_ack=ack) if c.level == FAIL]
        if bad:
            raise Conflict(
                "billing authority readiness failed: "
                + "; ".join(f"{c.name}: {c.reason}" for c in bad[:6])
            )
    elif cur == "central":
        if not ack:
            raise Conflict("rolling back from central requires an explicit ACK")
        n = central_invoice_count(db)
        if n:
            raise Conflict(
                f"Central has already issued {n} authoritative invoice(s): rollback to Enterprise is blocked; use the forward-fix / manual reconciliation procedure"
            )
    st.mode, st.ack, st.changed_by_id, st.changed_at, st.reason = (
        target,
        bool(ack) if target == "central" else False,
        actor.id,
        now,
        why,
    )
    db.flush()
    audit.record(
        db,
        actor,
        "billing.authority_change",
        "billing_authority",
        "1",
        {"from": cur, "to": target, "ack": bool(ack), "reason": why},
        now=now,
    )
    return st


# --- gatishmëria (vetëm lexim) ------------------------------------------------------------------------------------------------------------------


def _latest_batch(db):
    return db.scalar(
        select(BillingImportBatch)
        .order_by(BillingImportBatch.applied_at.desc(), BillingImportBatch.id)
        .limit(1)
    )


def latest_shadow(db: Session) -> dict:
    """Krahasimi më i fundit për çdo (abonim, periudhë)."""
    latest: dict = {}
    for r in db.scalars(
        select(BillingShadowComparison).order_by(
            BillingShadowComparison.computed_at, BillingShadowComparison.id
        )
    ):
        latest[(r.subscription_id, r.period_index)] = r
    return latest


def readiness(
    db: Session, now: datetime | None = None, *, prod_ack: bool | None = None
) -> list[Check]:
    now = billing.utc(now or utcnow())
    out: list[Check] = []
    batch = _latest_batch(db)
    out.append(
        Check(
            "legacy_import_complete",
            PASS if batch else FAIL,
            "no import batch applied" if not batch else f"latest batch {batch.export_id}",
        )
    )
    unresolved = billing_import.unresolved_issues(db)
    kinds = sorted({i.classification for i in unresolved})
    out.append(
        Check(
            "import_conflicts_resolved",
            FAIL if unresolved else PASS,
            f"{len(unresolved)} unresolved import issue(s): {', '.join(kinds)}"
            if unresolved
            else "0 unresolved",
        )
    )
    partial = [i for i in unresolved if i.classification == "unsupported"]
    out.append(
        Check(
            "no_unresolved_partial_or_overpayment",
            FAIL if partial else PASS,
            f"{len(partial)} legacy partial/over-settlement case(s) unresolved"
            if partial
            else "none",
        )
    )
    imported_subs = {
        i.target_id
        for i in db.scalars(
            select(BillingImportItem).where(BillingImportItem.source_table == "subscriptions")
        )
    }
    active = list(
        db.scalars(select(BillingSubscription).where(BillingSubscription.status == SUB_ACTIVE))
    )
    unmapped = [s for s in active if s.id not in imported_subs]
    out.append(
        Check(
            "active_subscriptions_mapped",
            FAIL if (not active or unmapped) else PASS,
            f"{len(unmapped)} active Central subscription(s) not backed by the legacy import"
            if unmapped
            else (f"{len(active)} mapped" if active else "no active subscription"),
        )
    )
    bad_seq = []
    for year, last in db.execute(
        select(InvoiceNumberSequence.year, InvoiceNumberSequence.last_number)
    ):
        mx = max(
            (
                int(n.rsplit("-", 1)[1])
                for (n,) in db.execute(
                    select(Invoice.number).where(Invoice.number.like(f"INV-{year}-%"))
                )
            ),
            default=0,
        )
        if last < mx:
            bad_seq.append(year)
    legacy_years = {int(k) for k in (batch.summary.get("sequence_seeds", {}) if batch else {})}
    missing_seed = [
        y
        for y in legacy_years
        if (
            db.scalar(
                select(InvoiceNumberSequence.last_number).where(InvoiceNumberSequence.year == y)
            )
            or 0
        )
        < int(batch.summary["sequence_seeds"][str(y)])
    ]
    out.append(
        Check(
            "sequence_seeds_safe",
            FAIL if (bad_seq or missing_seed) else PASS,
            f"sequence behind existing numbers for years {sorted(bad_seq + missing_seed)}"
            if (bad_seq or missing_seed)
            else "seeded above every legacy/Central number",
        )
    )
    metered = billing_readiness._metered_subscriptions(db, now)
    no_base, post, pricing_gap = [], [], []
    for sub, pid in metered:
        if sub.id not in imported_subs or sub.status != SUB_ACTIVE:
            continue
        start, end = billing.period_bounds(sub, sub.next_period_index)
        pv = db.get(billing.PlanVersion, sub.plan_version_id)
        rep, opening = billing_overage._baseline(db, sub, pid, start, sub.next_period_index)
        if (
            rep is None and opening is None
        ):  # baseline-i kontrollohet pavarësisht nga raporti i prerjes
            no_base.append(str(sub.enterprise_id))
        ev = billing_overage.evaluate(db, sub, pv, start, end)
        if ev.postpone and ev.postpone != billing_overage.BASELINE_MISSING:
            post.append(f"{ev.postpone}:{sub.enterprise_id}")
    out.append(
        Check(
            "usage_opening_baseline",
            FAIL if no_base else PASS,
            f"no opening baseline or earlier report for {len(no_base)} metered subscription(s)"
            if no_base
            else "present for every metered subscription",
        )
    )
    out.append(
        Check(
            "currency_pricing_mapping_valid",
            FAIL if post else PASS,
            "; ".join(post[:5]) if post else "valid",
        )
    )
    metered_ids = {s.id for s, _ in metered}
    for it in db.scalars(
        select(BillingImportItem).where(BillingImportItem.source_table == "plans")
    ):
        price = it.detail.get("legacy_email_overage_price")
        if price and float(price) > 0:
            for sub in active:
                pvv = db.get(billing.PlanVersion, sub.plan_version_id)
                if pvv is not None and pvv.id == it.target_id and sub.id not in metered_ids:
                    pricing_gap.append(str(sub.enterprise_id))
    out.append(
        Check(
            "legacy_overage_has_central_price",
            FAIL if pricing_gap else PASS,
            f"legacy plan charged email overage but Central has no email price assignment for {len(set(pricing_gap))} enterprise(s)"
            if pricing_gap
            else "mapped",
        )
    )
    rows = billing_readiness.summary(db, now)["reports"]
    stale = [
        r
        for r in rows
        if r["age_seconds"] is None or r["age_seconds"] > settings.billing_usage_stale_seconds
    ]
    out.append(
        Check(
            "latest_usage_report_healthy",
            FAIL if stale else PASS,
            f"{len(stale)} metered enterprise(s) without a fresh usage report"
            if stale
            else "fresh",
        )
    )
    latest = latest_shadow(db)
    legacy_periods = {(s.id) for s in active if s.id in imported_subs and s.next_period_index > 0}
    compared = {k[0] for k in latest}
    cats = [r.category for r in latest.values()]
    hard = [
        r.category
        for r in latest.values()
        if r.category in HARD_CATEGORIES
        and not (r.category == "amount_mismatch" and _within_tolerance(r))
    ]
    soft = [
        r.category
        for r in latest.values()
        if r.category in SOFT_CATEGORIES
        or (r.category == "amount_mismatch" and _within_tolerance(r))
    ]
    if legacy_periods - compared:
        out.append(
            Check(
                "shadow_comparison_acceptable",
                FAIL,
                f"shadow comparison has not been run for {len(legacy_periods - compared)} subscription(s)",
            )
        )
    elif hard:
        out.append(
            Check(
                "shadow_comparison_acceptable",
                FAIL,
                "unexplained mismatches: " + ", ".join(sorted(set(hard))),
            )
        )
    elif soft:
        out.append(
            Check(
                "shadow_comparison_acceptable",
                WARN,
                "explained differences need review: " + ", ".join(sorted(set(soft))),
            )
        )
    else:
        out.append(Check("shadow_comparison_acceptable", PASS, f"{len(cats)} comparison(s) exact"))
    attested = batch is not None and batch.attestation.get("mode") == "central"
    out.append(
        Check(
            "enterprise_billing_frozen",
            PASS if attested else FAIL,
            "latest export attests the Enterprise billing authority = central"
            if attested
            else "the latest export was not taken with Enterprise frozen (billing authority = central)",
        )
    )
    out.append(
        Check(
            "no_dual_issuer",
            PASS if mode(db) in ("shadow", "central") and attested else FAIL,
            "Enterprise frozen and Central in shadow/central"
            if attested
            else "Enterprise could still issue",
        )
    )
    out.append(
        Check(
            "central_billing_worker_configured",
            PASS if settings.billing_worker_configured else FAIL,
            "CENTRAL_BILLING_WORKER_CONFIGURED "
            + ("set" if settings.billing_worker_configured else "missing"),
        )
    )
    st = get_state(db)
    ack = prod_ack if prod_ack is not None else bool(st and st.ack)
    out.append(
        Check(
            "cutover_ack_present",
            PASS if ack else (FAIL if settings.env == "production" else WARN),
            "ACK recorded" if ack else "no ACK recorded (required to enable central)",
        )
    )
    return out


def overall(items: list[Check]) -> str:
    return (
        FAIL
        if any(c.level == FAIL for c in items)
        else (WARN if any(c.level == WARN for c in items) else PASS)
    )
