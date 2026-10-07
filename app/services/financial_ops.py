"""M9-f: pamja operacionale financiare e Enterprise (VETËM LEXIM) + alarmet me prag të shpjeguar.

`snapshot(db)` → numra të sigurt (pa sekrete, pa PII, pa numra telefoni); `alerts(snap)` → lista e kushteve me nivel
`CRITICAL`/`WARN` (kodi, subjekti, arsyeja, veprimi). Asnjë integrim alarmi: ky është kontrata që lexon stafi/mjeti
(`scripts.financial_ops`, `GET /v1/admin/financial`, `apps.central.tools.financial_readiness`). Asnjë mutacion.
"""

import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import as_utc, utcnow
from app.models.email import Email, EmailStatus
from app.models.money_authority import (
    G_RECON,
    MoneyBaseline,
    MoneyCursor,
    MoneyGrant,
)
from app.models.money_usage import R_FAILED, R_PENDING, R_RETRY, R_SENDING, UsageReport
from app.models.pricing import PricingComparison, PricingState
from app.models.sending import Message, MessageStatus
from app.models.wallet import Hold, HoldStatus, LedgerEntry, Wallet
from app.services import billing_usage

CRITICAL, WARN = "CRITICAL", "WARN"
log = logging.getLogger("sms.financial_ops")


def _age(now: datetime, then: datetime | None) -> int | None:
    return None if then is None else max(0, int((now - as_utc(then)).total_seconds()))


def _s(d) -> str:
    return str(Decimal(d or 0).quantize(Decimal("0.000001")))


def _unknown(db: Session, now: datetime) -> dict:
    """UNKNOWN me hold aktiv = para e ngrirë dhe rrezik operacional (hold nuk lirohet vetë)."""
    sms = db.execute(
        select(Message.currency, func.count(), func.min(Message.updated_at), func.coalesce(func.sum(Hold.amount), 0))
        .join(Hold, Hold.id == Message.hold_id, isouter=True)
        .where(Message.status == MessageStatus.UNKNOWN)
        .group_by(Message.currency)
    ).all()  # fmt: skip
    mail = db.execute(
        select(func.count(), func.min(Email.updated_at)).where(Email.status == EmailStatus.UNKNOWN)
    ).one()
    oldest = [as_utc(r[2]) for r in sms if r[2] is not None]
    if mail[1] is not None:
        oldest.append(as_utc(mail[1]))
    return {
        "sms": sum(r[1] for r in sms),
        "email": int(mail[0]),
        "oldest_age_seconds": _age(now, min(oldest)) if oldest else None,
        "held_amount_by_currency": {r[0]: _s(r[3]) for r in sms},
    }


def _wallets(db: Session) -> dict:
    """Totalet nga ledger-i (hyrja e fundit per wallet) + kontrolli hold-vs-ledger + negativët."""
    last = select(func.max(LedgerEntry.id).label("id")).group_by(LedgerEntry.wallet_id).subquery()
    rows = db.execute(
        select(Wallet.id, Wallet.currency, LedgerEntry.available_after, LedgerEntry.held_after)
        .join(LedgerEntry, LedgerEntry.wallet_id == Wallet.id)
        .join(last, last.c.id == LedgerEntry.id)
    ).all()
    holds = dict(
        db.execute(
            select(Hold.wallet_id, func.coalesce(func.sum(Hold.amount), 0))
            .where(Hold.status == HoldStatus.ACTIVE)
            .group_by(Hold.wallet_id)
        ).all()
    )
    by_cur: dict[str, dict[str, Decimal]] = {}
    mismatch, negative = [], []
    for wid, cur, avail, held in rows:
        t = by_cur.setdefault(
            cur, {"available": Decimal(0), "held": Decimal(0), "wallets": Decimal(0)}
        )
        t["available"] += avail
        t["held"] += held
        t["wallets"] += 1
        if avail < 0 or held < 0:
            negative.append(wid)
        if Decimal(holds.get(wid, 0)) != held:
            mismatch.append(
                {"wallet_id": wid, "ledger_held": _s(held), "active_holds": _s(holds.get(wid, 0))}
            )
    return {
        "by_currency": {c: {k: _s(v) if k != "wallets" else int(v) for k, v in t.items()} for c, t in by_cur.items()},
        "hold_mismatch": mismatch[:50], "hold_mismatch_count": len(mismatch),
        "negative_wallets": negative[:50], "negative_wallet_count": len(negative),
    }  # fmt: skip


