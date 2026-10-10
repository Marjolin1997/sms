"""M9-d: rakordimi financiar Central ↔ Enterprise. VETËM-LEXIM: pa mutacion, pa korrigjim automatik (asnjë
wallet +=/−=, asnjë replay i grant-it, asnjë reset kursori). Raporton mospërputhje; veprimi i operatorit është eksplicit.

Krahason TRE domene pa krijuar të vërtetë të dytë:
  1. çfarë ka autorizuar Central (grant-e `credit_grants` + ngjarjet e `money_events`, me `grant_id` të qëndrueshëm);
  2. çfarë ka marrë Enterprise (grant-et në raportin AKTUAL: `grant_id`, status, shumë, produkt, monedhë, seq);
  3. çfarë mban/shpenzon/rezervon Enterprise (wallet, holds, flukset kumulative nga ledger-i i pandryshueshëm).
Grant-et lidhen VETËM me `grant_id` (kurrë me shumë). Pamja është deterministike mbi (DB, `now`, pragjet).

Ekuacioni i ruajtjes (nga llojet reale të ledger-it; shih `usage_v1`):
  gross = baseline_gross + grants_applied − grant_reversals − captured − negative_adjustments
          − invoice_debits − other_debits + positive_local_credit
Ashpërsia: INFO (vetëm informim) < WARN < FAIL < CRITICAL. Mënyra `local` = informative: mospërputhjet e
autoritetit zbriten në INFO/WARN; invariantet e brendshme të wallet-it mbeten të plota (janë fakte lokale)."""

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.timeutil import utcnow
from apps.central.models.money import (
    EVENT_GRANT_ISSUED,
    CreditAccount,
    CreditGrant,
    MoneyEvent,
)
from apps.central.services import money_feed, usage_reports
from packages.contracts.control_plane.money import usage_v1 as uv

INFO, WARN, FAIL, CRITICAL = "INFO", "WARN", "FAIL", "CRITICAL"
_RANK = {INFO: 0, WARN: 1, FAIL: 2, CRITICAL: 3}
PASS = "PASS"

# kategoritë (vetëm ato që mbështeten nga të dhënat reale)
MISSING_GRANT, UNEXPECTED_GRANT = "missing_grant", "unexpected_grant"
GRANT_AMOUNT, GRANT_CURRENCY, GRANT_PRODUCT = (
    "grant_amount_mismatch", "grant_currency_mismatch", "grant_product_mismatch",
)  # fmt: skip
GRANT_STATE, GRANT_UNMAPPED, GRANT_DEFERRED = (
    "grant_state_mismatch",
    "grant_unmapped",
    "grant_deferred_in_central",
)
MISSING_REVERSAL, UNRESOLVED_REVERSAL = "missing_reversal", "unresolved_reversal"
CURSOR_BEHIND, CURSOR_AHEAD, CURSOR_STALE, EPOCH_MISMATCH = (
    "cursor_behind", "cursor_ahead", "cursor_stale", "epoch_mismatch",
)  # fmt: skip
STALE_REPORT, REPORT_MISSING = "stale_report", "report_missing"
WALLET_FORMULA, HOLD_TOTAL, NEGATIVE = (
    "wallet_formula_mismatch",
    "hold_total_mismatch",
    "negative_invariant",
)
UNEXPLAINED_CREDIT, UNEXPLAINED_DEBIT = "unexplained_positive_credit", "unexplained_debit"
BASELINE_MISMATCH, BASELINE_PENDING, MODE_MISMATCH = (
    "baseline_mismatch",
    "baseline_pending",
    "authority_mode_mismatch",
)
CATEGORIES = frozenset({
    MISSING_GRANT, UNEXPECTED_GRANT, GRANT_AMOUNT, GRANT_CURRENCY, GRANT_PRODUCT, GRANT_STATE, GRANT_UNMAPPED,
    GRANT_DEFERRED, MISSING_REVERSAL, UNRESOLVED_REVERSAL, CURSOR_BEHIND, CURSOR_AHEAD, CURSOR_STALE,
    EPOCH_MISMATCH, STALE_REPORT, REPORT_MISSING, WALLET_FORMULA, HOLD_TOTAL, NEGATIVE, UNEXPLAINED_CREDIT,
    UNEXPLAINED_DEBIT, BASELINE_MISMATCH, BASELINE_PENDING, MODE_MISMATCH,
})  # fmt: skip


