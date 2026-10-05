"""M9-c: gatishmëria për `SMS_MONEY_AUTHORITY=central` (vetëm lexim; NUK ndryshon konfigurimin).

Çdo kontroll është provë nga DB-ja (ledger, baseline, grant-et, kursori), jo deklaratë operatori. PASS
vetëm kur s'ka asnjë FAIL. Shih `scripts/money_authority_readiness.py` (CLI) dhe `docs/M9_MONEY_AUDIT.md`.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import as_utc, utcnow
from app.models.enterprise import Enterprise
from app.models.money_authority import (
    BASELINE_ACTIVE,
    G_MATCHED,
    G_MISMATCH,
    G_RECON,
    G_REVERSED,
    G_UNMAPPED,
    MoneyBaseline,
    MoneyGrant,
)
from app.models.wallet import EntryType, Hold, HoldStatus, LedgerEntry, Wallet
from app.services import money_authority as ma
from app.services import money_sync as ms
from app.services import wallet as wallets

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: str
    reason: str


def _c(name: str, bad: list[str] | str | None, ok: str = "ok") -> Check:
    if bad:
        return Check(name, FAIL, bad if isinstance(bad, str) else "; ".join(bad[:10]))
    return Check(name, PASS, ok)


def evaluate(
    db: Session,
    *,
    now: datetime | None = None,
    include_queue: bool = True,
    include_usage: bool = True,
    reconciliation_fetch: Callable[[uuid.UUID], dict] | None = None,
) -> list[Check]:
    now = now or utcnow()
    out: list[Check] = []
    mode = settings.money_authority
    out.append(_c("authority_mode", mode == "local" and "SMS_MONEY_AUTHORITY=local: set shadow first",
                  f"mode={mode}"))  # fmt: skip

    from app.services.control_plane_client import ConfigError, config_from_settings

    try:
        config_from_settings(settings)
        out.append(
            Check("consumer_configured", PASS, "base url, client id, key id and key file ok")
        )
    except ConfigError as e:
        out.append(Check("consumer_configured", FAIL, str(e)))

    cur = ms.get_cursor(db)
    age = ms.sync_age_seconds(cur, now)
    problems = []
    if cur.epoch is None:
        problems.append("cursor not initialised (the money consumer never ran)")
    if age is None:
        problems.append("no successful money sync yet")
    elif age > ms.SLO_AGE_S:
        problems.append(f"last success {int(age)} s ago (> {ms.SLO_AGE_S} s)")
    if cur.last_error:
        problems.append(f"cursor blocked: {cur.last_error[:200]}")
    out.append(_c("money_cursor_healthy", problems, f"epoch set, last_seq={cur.last_seq}"))

    by_status: dict[str, list[str]] = {}
    for g in db.scalars(select(MoneyGrant)):
        by_status.setdefault(g.status, []).append(f"{g.grant_id}({g.detail or ''})"[:120])
    out.append(_c("no_baseline_mismatch", by_status.get(G_MISMATCH),
                  "no baseline_mismatch grants"))  # fmt: skip
    out.append(_c("no_unmapped_grants", by_status.get(G_UNMAPPED), "all grants map to a wallet"))
    out.append(_c("no_unresolved_reversal", by_status.get(G_RECON),
                  "no reconciliation_required reversals"))  # fmt: skip

    wallet_rows = list(db.scalars(select(Wallet)))
    inv, neg, held_bad, need_base, mapping = [], [], [], [], []
    for w in wallet_rows:
        if not wallets.verify_wallet(db, w.id):
            inv.append(f"wallet {w.id}: stored balance != SUM(delta)")
        avail, held = wallets.balances(db, w.id)
        if avail < 0 or held < 0:
            neg.append(f"wallet {w.id}")
        active = ma.active_holds_sum(db, w.id)
        if held != active:
            held_bad.append(f"wallet {w.id}: held={held} active_holds={active}")
        had_local_credit = any(
            e.available_delta + e.held_delta > 0 and e.entry_type != EntryType.GRANT
            for e in db.scalars(select(LedgerEntry).where(LedgerEntry.wallet_id == w.id))
        )
        if had_local_credit and ma.active_baseline(db, w.id) is None:
            need_base.append(f"wallet {w.id} has local credit history but no active baseline")
        ent = db.scalar(select(Enterprise).where(Enterprise.owner_ref == w.owner_ref))
        if ent is not None:
            try:
                ma.sms_product_for(db, ent.id)
            except ma.Unmapped as e:
                mapping.append(f"wallet {w.id}: {e}")
        elif had_local_credit:
            mapping.append(f"wallet {w.id}: owner has no enterprise identity")
    out.append(_c("wallet_ledger_consistent", inv, f"{len(wallet_rows)} wallet(s)"))
    out.append(_c("no_negative_balance", neg))
    out.append(_c("held_equals_active_holds", held_bad))
    out.append(_c("baseline_exists_for_existing_wallets", need_base))
    out.append(_c("product_mapping_unambiguous", mapping, "one sms product per enterprise"))

    base_bad, boot_bad, mint_bad = [], [], []
    for b in db.scalars(select(MoneyBaseline).where(MoneyBaseline.status == BASELINE_ACTIVE)):
        if not ma.baseline_valid(b):
            base_bad.append(f"baseline {b.baseline_ref[:12]}: hash/identity invalid")
        g = db.scalar(select(MoneyGrant).where(MoneyGrant.baseline_ref == b.baseline_ref,
                                               MoneyGrant.status.in_((G_MATCHED, G_REVERSED))))  # fmt: skip
        if g is None:
            boot_bad.append(f"baseline {b.baseline_ref[:12]}: no matched bootstrap grant")
        elif (g.amount, g.currency, g.product_id, g.enterprise_id) != (
            b.gross_at_cutover, b.currency, b.product_id, b.enterprise_id
        ):  # fmt: skip
            boot_bad.append(
                f"baseline {b.baseline_ref[:12]}: bootstrap does not match gross/currency/product"
            )
        mints = ma.positive_mints_after(db, b.wallet_id, b.ledger_max_id)
        if mints:
            mint_bad.append(f"wallet {b.wallet_id}: {len(mints)} positive local entries after baseline "
                            f"(ids {[e.id for e in mints][:5]})")  # fmt: skip
    out.append(_c("baseline_hash_valid", base_bad))
    out.append(_c("bootstrap_matched_to_baseline", boot_bad))
    out.append(_c("no_positive_local_mint_after_baseline", mint_bad))
    orphans = ma.orphan_grant_entries(db)
    out.append(
        _c("no_orphan_grant_ledger_entries", [f"ledger ids {orphans[:10]}"] if orphans else None)
    )

    held_total = db.scalar(select(Hold.id).where(Hold.status == HoldStatus.ACTIVE).limit(1))
    out.append(Check("active_holds", PASS, "present" if held_total else "none"))

    if include_queue:
        from scripts import queue_readiness as qr

        bad = [c for c in qr.evaluate(db, now=now) if c.level == qr.FAIL]
        out.append(_c("queue_readiness", [f"{c.name}: {c.reason}" for c in bad],
                      "no stuck/unknown beyond limits"))  # fmt: skip

    if include_usage:
        out.extend(_usage_checks(db, now, reconciliation_fetch))

    if settings.env == "production":
        out.append(_c("production_ack", None if settings.money_authority_ack else
                      "SMS_MONEY_AUTHORITY_ACK=true is required in production",
                      "ack present"))  # fmt: skip
    else:
        out.append(Check("production_ack", PASS, "not production"))
    return out


def ok(checks: list[Check]) -> bool:
    return not any(c.level == FAIL for c in checks)


def _usage_checks(
    db: Session, now: datetime, fetch: Callable[[uuid.UUID], dict] | None
) -> list[Check]:
    """M9-d: raportimi i përdorimit + rakordimi me Central. Vetëm lexim; Central thirret VETËM nga ky mjet CLI."""
    from app.models.money_usage import R_FAILED
    from app.services import money_usage as mu
    from packages.contracts.control_plane.money import usage_v1 as uv

    out: list[Check] = []
    out.append(_c("usage_reporting_enabled", None if settings.money_reporting else
                  "SMS_MONEY_REPORTING=false: Central cannot prove what Enterprise holds/spends",
                  "reporting on"))  # fmt: skip
    with mu.snapshot_session(db.get_bind()) as snap:
        built = mu.build_drafts(snap, now=now)
    sent, latest = mu.last_sent_by_key(db), mu.latest_by_key(db)
    stale, failed, eq_bad, hold_bad = [], [], [], []
    warn = []
    for d in built.drafts:
        k = (d.enterprise_id, d.product_id, d.currency)
        rep = uv.UsageReportV1.parse({**d.doc, "report_id": str(uuid.uuid4()), "report_seq": 1,
                                      "generated_at": uv.format_ts(now)})  # fmt: skip
        if rep.conservation_gap() != 0:
            eq_bad.append(f"{d.currency}: conservation gap {rep.conservation_gap()}")
        w = d.doc["wallet"]
        if (
            w["held"] != w["active_hold_total"]
            or w["gross"].startswith("-")
            or w["available"].startswith("-")
        ):
            hold_bad.append(f"{d.currency}: held={w['held']} active_holds={w['active_hold_total']}")
        s = sent.get(k)
        if s is None:
            stale.append(f"{d.currency}: no usage report was ever delivered")
        else:
            age = (as_utc(now) - as_utc(s.sent_at)).total_seconds() if s.sent_at else 10**9
            if age > settings.money_report_stale_seconds:
                stale.append(
                    f"{d.currency}: last delivered report is {int(age)} s old (> {settings.money_report_stale_seconds})"
                )
            elif age > settings.money_report_fresh_seconds:
                warn.append(f"{d.currency}: last delivered report is {int(age)} s old")
        top = latest.get(k)
        if top is not None and top.status == R_FAILED:
            failed.append(
                f"{d.currency}: latest report {top.report_id} was rejected by Central ({top.last_error})"
            )
    if not built.drafts:
        out.append(Check("usage_report_fresh", PASS, "no reportable wallet"))
    elif stale:
        out.append(Check("usage_report_fresh", FAIL, "; ".join(stale)))
    elif warn:
        out.append(Check("usage_report_fresh", WARN, "; ".join(warn)))
    else:
        out.append(Check("usage_report_fresh", PASS, "delivered within the freshness window"))
    out.append(_c("usage_report_delivery", failed, "no permanently rejected latest report"))
    out.append(
        _c(
            "usage_report_equation",
            eq_bad + hold_bad,
            "conservation equation and hold invariant hold",
        )
    )
    if fetch is None:
        out.append(_c("central_reconciliation", "Central reconciliation is required in production (use --with-central)"
                      if settings.env == "production" else None, "skipped (not production; use --with-central)"))  # fmt: skip
        return out
    from app.services.control_plane_client import CpError

    bad, notes = [], []
    for eid in sorted({d.enterprise_id for d in built.drafts}, key=str):
        try:
            res = fetch(eid)
        except CpError as e:
            bad.append(f"{eid}: Central unreachable or denied ({type(e).__name__})")
            continue
        if res["status"] in ("FAIL", "CRITICAL"):
            top = [f"{x['code']}[{x['severity']}]" for x in res.get("discrepancies", [])[:5]]
            bad.append(f"{eid}: reconciliation {res['status']} {top}")
        elif res["status"] == "WARN":
            notes.append(f"{eid}: reconciliation WARN")
    if bad:
        out.append(Check("central_reconciliation", FAIL, "; ".join(bad)))
    elif notes:
        out.append(Check("central_reconciliation", WARN, "; ".join(notes)))
    else:
        out.append(Check("central_reconciliation", PASS, "Central reconciliation PASS"))
    return out
