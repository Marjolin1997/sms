"""M9-e: gatishmëria për SMS_PRICING_AUTHORITY=central (vetëm lexim; NUK ndryshon konfigurimin as DB-në).

Provë nga cache-i, krahasimet shadow dhe kodi — jo deklaratë operatori. Central thirret VETËM nga sinkronizimi (jo këtu)."""

import inspect
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import as_utc, utcnow
from app.models.enterprise import Enterprise
from app.models.pricing import (
    PricingAssignment,
    PricingBook,
    PricingComparison,
    PricingRule,
    PricingVersion,
)
from app.models.rates import RateCardVersion
from app.models.sending import AccountPlan
from app.models.wallet import Wallet
from app.services import pricing, pricing_sync
from packages.contracts.control_plane.pricing import v1 as pv

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: str
    reason: str


def _c(name: str, bad, ok: str = "ok") -> Check:
    if bad:
        return Check(name, FAIL, bad if isinstance(bad, str) else "; ".join(bad[:10]))
    return Check(name, PASS, ok)


def evaluate(
    db: Session,
    *,
    now: datetime | None = None,
    min_samples: int = 20,
    max_mismatch_pct: float = 0.0,
) -> list[Check]:
    now = as_utc(now or utcnow())
    out: list[Check] = []
    mode = settings.pricing_authority
    out.append(
        _c(
            "authority_mode",
            mode == "local" and "SMS_PRICING_AUTHORITY=local: set shadow first",
            f"mode={mode}",
        )
    )

    from app.services.control_plane_client import ConfigError, config_from_settings

    try:
        config_from_settings(settings)
        out.append(Check("sync_configured", PASS, "base url, client id, key id and key file ok"))
    except ConfigError as e:
        out.append(Check("sync_configured", FAIL, str(e)))

    st = pricing_sync.get_state(db)
    out.append(_c("snapshot_present", None if st.active_snapshot_id else "no complete Central pricing snapshot has been applied",
                  f"revision={st.revision} epoch={st.epoch}"))  # fmt: skip
    age = pricing_sync.sync_age_seconds(st, now)
    if age is None:
        out.append(Check("sync_fresh", FAIL, "pricing sync has never succeeded"))
    elif age > settings.pricing_stale_fail_seconds:
        out.append(
            Check(
                "sync_fresh",
                FAIL,
                f"last success {int(age)} s ago (> {settings.pricing_stale_fail_seconds} s)",
            )
        )
    elif age > settings.pricing_stale_warn_seconds:
        out.append(
            Check(
                "sync_fresh",
                WARN,
                f"last success {int(age)} s ago (> {settings.pricing_stale_warn_seconds} s)",
            )
        )
    else:
        out.append(Check("sync_fresh", PASS, f"last success {int(age)} s ago"))
    out.append(
        _c(
            "sync_no_error",
            st.last_error and f"last sync error: {st.last_error[:200]}",
            "no blocked snapshot",
        )
    )

    mapping, current, currency = [], [], []
    plans = list(db.scalars(select(AccountPlan)))
    for plan in plans:
        ent = db.scalar(select(Enterprise).where(Enterprise.owner_ref == plan.owner_ref))
        if ent is None:
            mapping.append(f"{plan.owner_ref}: no Enterprise identity")
            continue
        try:
            book, ver, _pid = pricing._resolve_version(db, ent.id, "sms", now)
        except pricing.CentralPriceError as e:
            (
                mapping if e.reason in ("no_product", "no_enterprise", "no_snapshot") else current
            ).append(f"{plan.owner_ref}: {e.reason}")
            continue
        if not db.scalar(
            select(Wallet.id).where(
                Wallet.owner_ref == plan.owner_ref, Wallet.currency == book.currency
            )
        ):
            currency.append(f"{plan.owner_ref}: no {book.currency} wallet (book currency)")
        rules = db.scalar(
            select(func.count())
            .select_from(PricingRule)
            .where(PricingRule.version_id == ver.id, PricingRule.channel == "sms")
        )
        if not rules:
            current.append(f"{plan.owner_ref}: effective version has no sms rule")
    out.append(
        _c("product_mapping_valid", mapping, f"{len(plans)} account plan(s) map to one sms product")
    )
    out.append(
        _c(
            "assignment_and_effective_version",
            current,
            "every account has an effective, non-retired Central price version",
        )
    )
    out.append(
        _c(
            "wallet_currency_matches",
            currency,
            "price currency equals the SMS wallet currency (no FX)",
        )
    )

    bad = []
    for v in db.scalars(select(PricingVersion)):
        rules = [{"rule_id": str(r.id), "channel": r.channel, "prefix": r.prefix, "operator": r.operator,
                  "unit_price": pv.format_price(r.unit_price)} for r in db.scalars(select(PricingRule).where(PricingRule.version_id == v.id))]  # fmt: skip
        if len(rules) != v.rule_count or pv.rules_hash(rules) != v.content_hash:
            bad.append(f"version {v.id}: cached rules do not match the content hash")
    out.append(
        _c(
            "cache_integrity_no_ambiguous_rules",
            bad,
            "every cached version matches its content hash; scopes are UNIQUE",
        )
    )

    rows = list(db.scalars(select(PricingComparison).where(PricingComparison.kind == "sms")))
    n, mism = len(rows), [r for r in rows if not r.ok]
    pct = (100.0 * len(mism) / n) if n else 0.0
    breakdown = dict(Counter(r.classification for r in mism))
    if n < min_samples:
        out.append(
            Check(
                "shadow_comparison",
                FAIL,
                f"only {n} shadow comparisons (< {min_samples}); run shadow longer",
            )
        )
    elif pct > max_mismatch_pct:
        out.append(
            Check(
                "shadow_comparison",
                FAIL,
                f"{len(mism)}/{n} mismatches ({pct:.2f}% > {max_mismatch_pct}%): {breakdown}",
            )
        )
    else:
        out.append(
            Check(
                "shadow_comparison", PASS, f"{n} comparisons, {len(mism)} mismatches ({pct:.2f}%)"
            )
        )
    email = list(db.scalars(select(PricingComparison).where(PricingComparison.kind == "email")))
    em_bad = [r for r in email if not r.ok]
    out.append(Check("shadow_email_comparison", WARN if em_bad else PASS,
                     f"{len(em_bad)}/{len(email)} email overage mismatches" if em_bad else f"{len(email)} email comparisons, no mismatch"))  # fmt: skip

    first = db.scalar(select(func.min(PricingComparison.created_at)))
    late = []
    if first is not None:
        for v in db.scalars(select(RateCardVersion).where(RateCardVersion.created_at > first)):
            late.append(f"legacy rate card version {v.id} created after the shadow window began")
    out.append(
        _c(
            "no_local_pricing_mutation_since_shadow",
            late,
            "no legacy rate card version created since the shadow window began",
        )
    )

    src_bad = []
    from app.api import console as console_api
    from app.services import campaigns, messages

    for mod, needle in (
        (messages, "pricing.quote("),
        (campaigns, "pricing.quote("),
        (console_api, "pricing_svc.quote("),
    ):
        src = inspect.getsource(mod)
        if (
            needle not in src
            or "rates.quote(" in src.replace("pricing.quote(", "")
            or "rates_svc.quote(" in src
        ):
            src_bad.append(f"{mod.__name__} does not use the authority-aware pricing engine")
    out.append(
        _c(
            "estimator_and_submit_use_pricing_engine",
            src_bad,
            "submit, campaign estimate and console quote share one engine",
        )
    )

    out.append(_c("central_version_available", None if db.scalar(select(func.count()).select_from(PricingVersion)) else
                  "no Central price version in the cache", "Central price versions present"))  # fmt: skip
    if settings.env == "production":
        out.append(
            _c(
                "production_ack",
                None
                if settings.pricing_authority_ack
                else "SMS_PRICING_AUTHORITY_ACK=true is required in production",
                "ack present",
            )
        )
    else:
        out.append(Check("production_ack", PASS, "not production"))
    _ = (PricingAssignment, PricingBook)
    return out


def ok(checks: list[Check]) -> bool:
    return not any(c.level == FAIL for c in checks)