@dataclass(frozen=True, slots=True)
class Thresholds:
    report_fresh_s: int
    report_stale_s: int
    lag_grace_s: int
    cursor_warn_s: int
    cursor_fail_s: int
    unresolved_reversal_fail_s: int
    report_missing_grace_s: int

    @classmethod
    def from_settings(cls) -> "Thresholds":
        s = settings
        return cls(s.money_report_fresh_seconds, s.money_report_stale_seconds, s.money_cursor_lag_grace_seconds,
                   s.money_cursor_stale_warn_seconds, s.money_cursor_stale_fail_seconds,
                   s.money_unresolved_reversal_fail_seconds, s.money_report_missing_grace_seconds)  # fmt: skip


@dataclass(slots=True)
class Discrepancy:
    code: str
    severity: str
    enterprise_id: str
    product_id: str | None
    currency: str | None
    subject: str | None = None  # grant_id ose "wallet"
    expected: str | None = None
    reported: str | None = None
    detail: str = ""
    extra: dict = field(default_factory=dict)


@dataclass(slots=True)
class KeySummary:
    enterprise_id: str
    product_id: str
    currency: str
    authority_mode: str | None
    report_seq: int | None
    report_age_seconds: float | None
    ledger_max_id: int | None
    money_cursor_seq: int | None
    central_issued_total: str
    central_reversed_total: str
    enterprise_received_total: str
    applied_total: str
    deferred_total: str
    reversed_applied_total: str
    unresolved_reversal_total: str
    gross: str | None
    available: str | None
    held: str | None
    shadow_projection: dict | None


@dataclass(slots=True)
class Result:
    status: str
    generated_at: str
    discrepancies: list[Discrepancy]
    keys: list[KeySummary]

    def counts(self) -> dict[str, int]:
        c = {INFO: 0, WARN: 0, FAIL: 0, CRITICAL: 0}
        for d in self.discrepancies:
            c[d.severity] += 1
        return c

    def to_dict(self) -> dict:
        return {"status": self.status, "generated_at": self.generated_at, "counts": self.counts(),
                "discrepancies": [asdict(d) for d in self.discrepancies], "keys": [asdict(k) for k in self.keys]}  # fmt: skip


def as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _fmt(d: Decimal) -> str:
    return format(Decimal(d).quantize(Decimal("0.000001")), "f")


def _age(now: datetime, then: datetime) -> float:
    return (as_utc(now) - as_utc(then)).total_seconds()


def overall(discrepancies: list[Discrepancy]) -> str:
    worst = max((_RANK[d.severity] for d in discrepancies), default=0)
    return {0: PASS, 1: WARN, 2: FAIL, 3: CRITICAL}[worst] if worst else PASS


def exit_code(result: Result, *, strict: bool = False) -> int:
    bad = {WARN, FAIL, CRITICAL} if strict else {FAIL, CRITICAL}
    return 1 if any(d.severity in bad for d in result.discrepancies) else 0


@dataclass(slots=True)
class _Central:
    grants: dict[str, CreditGrant]
    issued: dict[str, MoneyEvent]
    reversed: dict[str, MoneyEvent]
    accounts: dict[uuid.UUID, CreditAccount]


def _load_central(db: Session, enterprise_id) -> _Central:
    grants = {
        str(g.id): g
        for g in db.scalars(select(CreditGrant).where(CreditGrant.enterprise_id == enterprise_id))
    }
    issued, reversed_ = {}, {}
    for e in db.scalars(select(MoneyEvent).where(MoneyEvent.enterprise_id == enterprise_id)):
        (issued if e.event_type == EVENT_GRANT_ISSUED else reversed_)[str(e.entity_id)] = e
    accounts = {
        a.id: a
        for a in db.scalars(
            select(CreditAccount).where(CreditAccount.enterprise_id == enterprise_id)
        )
    }
    return _Central(grants, issued, reversed_, accounts)