def _money(db: Session, now: datetime) -> dict:
    cur = db.get(MoneyCursor, 1)
    by_status = {
        s: n
        for s, n in db.execute(select(MoneyGrant.status, func.count()).group_by(MoneyGrant.status))
    }
    unresolved = []
    for g in db.scalars(
        select(MoneyGrant).where(MoneyGrant.status == G_RECON).order_by(MoneyGrant.updated_at)
    ):
        w = db.get(Wallet, g.wallet_id) if g.wallet_id else None
        avail = held = None
        if w is not None:
            last = db.scalar(
                select(LedgerEntry)
                .where(LedgerEntry.wallet_id == w.id)
                .order_by(LedgerEntry.id.desc())
                .limit(1)
            )
            avail, held = (
                (_s(last.available_after), _s(last.held_after))
                if last
                else ("0.000000", "0.000000")
            )
        unresolved.append({
            "grant_id": str(g.grant_id), "amount": _s(g.amount), "currency": g.currency, "available": avail,
            "held": held, "age_seconds": _age(now, g.updated_at), "reason": g.detail,
            "enterprise_state": g.status, "reversed_seq": g.reversed_seq,
        })  # fmt: skip
    baselines = {
        s: n
        for s, n in db.execute(
            select(MoneyBaseline.status, func.count()).group_by(MoneyBaseline.status)
        )
    }
    return {
        "authority": settings.money_authority,
        "cursor": None
        if cur is None
        else {
            "epoch": str(cur.epoch) if cur.epoch else None,
            "last_seq": cur.last_seq,
            "age_seconds": _age(now, cur.last_success_at),
            "has_error": cur.last_error is not None,
            "last_error": (cur.last_error or "")[:200] or None,
        },  # fmt: skip
        "grants_by_status": by_status,
        "unresolved_reversals": unresolved,
        "baselines_by_status": baselines,
    }


def _usage_outbox(db: Session, now: datetime) -> dict:
    counts = {
        s: n
        for s, n in db.execute(
            select(UsageReport.status, func.count()).group_by(UsageReport.status)
        )
    }
    open_states = (R_PENDING, R_RETRY, R_SENDING, R_FAILED)
    oldest = db.scalar(
        select(func.min(UsageReport.created_at)).where(UsageReport.status.in_(open_states))
    )
    last_sent = db.scalar(select(func.max(UsageReport.sent_at)))
    return {"by_status": counts, "oldest_unsent_age_seconds": _age(now, oldest),
            "last_sent_age_seconds": _age(now, last_sent)}  # fmt: skip


def _pricing(db: Session, now: datetime) -> dict:
    st = db.get(PricingState, 1)
    rows = db.execute(
        select(PricingComparison.kind, PricingComparison.classification, func.count())
        .where(PricingComparison.ok.is_(False))
        .group_by(PricingComparison.kind, PricingComparison.classification)
    ).all()
    total = int(db.scalar(select(func.count()).select_from(PricingComparison)) or 0)
    return {
        "authority": settings.pricing_authority,
        "snapshot": None
        if st is None or st.active_snapshot_id is None
        else {
            "snapshot_id": str(st.active_snapshot_id),
            "epoch": str(st.epoch),
            "revision": st.revision,
            "sync_age_seconds": _age(now, st.last_success_at),
            "has_error": st.last_error is not None,
            "last_error": (st.last_error or "")[:200] or None,
        },  # fmt: skip
        "comparisons": {"total": total, "mismatches": {f"{k}:{c}": n for k, c, n in rows}},
    }


def snapshot(db: Session, now: datetime | None = None) -> dict:
    now = as_utc(now or utcnow())
    return {
        "generated_at": now.isoformat(),
        "unknown": _unknown(db, now),
        "wallets": _wallets(db),
        "money": _money(db, now),
        "usage_reports": _usage_outbox(db, now),
        "billing_usage": billing_usage.stats(db, now),
        "pricing": _pricing(db, now),
    }


