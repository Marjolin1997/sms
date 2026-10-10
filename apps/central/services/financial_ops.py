"""M9-f: pamja operacionale financiare e Central (VETËM LEXIM) — statistika, alarme, reversal-et e pazgjidhura, readiness.

Asnjë mutacion, asnjë korrigjim: rakordimi (`money_reconciliation`) mbetet burimi i diskrepancave; kjo shtresë i grupon,
i shpjegon dhe u jep nivel alarmi (`CRITICAL`/`WARN`) sipas rregullave të dokumentuara te `docs/M9_MONEY_AUDIT.md` (M9-f).
Diskrepanca origjinale mbetet e dukshme sa kohë ekziston arsyeja; s'ka veprim "shëno si zgjidhur".
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.timeutil import utcnow
from apps.central.models.money import (
    APPROVED,
    GRANT_ACTIVE,
    GRANT_REVERSED,
    PENDING,
    REJECTED,
    CreditAccount,
    CreditGrant,
    Payment,
)
from apps.central.models.pricing import V_ACTIVE, PriceAssignment, PriceVersion
from apps.central.models.service_auth import ServiceClient, ServiceClientEnterprise, ServiceKey
from apps.central.services import credit_accounts, pricing, usage_reports
from apps.central.services import money_reconciliation as mr

CRITICAL, WARN = "CRITICAL", "WARN"
PASS, FAIL = "PASS", "FAIL"
FINANCIAL_SCOPES = ("money:read", "money:report", "pricing:read")
# Kodet e rakordimit që e bëjnë një furnizim parash të thyer kur autoriteti është `central`.
_FEED_CODES = frozenset(
    {
        mr.CURSOR_STALE,
        mr.CURSOR_BEHIND,
        mr.CURSOR_AHEAD,
        mr.MISSING_GRANT,
        mr.MISSING_REVERSAL,
        mr.EPOCH_MISMATCH,
    }
)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: str
    reason: str


def _iso(d: datetime | None) -> str | None:
    return None if d is None else mr.as_utc(d).isoformat()


def _age(now: datetime, then: datetime | None) -> int | None:
    return None if then is None else max(0, int(mr._age(now, then)))


def payments_summary(db: Session, now: datetime) -> dict:
    by = {
        s: n for s, n in db.execute(select(Payment.status, func.count()).group_by(Payment.status))
    }
    oldest = db.scalar(select(func.min(Payment.created_at)).where(Payment.status == PENDING))
    stale_cutoff = settings.payment_pending_stale_seconds
    stale = 0
    if oldest is not None:
        for created in db.scalars(select(Payment.created_at).where(Payment.status == PENDING)):
            if mr._age(now, created) > stale_cutoff:
                stale += 1
    return {PENDING: by.get(PENDING, 0), APPROVED: by.get(APPROVED, 0), REJECTED: by.get(REJECTED, 0),
            "oldest_pending_age_seconds": _age(now, oldest), "stale_pending": stale,
            "stale_after_seconds": stale_cutoff}  # fmt: skip


def accounts_summary(db: Session) -> list[dict]:
    out = []
    for a in db.scalars(select(CreditAccount).order_by(CreditAccount.created_at, CreditAccount.id)):
        t = credit_accounts.totals(db, a.id)
        out.append({"account_id": str(a.id), "enterprise_id": str(a.enterprise_id), "product_id": str(a.product_id),
                    "currency": a.currency, "status": a.status, "available_to_grant": str(t.available_to_grant),
                    "outstanding_grants": str(t.outstanding_grants), "funds": str(t.funds)})  # fmt: skip
    return out


def grants_summary(db: Session) -> dict:
    by = {s: (n, str(total)) for s, n, total in db.execute(
        select(CreditGrant.status, func.count(), func.coalesce(func.sum(CreditGrant.amount), 0)).group_by(CreditGrant.status))}  # fmt: skip
    return {"active": by.get(GRANT_ACTIVE, (0, "0"))[0], "reversed": by.get(GRANT_REVERSED, (0, "0"))[0],
            "active_amount": by.get(GRANT_ACTIVE, (0, "0"))[1]}  # fmt: skip


def reports_summary(db: Session, now: datetime) -> list[dict]:
    t = mr.Thresholds.from_settings()
    out = []
    for r in usage_reports.latest_per_key(db):
        age = mr._age(now, r.received_at)
        level = PASS if age <= t.report_fresh_s else WARN if age <= t.report_stale_s else FAIL
        out.append({"enterprise_id": str(r.enterprise_id), "product_id": str(r.product_id), "currency": r.currency,
                    "report_seq": r.report_seq, "authority_mode": r.authority_mode, "received_at": _iso(r.received_at),
                    "age_seconds": int(age), "freshness": level, "money_cursor_seq": r.money_cursor_seq,
                    "available": str(r.available), "held": str(r.held), "gross": str(r.gross)})  # fmt: skip
    return out


def pricing_summary(db: Session, now: datetime) -> dict:
    epoch, revision = pricing.read_state(db)
    active = int(
        db.scalar(
            select(func.count()).select_from(PriceVersion).where(PriceVersion.status == V_ACTIVE)
        )
        or 0
    )
    # çdo llogari me çmim të caktuar sot? (enterprise, product) pa caktim efektiv = WARN (Central s'e di modin e Enterprise)
    missing = []
    for a in db.scalars(select(CreditAccount)):
        if pricing.assignment_at(db, a.enterprise_id, a.product_id, now) is None:
            missing.append({"enterprise_id": str(a.enterprise_id), "product_id": str(a.product_id)})
    n_assign = int(db.scalar(select(func.count()).select_from(PriceAssignment)) or 0)
    return {"epoch": str(epoch), "revision": revision, "active_versions": active, "assignments": n_assign,
            "accounts_without_price_assignment": missing}  # fmt: skip


def unresolved_reversals(db: Session, result: mr.Result) -> list[dict]:
    """Çdo `unresolved_reversal`: grant_id, shuma, monedha, available/held, mosha, arsyeja, gjendja Central dhe Enterprise.
    Vetëm lexim; veprimi i operatorit është një veprim financiar real (shih docs), kurrë "shëno si zgjidhur"."""
    reports = {
        (str(r.enterprise_id), str(r.product_id), r.currency): r
        for r in usage_reports.latest_per_key(db)
    }
    out = []
    for d in result.discrepancies:
        if d.code != mr.UNRESOLVED_REVERSAL:
            continue
        g = db.get(CreditGrant, uuid.UUID(d.subject))
        ent_state = None
        r = reports.get((d.enterprise_id, d.product_id, d.currency))
        if r is not None:
            for eg in r.payload.get("grants", []):
                if eg["grant_id"] == d.subject:
                    ent_state = {
                        "status": eg["status"],
                        "detail": eg["detail"],
                        "updated_at": eg["updated_at"],
                    }
        x = d.extra
        out.append({
            "grant_id": d.subject, "enterprise_id": d.enterprise_id, "product_id": d.product_id, "currency": d.currency,
            "amount": x.get("reversal_amount"), "available": x.get("available"), "held": x.get("held"),
            "age_seconds": x.get("age_seconds"), "severity": d.severity,
            "reason": None if g is None else g.reversal_reason,
            "central_state": None if g is None else {"status": g.status, "reversed_at": _iso(g.reversed_at)},
            "enterprise_state": ent_state, "detail": d.detail,
        })  # fmt: skip
    return out


def _mode_of(result: mr.Result) -> dict:
    return {(k.enterprise_id, k.product_id, k.currency): k.authority_mode for k in result.keys}


def alerts(result: mr.Result, payments: dict, pricing_: dict) -> list[dict]:
    """Rregulla deterministe: CRITICAL = rrezik parash/ndërprerje; WARN = ngecje/drift i pakritik. INFO nuk alarmon."""
    modes = _mode_of(result)
    out: list[dict] = []

    def add(level, code, d: mr.Discrepancy, why):
        out.append({"level": level, "code": code, "enterprise_id": d.enterprise_id, "product_id": d.product_id,
                    "currency": d.currency, "subject": d.subject, "message": why})  # fmt: skip

    for d in result.discrepancies:
        if d.severity == mr.INFO:
            continue
        central_mode = modes.get((d.enterprise_id, d.product_id, d.currency)) == "central"
        if d.code == mr.UNEXPLAINED_CREDIT:
            add(
                CRITICAL,
                "unexplained_positive_credit",
                d,
                d.detail or "wallet gained credit not explained by any grant",
            )
        elif d.code == mr.NEGATIVE:
            add(CRITICAL, "negative_invariant", d, d.detail or "negative balance")
        elif d.code in (mr.WALLET_FORMULA, mr.HOLD_TOTAL):
            add(CRITICAL, "wallet_hold_mismatch", d, d.detail or d.code)
        elif d.code == mr.UNRESOLVED_REVERSAL and d.severity in (mr.FAIL, mr.CRITICAL):
            add(CRITICAL, "unresolved_reversal", d, d.detail)
        elif d.code in _FEED_CODES and d.severity in (mr.FAIL, mr.CRITICAL) and central_mode:
            add(CRITICAL, "money_feed_broken", d, f"{d.code}: {d.detail}")
        elif d.severity == mr.CRITICAL:
            add(CRITICAL, d.code, d, d.detail)
        elif d.code == mr.STALE_REPORT:
            add(WARN, "stale_usage_report", d, d.detail or "usage report is stale")
        elif d.code in (mr.CURSOR_BEHIND, mr.CURSOR_STALE):
            add(WARN, "cursor_lag", d, d.detail or d.code)
        else:
            add(WARN, "reconciliation_drift", d, f"{d.code}: {d.detail}")
    if payments["stale_pending"]:
        out.append({"level": WARN, "code": "stale_pending_payments", "enterprise_id": None, "product_id": None,
                    "currency": None, "subject": None,
                    "message": f"{payments['stale_pending']} payment(s) pending > {payments['stale_after_seconds']}s"})  # fmt: skip
    for m in pricing_["accounts_without_price_assignment"]:
        out.append({"level": WARN, "code": "price_assignment_missing", "enterprise_id": m["enterprise_id"],
                    "product_id": m["product_id"], "currency": None, "subject": None,
                    "message": "credit account has no effective price assignment"})  # fmt: skip
    order = {CRITICAL: 0, WARN: 1}
    return sorted(
        out,
        key=lambda a: (order[a["level"]], a["code"], a["enterprise_id"] or "", a["subject"] or ""),
    )


def snapshot(db: Session, now: datetime | None = None, result: mr.Result | None = None) -> dict:
    now = now or utcnow()
    result = result or mr.reconcile(db, now=now)
    payments = payments_summary(db, now)
    pr = pricing_summary(db, now)
    return {
        "generated_at": _iso(now), "reconciliation": {"status": result.status, "counts": result.counts()},
        "payments": payments, "accounts": accounts_summary(db), "grants": grants_summary(db),
        "usage_reports": reports_summary(db, now), "unresolved_reversals": unresolved_reversals(db, result),
        "pricing": pr, "alerts": alerts(result, payments, pr),
    }  # fmt: skip


# --- readiness (pjesa Central e gate-it financiar) -------------------------------------------------------------------


def readiness_checks(
    db: Session, now: datetime | None = None, result: mr.Result | None = None
) -> list[Check]:
    now = now or utcnow()
    result = result or mr.reconcile(db, now=now)
    out: list[Check] = []
    crit = [d for d in result.discrepancies if d.severity == mr.CRITICAL]
    out.append(Check("reconciliation_no_critical", FAIL if crit else PASS,
                     "; ".join(f"{d.code} {d.subject or ''}" for d in crit[:8]) if crit else "no CRITICAL discrepancy"))  # fmt: skip
    mint = [d for d in result.discrepancies if d.code == mr.UNEXPLAINED_CREDIT]
    out.append(Check("no_unexplained_positive_mint", FAIL if mint else PASS,
                     f"{len(mint)} unexplained positive credit(s)" if mint else "none"))  # fmt: skip
    rev = [d for d in result.discrepancies if d.code == mr.UNRESOLVED_REVERSAL]
    unsafe = [d for d in rev if d.severity in (mr.FAIL, mr.CRITICAL)]
    out.append(Check("no_unresolved_unsafe_reversal", FAIL if unsafe else (WARN if rev else PASS),
                     f"{len(unsafe)} beyond threshold, {len(rev)} total" if rev else "none"))  # fmt: skip
    fails = [
        d
        for d in result.discrepancies
        if d.severity == mr.FAIL and d.code not in (mr.UNRESOLVED_REVERSAL,)
    ]
    out.append(Check("reconciliation_no_fail", FAIL if fails else PASS,
                     "; ".join(f"{d.code} {d.subject or ''}" for d in fails[:8]) if fails else "no FAIL discrepancy"))  # fmt: skip
    warns = [d for d in result.discrepancies if d.severity == mr.WARN]
    out.append(Check("reconciliation_no_drift", WARN if warns else PASS,
                     f"{len(warns)} WARN discrepancies" if warns else "no drift"))  # fmt: skip
    reports = reports_summary(db, now)
    if not reports:
        out.append(Check("usage_reports_fresh", FAIL, "Central has received no usage report"))
    else:
        worst = max(reports, key=lambda r: {PASS: 0, WARN: 1, FAIL: 2}[r["freshness"]])
        bad = [r for r in reports if r["freshness"] != PASS]
        out.append(Check("usage_reports_fresh", worst["freshness"],
                         f"{len(bad)}/{len(reports)} key(s) not fresh (oldest {max(r['age_seconds'] for r in reports)}s)" if bad
                         else f"{len(reports)} key(s) fresh"))  # fmt: skip
    pend = payments_summary(db, now)
    out.append(Check("payments_not_stale", WARN if pend["stale_pending"] else PASS,
                     f"{pend['stale_pending']} pending payment(s) older than {pend['stale_after_seconds']}s" if pend["stale_pending"] else "none"))  # fmt: skip
    out.extend(pricing_checks(db, now))
    base = [
        d for d in result.discrepancies if d.code in (mr.BASELINE_MISMATCH, mr.BASELINE_PENDING)
    ]
    out.append(Check("baseline_cutover_status", FAIL if any(d.code == mr.BASELINE_MISMATCH for d in base) else (WARN if base else PASS),
                     "; ".join(f"{d.code} {d.subject or ''}" for d in base[:8]) if base else "baseline consistent"))  # fmt: skip
    out.extend(_credential_checks(db))
    return out


def pricing_checks(db: Session, now: datetime) -> list[Check]:
    """Çmimi i klientit në Central: version aktiv, caktim efektiv, monedhë e librit = monedha e llogarisë, version efektiv."""
    from apps.central.models.pricing import PriceBook
    from packages.contracts.control_plane.pricing import v1 as pv

    out: list[Check] = []
    active = int(
        db.scalar(
            select(func.count()).select_from(PriceVersion).where(PriceVersion.status == V_ACTIVE)
        )
        or 0
    )
    out.append(Check("pricing_active_version", PASS if active else FAIL,
                     f"{active} active price version(s)" if active else "no active price version"))  # fmt: skip
    no_assign, cur_bad, no_version = [], [], []
    for a in db.scalars(select(CreditAccount)):
        key = f"{a.enterprise_id}/{a.product_id}"
        asg = pricing.assignment_at(db, a.enterprise_id, a.product_id, now)
        if asg is None:
            no_assign.append(key)
            continue
        book = db.get(PriceBook, asg.price_book_id)
        if book.currency != a.currency:
            cur_bad.append(f"{key}: book {book.currency} vs account {a.currency}")
        ver, _why = pv.select_version(pricing.versions_of(db, book.id), now)
        if ver is None:
            no_version.append(key)
    out.append(Check("pricing_assignment_present", FAIL if no_assign else PASS,
                     f"no effective assignment: {no_assign[:6]}" if no_assign else "every credit account has an assignment"))  # fmt: skip
    out.append(Check("pricing_currency_matches_account", FAIL if cur_bad else PASS,
                     "; ".join(cur_bad[:6]) if cur_bad else "book currency equals account currency"))  # fmt: skip
    out.append(Check("pricing_effective_version", FAIL if no_version else PASS,
                     f"assigned book without an effective version: {no_version[:6]}" if no_version else "every assigned book has an effective version"))  # fmt: skip
    return out


def _credential_checks(db: Session) -> list[Check]:
    """Klientët me scope financiare: aktiv + ≥1 çelës aktiv + ≥1 enterprise të autorizuar; përndryshe WARN (gate s'mund t'i besojë)."""
    out = []
    issues = []
    clients = [c for c in db.scalars(select(ServiceClient).where(ServiceClient.status == "active"))
               if set(c.scopes) & set(FINANCIAL_SCOPES)]  # fmt: skip
    for c in clients:
        keys = db.scalar(
            select(func.count())
            .select_from(ServiceKey)
            .where(ServiceKey.client_pk == c.id, ServiceKey.status == "active")
        )
        ents = db.scalar(
            select(func.count())
            .select_from(ServiceClientEnterprise)
            .where(ServiceClientEnterprise.client_pk == c.id)
        )
        if not keys or not ents:
            issues.append(f"{c.client_id}: active_keys={keys} enterprises={ents}")
    if not clients:
        out.append(
            Check(
                "financial_service_credentials",
                FAIL,
                "no active service client holds a financial scope",
            )
        )
    else:
        out.append(Check("financial_service_credentials", WARN if issues else PASS,
                         "; ".join(issues[:6]) if issues else f"{len(clients)} active client(s) with financial scopes"))  # fmt: skip
    return out