def reconcile(
    db: Session,
    *,
    enterprise_id=None,
    now: datetime | None = None,
    thresholds: Thresholds | None = None,
) -> Result:
    now = now or utcnow()
    t = thresholds or Thresholds.from_settings()
    epoch, central_latest = money_feed.read_state(db)
    reports = usage_reports.latest_per_key(db, enterprise_id)
    ent_ids = {r.enterprise_id for r in reports}
    q = select(CreditAccount.enterprise_id).distinct()
    if enterprise_id is not None:
        q = q.where(CreditAccount.enterprise_id == enterprise_id)
        ent_ids.add(enterprise_id)
    ent_ids |= set(db.scalars(q))
    out: list[Discrepancy] = []
    keys: list[KeySummary] = []
    for eid in sorted(ent_ids, key=str):
        c = _load_central(db, eid)
        ent_reports = [r for r in reports if r.enterprise_id == eid]
        _reconcile_enterprise(str(eid), c, ent_reports, epoch, central_latest, now, t, out, keys)
    return Result(overall(out), as_utc(now).isoformat(), out, keys)


def _reconcile_enterprise(
    eid, c: _Central, reports, epoch, central_latest, now, t: Thresholds, out, keys
) -> None:
    acct_keys = {(a.product_id, a.currency) for a in c.accounts.values()}
    rep_by_key = {(r.product_id, r.currency): r for r in reports}
    known_ids = set(c.grants)
    attributed_unexpected: set[str] = set()
    for pid, cur in sorted(acct_keys | set(rep_by_key), key=lambda k: (str(k[0]), k[1])):
        R = rep_by_key.get((pid, cur))
        doc = R.payload if R is not None else None
        my = {gid: g for gid, g in c.grants.items() if g.product_id == pid and g.currency == cur}
        add = _adder(out, eid, str(pid), cur)
        keys.append(_summary(eid, pid, cur, my, c, R, doc, now))
        if (pid, cur) not in acct_keys and doc is not None:
            # Enterprise raporton një wallet (produkt, monedhë) pa llogari të tillë në Central
            funded = Decimal(doc["flows"]["grants_applied"]) > 0 or any(
                g["currency"] == cur for g in doc["grants"]
            )
            same_cur = any(k[1] == cur for k in acct_keys)
            code = GRANT_PRODUCT if same_cur else GRANT_CURRENCY
            add(code, (INFO if doc["authority_mode"] == "local" else CRITICAL) if funded else INFO,
                subject="wallet", reported=f"{pid}/{cur}", expected=", ".join(sorted(f"{p}/{c_}" for p, c_ in acct_keys)) or "(no account)",
                detail="Enterprise reports a funded wallet for which Central has no credit account")  # fmt: skip
        if R is None:
            if my:
                oldest = max(
                    (_age(now, c.issued[g].created_at) for g in my if g in c.issued), default=0
                )
                add(REPORT_MISSING, WARN if oldest <= t.report_missing_grace_s else FAIL,
                    detail=f"Central issued {len(my)} grant(s) for this account but no usage report was ever received "
                           f"(oldest grant {int(oldest)} s old)")  # fmt: skip
            continue
        mode = doc["authority_mode"]
        local = mode == "local"
        cursor = doc["cursor"]
        wallet, flows, integ = doc["wallet"], doc["flows"], doc["integrity"]
        # -- freskia e raportit
        age = _age(now, R.received_at)
        if age > t.report_stale_s:
            add(STALE_REPORT, INFO if local else FAIL, subject="report", reported=f"{int(age)}s",
                expected=f"<= {t.report_stale_s}s", detail="latest usage report is stale")  # fmt: skip
        elif age > t.report_fresh_s:
            add(STALE_REPORT, INFO if local else WARN, subject="report", reported=f"{int(age)}s",
                expected=f"<= {t.report_fresh_s}s", detail="latest usage report is ageing")  # fmt: skip
        # -- invariantet e brendshme të wallet-it (fakte lokale: edhe në local)
        a, h, g = Decimal(wallet["available"]), Decimal(wallet["held"]), Decimal(wallet["gross"])
        if a < 0 or h < 0 or g < 0:
            add(NEGATIVE, CRITICAL, subject="wallet", reported=f"available={_fmt(a)} held={_fmt(h)}",
                expected=">= 0", detail="negative wallet balance")  # fmt: skip
        if h != Decimal(wallet["active_hold_total"]):
            add(HOLD_TOTAL, CRITICAL, subject="wallet", reported=_fmt(h), expected=wallet["active_hold_total"],
                detail=f"held != SUM(active holds) ({wallet['active_hold_count']} active)")  # fmt: skip
        rep = uv.UsageReportV1(doc)
        gap = rep.conservation_gap()
        if gap != 0:
            add(WALLET_FORMULA, CRITICAL, subject="wallet", reported=_fmt(g), expected=_fmt(g - gap),
                detail="gross != baseline + grants − reversals − captured − debits + local credit")  # fmt: skip
        if Decimal(integ["ledger_sum_available"]) != a or Decimal(integ["ledger_sum_held"]) != h:
            add(WALLET_FORMULA, CRITICAL, subject="wallet",
                reported=f"stored a={_fmt(a)} h={_fmt(h)}",
                expected=f"ledger sum a={integ['ledger_sum_available']} h={integ['ledger_sum_held']}",
                detail="stored balance differs from the sum of ledger deltas")  # fmt: skip
        if Decimal(integ["orphan_grant_credit"]) > 0:
            add(UNEXPLAINED_CREDIT, CRITICAL, subject="wallet", reported=integ["orphan_grant_credit"], expected="0.000000",
                detail="GRANT ledger credit not backed by a received grant record")  # fmt: skip
        if Decimal(integ["orphan_grant_reversal"]) > 0 or Decimal(flows["other_debits"]) > 0:
            add(UNEXPLAINED_DEBIT, CRITICAL, subject="wallet",
                reported=f"orphan_reversal={integ['orphan_grant_reversal']} other={flows['other_debits']}",
                expected="0.000000", detail="debit not explained by capture/adjustment/invoice/grant reversal")  # fmt: skip
        if not local:
            if Decimal(flows["positive_local_credit"]) > 0:
                add(UNEXPLAINED_CREDIT, CRITICAL, subject="wallet", reported=flows["positive_local_credit"],
                    expected="0.000000", detail=f"positive local credit after baseline under authority={mode}")  # fmt: skip
            if Decimal(flows["invoice_debits"]) > 0:
                add(UNEXPLAINED_DEBIT, WARN, subject="wallet", reported=flows["invoice_debits"], expected="0.000000",
                    detail=f"wallet paid invoices under authority={mode} (wallet is SMS-only)")  # fmt: skip
            # -- kursori
            if cursor["epoch"] is not None and uuid.UUID(cursor["epoch"]) != epoch:
                add(EPOCH_MISMATCH, CRITICAL, subject="cursor", reported=cursor["epoch"], expected=str(epoch),
                    detail="Enterprise money cursor belongs to a different Central epoch")  # fmt: skip
            elif cursor["last_seq"] > central_latest:
                add(CURSOR_AHEAD, CRITICAL, subject="cursor", reported=str(cursor["last_seq"]),
                    expected=f"<= {central_latest}", detail="cursor is ahead of Central's money feed")  # fmt: skip
            ls = cursor["last_success_at"]
            if ls is None:
                add(CURSOR_STALE, WARN, subject="cursor", detail="money cursor has never succeeded")
            else:
                lag = _age(datetime.fromisoformat(doc["generated_at"]), datetime.fromisoformat(ls))
                if lag > t.cursor_fail_s:
                    add(
                        CURSOR_STALE,
                        FAIL,
                        subject="cursor",
                        reported=f"{int(lag)}s",
                        expected=f"<= {t.cursor_fail_s}s",
                    )
                elif lag > t.cursor_warn_s:
                    add(
                        CURSOR_STALE,
                        WARN,
                        subject="cursor",
                        reported=f"{int(lag)}s",
                        expected=f"<= {t.cursor_warn_s}s",
                    )
            if cursor["has_error"]:
                add(
                    CURSOR_STALE,
                    FAIL,
                    subject="cursor",
                    detail="money cursor is blocked by an error (operator action)",
                )
        # -- autoriteti vs grant-et e Central
        if local and any(g.status == "active" and g.purpose == "standard" for g in my.values()):
            add(
                MODE_MISMATCH,
                WARN,
                detail="Central issued grants but Enterprise reports authority=local (grants cannot apply)",
            )
        _grants(add, my, c, doc, mode, cursor, central_latest, now, t, wallet)
        _baseline(add, my, c, doc, mode, cursor, now, t)
        for rg in doc["grants"]:
            if rg["grant_id"] not in known_ids and rg["grant_id"] not in attributed_unexpected:
                attributed_unexpected.add(rg["grant_id"])
                add(UNEXPECTED_GRANT, CRITICAL if not local else WARN, subject=rg["grant_id"], reported=rg["amount"],
                    expected="(none)", detail=f"Enterprise holds a grant Central never issued (status {rg['status']})")  # fmt: skip