def alerts(snap: dict) -> list[dict]:
    """Pragjet vijnë nga konfigurimi (`SMS_FINANCIAL_*`, `SMS_PRICING_STALE_*`). Çdo alarm: code, level, subject, message."""
    s = settings
    out: list[dict] = []

    def add(level, code, subject, message):
        out.append({"level": level, "code": code, "subject": subject, "message": message})

    w = snap["wallets"]
    if w["negative_wallet_count"]:
        add(
            CRITICAL,
            "negative_balance",
            f"wallets={w['negative_wallets'][:5]}",
            "a wallet has a negative available/held balance",
        )
    if w["hold_mismatch_count"]:
        add(CRITICAL, "wallet_hold_mismatch", f"wallets={[m['wallet_id'] for m in w['hold_mismatch'][:5]]}",
            "ledger held != sum of active holds")  # fmt: skip
    m = snap["money"]
    for g in m["unresolved_reversals"]:
        if (g["age_seconds"] or 0) > s.financial_unresolved_reversal_critical_seconds:
            add(CRITICAL, "unresolved_reversal", g["grant_id"],
                f"grant reversal not applied for {g['age_seconds']}s (amount {g['amount']} {g['currency']}, available {g['available']})")  # fmt: skip
    cur = m["cursor"]
    if m["authority"] == "central":
        if cur is None or cur["epoch"] is None:
            add(
                CRITICAL,
                "money_feed_broken",
                "cursor",
                "authority=central but the money cursor is not initialised",
            )
        elif cur["has_error"]:
            add(CRITICAL, "money_feed_broken", "cursor", f"money cursor error: {cur['last_error']}")
        elif (
            cur["age_seconds"] is None
            or cur["age_seconds"] > s.financial_money_cursor_critical_seconds
        ):
            add(CRITICAL, "money_feed_broken", "cursor", f"money cursor age {cur['age_seconds']}s > {s.financial_money_cursor_critical_seconds}s")  # fmt: skip
    if cur and cur["age_seconds"] is not None and m["authority"] != "local":
        if (
            s.financial_money_cursor_warn_seconds
            < cur["age_seconds"]
            <= s.financial_money_cursor_critical_seconds
        ):
            add(WARN, "money_cursor_lag", "cursor", f"money cursor age {cur['age_seconds']}s")
    p = snap["pricing"]
    snapshot_ = p["snapshot"]
    if p["authority"] == "central":
        if snapshot_ is None:
            add(
                CRITICAL,
                "pricing_missing",
                "snapshot",
                "authority=central but no complete pricing snapshot is active",
            )
        elif (
            snapshot_["has_error"]
            or (snapshot_["sync_age_seconds"] or 0) > s.pricing_stale_fail_seconds
        ):
            add(CRITICAL, "pricing_missing", snapshot_["snapshot_id"],
                f"pricing sync stale/errored (age {snapshot_['sync_age_seconds']}s): last complete snapshot still in use")  # fmt: skip
    elif p["authority"] == "shadow":
        mism = sum(p["comparisons"]["mismatches"].values())
        if mism:
            add(WARN, "shadow_pricing_mismatch", "comparisons", f"{mism} shadow pricing mismatches: {p['comparisons']['mismatches']}")  # fmt: skip
    if (
        snapshot_
        and (snapshot_["sync_age_seconds"] or 0) > s.pricing_stale_warn_seconds
        and p["authority"] != "local"
    ):
        add(
            WARN,
            "pricing_snapshot_stale",
            snapshot_["snapshot_id"],
            f"pricing sync age {snapshot_['sync_age_seconds']}s",
        )
    u = snap["usage_reports"]
    if s.money_reporting and (u["oldest_unsent_age_seconds"] or 0) > s.money_report_stale_seconds:
        add(
            WARN,
            "usage_reports_stale",
            "outbox",
            f"oldest unsent usage report is {u['oldest_unsent_age_seconds']}s old",
        )
    b = snap["billing_usage"]
    if b["outbox"].get(R_FAILED):
        add(CRITICAL, "billing_usage_rejected", "outbox",
            f"{b['outbox'][R_FAILED]} billing usage report(s) rejected permanently by Central (billing is blocked until resolved)")  # fmt: skip
    if s.billing_usage_reporting and (b["oldest_unsent_age_seconds"] or 0) > s.billing_usage_stale_seconds:
        add(WARN, "billing_usage_stale", "outbox", f"oldest unsent billing usage report is {b['oldest_unsent_age_seconds']}s old")  # fmt: skip
    k = snap["unknown"]
    if (
        k["oldest_age_seconds"] is not None
        and k["oldest_age_seconds"] > s.financial_unknown_warn_seconds
    ):
        add(WARN, "unknown_backlog", "queue", f"{k['sms']} sms / {k['email']} email UNKNOWN, oldest {k['oldest_age_seconds']}s, held {k['held_amount_by_currency']}")  # fmt: skip
    order = {CRITICAL: 0, WARN: 1}
    return sorted(out, key=lambda a: (order[a["level"]], a["code"], a["subject"]))


def emit(alerts_: list[dict]) -> None:
    """Stili i log-ut ekzistues (`ALERT …`): CRITICAL ⇒ ERROR, WARN ⇒ WARNING. Pa integrim të jashtëm."""
    for a in alerts_:
        (log.error if a["level"] == CRITICAL else log.warning)(
            "ALERT %s %s %s: %s", a["level"], a["code"], a["subject"], a["message"]
        )