def _adder(out, eid, pid, cur):
    def add(code, severity, *, subject=None, expected=None, reported=None, detail="", **extra):
        assert code in CATEGORIES, code
        out.append(
            Discrepancy(code, severity, eid, pid, cur, subject, expected, reported, detail, extra)
        )

    return add


def _lag_severity(seq, cursor, created_at, now, t: Thresholds):
    """seq i grant/reversal-it kundrejt kursorit: (kodi, ashpërsia) ose None kur kursori e ka kaluar."""
    if seq <= cursor["last_seq"]:
        return None
    return CURSOR_BEHIND, (WARN if _age(now, created_at) <= t.lag_grace_s else FAIL)


def _grants(add, my, c: _Central, doc, mode, cursor, central_latest, now, t, wallet) -> None:
    local = mode == "local"
    by_id = {g["grant_id"]: g for g in doc["grants"]}
    for gid, g in sorted(my.items()):
        ev = c.issued.get(gid)
        eg = by_id.get(gid)
        if ev is None:
            continue  # grant pa ngjarje s'ekziston në Central (invariant i M9-b)
        if eg is None:
            lag = _lag_severity(ev.seq, cursor, ev.created_at, now, t)
            if lag is not None:
                add(CURSOR_BEHIND, INFO if local else lag[1], subject=gid, expected=f"seq {ev.seq}",
                    reported=f"cursor {cursor['last_seq']}", detail="grant issued by Central, not yet consumed")  # fmt: skip
            else:
                add(MISSING_GRANT, INFO if local else FAIL, subject=gid, expected=_fmt(g.amount), reported="(absent)",
                    detail=f"cursor {cursor['last_seq']} is past grant seq {ev.seq} but the grant is not recorded")  # fmt: skip
            continue
        if Decimal(eg["amount"]) != g.amount:
            add(GRANT_AMOUNT, CRITICAL, subject=gid, expected=_fmt(g.amount), reported=eg["amount"])
        if eg["currency"] != g.currency:
            add(GRANT_CURRENCY, CRITICAL, subject=gid, expected=g.currency, reported=eg["currency"])
        if eg["product_id"] != str(g.product_id):
            add(
                GRANT_PRODUCT,
                CRITICAL,
                subject=gid,
                expected=str(g.product_id),
                reported=eg["product_id"],
            )
        if eg["purpose"] != g.purpose or eg["baseline_ref"] != g.baseline_ref:
            add(BASELINE_MISMATCH, CRITICAL, subject=gid, expected=f"{g.purpose}/{g.baseline_ref}",
                reported=f"{eg['purpose']}/{eg['baseline_ref']}", detail="grant purpose/baseline_ref differs")  # fmt: skip
        st = eg["status"]
        if g.status == "reversed":
            rv = c.reversed.get(gid)
            if eg["reversed_seq"] is None:
                lag = _lag_severity(rv.seq, cursor, rv.created_at, now, t) if rv else None
                if lag is not None:
                    add(CURSOR_BEHIND, INFO if local else lag[1], subject=gid, expected=f"reversal seq {rv.seq}",
                        reported=f"cursor {cursor['last_seq']}", detail="reversal issued by Central, not yet consumed")  # fmt: skip
                else:
                    add(MISSING_REVERSAL, INFO if local else FAIL, subject=gid, expected="reversed",
                        reported=st, detail="Central reversed this grant; Enterprise recorded no reversal")  # fmt: skip
            elif st == "reconciliation_required":
                age = _age(now, datetime.fromisoformat(eg["updated_at"]))
                add(UNRESOLVED_REVERSAL, WARN if age <= t.unresolved_reversal_fail_s else FAIL, subject=gid,
                    expected=f"debit {_fmt(g.amount)}", reported="not applied",
                    detail=eg["detail"] or "reversal could not be applied operationally",
                    reversal_amount=_fmt(g.amount), reversed_seq=eg["reversed_seq"], available=wallet["available"],
                    held=wallet["held"], age_seconds=int(age))  # fmt: skip
        else:
            if eg["reversed_seq"] is not None or st in ("reversed", "voided_before_apply"):
                add(GRANT_STATE, CRITICAL, subject=gid, expected="active", reported=st,
                    detail="Enterprise recorded a reversal Central never issued")  # fmt: skip
            elif st == "unmapped":
                add(
                    GRANT_UNMAPPED,
                    INFO if local else FAIL,
                    subject=gid,
                    reported=st,
                    detail=eg["detail"] or "",
                )
            elif st == "baseline_mismatch":
                add(
                    BASELINE_MISMATCH, CRITICAL, subject=gid, reported=st, detail=eg["detail"] or ""
                )
            elif st == "deferred_shadow" and mode == "central":
                add(GRANT_DEFERRED, WARN, subject=gid, reported=st, expected="applied",
                    detail="authority=central but the grant is still only registered")  # fmt: skip


def _baseline(add, my, c: _Central, doc, mode, cursor, now, t) -> None:
    local = mode == "local"
    base = doc["baseline"]
    by_id = {g["grant_id"]: g for g in doc["grants"]}
    boots = {gid: g for gid, g in my.items() if g.purpose == "bootstrap"}
    for gid, g in sorted(boots.items()):
        eg = by_id.get(gid)
        if eg is None:
            continue  # tashmë i raportuar si missing/behind
        if base is None:
            add(BASELINE_MISMATCH, INFO if local else CRITICAL, subject=gid, expected=g.baseline_ref, reported="(no baseline)",
                detail="Central bootstrap grant exists but Enterprise reports no active baseline")  # fmt: skip
            continue
        if base["baseline_ref"] != g.baseline_ref:
            add(BASELINE_MISMATCH, CRITICAL, subject=gid, expected=g.baseline_ref, reported=base["baseline_ref"],
                detail="bootstrap baseline_ref differs from the Enterprise baseline")  # fmt: skip
        if Decimal(base["gross_at_cutover"]) != g.amount:
            add(BASELINE_MISMATCH, CRITICAL, subject=gid, expected=_fmt(g.amount), reported=base["gross_at_cutover"],
                detail="bootstrap amount != baseline gross_at_cutover (never compared to the current balance)")  # fmt: skip
        if eg["status"] not in (
            "matched_to_existing_balance",
            "reversed",
            "reconciliation_required",
            "voided_before_apply",
        ):
            add(
                BASELINE_MISMATCH,
                CRITICAL,
                subject=gid,
                expected="matched_to_existing_balance",
                reported=eg["status"],
            )
    if (
        base is not None
        and base["status"] == "active"
        and not any(g.baseline_ref == base["baseline_ref"] for g in c.grants.values())
    ):
        add(BASELINE_PENDING, INFO if local else WARN, subject=base["baseline_ref"], reported=base["gross_at_cutover"],
            expected="Central bootstrap grant", detail="Enterprise baseline has no Central bootstrap grant yet")  # fmt: skip


def _summary(eid, pid, cur, my, c: _Central, R, doc, now) -> KeySummary:
    z = Decimal(0)
    issued = sum((g.amount for g in my.values()), z)
    reversed_ = sum((g.amount for g in my.values() if g.status == "reversed"), z)
    rec = dict.fromkeys(("all", "applied", "deferred", "reversed", "unresolved"), z)
    shadow = None
    if doc is not None:
        for g in doc["grants"]:
            amt = Decimal(g["amount"])
            rec["all"] += amt
            if g["status"] in ("applied", "matched_to_existing_balance"):
                rec["applied"] += amt
            elif g["status"] == "deferred_shadow":
                rec["deferred"] += amt
            elif g["status"] == "reversed":
                rec["reversed"] += amt
            elif g["status"] == "reconciliation_required":
                rec["unresolved"] += amt
        w = doc["wallet"]
        if doc["authority_mode"] == "shadow":
            shadow = {"current_gross": w["gross"], "current_available": w["available"],
                      "deferred_grants_total": _fmt(rec["deferred"]),
                      "projected_gross_after_cutover": _fmt(Decimal(w["gross"]) + rec["deferred"]),
                      "projected_available_after_cutover": _fmt(Decimal(w["available"]) + rec["deferred"])}  # fmt: skip
    return KeySummary(
        eid, str(pid), cur, doc["authority_mode"] if doc else None, R.report_seq if R else None,
        _age(now, R.received_at) if R else None, R.ledger_max_id if R else None, R.money_cursor_seq if R else None,
        _fmt(issued), _fmt(reversed_), _fmt(rec["all"]), _fmt(rec["applied"]), _fmt(rec["deferred"]),
        _fmt(rec["reversed"]), _fmt(rec["unresolved"]), doc["wallet"]["gross"] if doc else None,
        doc["wallet"]["available"] if doc else None, doc["wallet"]["held"] if doc else None, shadow,
    )  # fmt: skip
