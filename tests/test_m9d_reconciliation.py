# ruff: noqa: F811
"""M9-d — rakordimi Central ↔ Enterprise (vetëm-lexim): kategoritë, ashpërsia, mënyrat, baseline, grant-et, pragjet."""

import ast
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.central.models import (
    CentralUser,
    CommercialLedgerEntry,
    CreditGrant,
    MoneyEvent,
    UsageReport,
)
from apps.central.models.money import EVENT_GRANT_ISSUED, EVENT_GRANT_REVERSED
from apps.central.services import credit_accounts as accts
from apps.central.services import grants, money_feed, payments, usage_reports, users
from apps.central.services import money_reconciliation as mr
from apps.central.tools import money_reconciliation as cli
from packages.contracts.control_plane.money import usage_v1 as uv
from tests.test_central import make_db  # noqa: F401
from tests.test_central_auth import (
    PW,
    auth_secret,  # noqa: F401
)
from tests.test_m9d_central_reports import env, mk_doc  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2030, 1, 1, 12, tzinfo=UTC)
MIN = timedelta(minutes=1)
T = mr.Thresholds(report_fresh_s=600, report_stale_s=1800, lag_grace_s=900, cursor_warn_s=900,
                  cursor_fail_s=3600, unresolved_reversal_fail_s=3600, report_missing_grace_s=1800)  # fmt: skip


# --- ndërtimi i gjendjes Central + raporteve --------------------------------------------------------------------


def fm(d) -> str:
    return format(D(d).quantize(D("0.000001")), "f")


def admins(env):
    if not hasattr(env, "admin_ids"):
        with Session(env.eng, expire_on_commit=False) as s:
            a = users.create_user(s, "a1@example.com", PW, "admin").id
            b = users.create_user(s, "a2@example.com", PW, "admin").id
            s.commit()
        env.admin_ids = (a, b)
    return env.admin_ids


def new_account(env, which="e1", cur="EUR", funds="10000"):
    a, b = admins(env)
    with Session(env.eng, expire_on_commit=False) as s:
        acct = accts.create(s, env.ids[which], env.ids["sms"], cur, s.get(CentralUser, a))
        p = payments.create(s, acct.id, funds, system="system:payment_import")
        payments.approve(s, p.id, s.get(CentralUser, b))
        s.commit()
        return acct.id


def issue(env, acct, amount, *, at=NOW - 60 * MIN, **kw):
    a, _ = admins(env)
    with Session(env.eng, expire_on_commit=False) as s:
        g = grants.issue(
            s,
            acct,
            amount,
            idempotency_key=uuid.uuid4().hex,
            actor=s.get(CentralUser, a),
            now=at,
            **kw,
        )
        s.commit()
        return g.id


def reverse(env, gid, *, at=NOW - 50 * MIN):
    a, _ = admins(env)
    with Session(env.eng, expire_on_commit=False) as s:
        grants.reverse(s, gid, s.get(CentralUser, a), "customer refund", now=at)
        s.commit()


def seq_of(env, gid, kind=EVENT_GRANT_ISSUED):
    with Session(env.eng) as s:
        return s.scalar(
            select(MoneyEvent.seq).where(MoneyEvent.entity_id == gid, MoneyEvent.event_type == kind)
        )


def central_state(env):
    with Session(env.eng) as s:
        epoch, latest = money_feed.read_state(s)
        return str(epoch), latest


def grow(env, gid, status="applied", **over):
    """Rreshti i grant-it siç do ta raportonte një Enterprise e sinkronizuar me Central."""
    with Session(env.eng) as s:
        g = s.get(CreditGrant, gid)
        rev = s.scalar(
            select(MoneyEvent.seq).where(
                MoneyEvent.entity_id == gid, MoneyEvent.event_type == EVENT_GRANT_REVERSED
            )
        )
        row = {"grant_id": str(gid), "status": status, "amount": format(g.amount, "f"), "currency": g.currency,
               "product_id": str(g.product_id), "purpose": g.purpose, "baseline_ref": g.baseline_ref,
               "issued_seq": seq_of(env, gid), "reversed_seq": rev if status in ("reversed", "reconciliation_required", "voided_before_apply") else None,
               "updated_at": "2030-01-01T11:00:00.000000+00:00", "detail": None}  # fmt: skip
    row.update(over)
    return row


def report(env, rows, *, mode="central", gross=None, held="0.000000", seq=1, which="e1", cur="EUR", cursor=None,
           baseline=None, flows=None, **kw):  # fmt: skip
    """Raport KONSISTENT me rreshtat (grants_applied = Σ applied/reversed/matched; reversals = Σ reversed)."""
    applied = sum((D(r["amount"]) for r in rows if r["status"] in ("applied", "reversed")), D(0))
    revd = sum((D(r["amount"]) for r in rows if r["status"] == "reversed"), D(0))
    g = D(gross) if gross is not None else applied - revd
    epoch, latest = central_state(env)
    f = {"grants_applied": fm(applied,), "grant_reversals": fm(revd,),
         "baseline_gross": baseline["gross_at_cutover"] if baseline else "0.000000", **(flows or {})}  # fmt: skip
    if baseline:
        f["grants_applied"], f["grant_reversals"] = (
            fm(
                applied,
            ),
            fm(
                revd,
            ),
        )
        f["positive_local_credit"] = "0.000000"
    d = mk_doc(env.ids[which], env.ids["sms"], seq=seq, cur=cur, mode=mode, gross=fm(g,), held=held,
               grants=rows, baseline=baseline, flows=f, cursor_seq=latest if cursor is None else cursor,
               epoch=epoch, **kw)  # fmt: skip
    if baseline:  # gross = baseline + applied − reversed − (captured…): fixojmë captured që ekuacioni të mbyllet
        gap = uv.UsageReportV1.parse(d).conservation_gap()
        d["flows"]["captured"] = (
            fm(
                -gap,
            )
            if gap < 0
            else "0.000000"
        )
        if gap > 0:
            d["flows"]["positive_local_credit"] = fm(
                gap,
            )
    return d


def ingest(env, d, *, at=NOW - MIN):
    with Session(env.eng) as s:
        usage_reports.ingest(s, usage_reports.parse(d), now=at)
        s.commit()


def recon(env, *, now=NOW, which=None, thresholds=T):
    with Session(env.eng) as s:
        res = mr.reconcile(
            s, enterprise_id=env.ids[which] if which else None, now=now, thresholds=thresholds
        )
        s.rollback()
        return res


def codes(res, *, min_sev=mr.WARN):
    r = {mr.INFO: 0, mr.WARN: 1, mr.FAIL: 2, mr.CRITICAL: 3}
    return sorted((d.code, d.severity) for d in res.discrepancies if r[d.severity] >= r[min_sev])


def has(res, code, sev=None):
    return any(d.code == code and (sev is None or d.severity == sev) for d in res.discrepancies)


def ready(env, amounts=("50", "30"), mode="central"):
    acct = new_account(env)
    gids = [issue(env, acct, a) for a in amounts]
    rows = [grow(env, g) for g in gids]
    return acct, gids, rows


# =============================================================================================================
# PASS + totalet
# =============================================================================================================


def test_fully_matching_central_and_enterprise_pass(env):
    acct, gids, rows = ready(env)
    ingest(env, report(env, rows))
    res = recon(env)
    assert res.status == "PASS" and codes(res, min_sev=mr.INFO) == []
    (k,) = res.keys
    assert (k.central_issued_total, k.enterprise_received_total, k.applied_total, k.gross) == (
        "80.000000", "80.000000", "80.000000", "80.000000")  # fmt: skip
    assert k.authority_mode == "central" and k.report_seq == 1 and k.report_age_seconds == 60


def test_grants_are_matched_by_grant_id_never_by_amount(env):
    acct, gids, rows = ready(env, ("50", "50"))  # dy grant me të njëjtën shumë
    swapped = [
        rows[0],
        {**rows[1], "grant_id": str(uuid.uuid4())},
    ]  # grant tjetër me të njëjtën shumë
    ingest(env, report(env, swapped))
    res = recon(env)
    assert (mr.MISSING_GRANT, mr.FAIL) in codes(res) and (
        mr.UNEXPECTED_GRANT,
        mr.CRITICAL,
    ) in codes(res)


def test_decimal_exactness_one_micro_unit_difference_is_detected(env):
    acct = new_account(env)
    g = issue(env, acct, "0.000001")
    ingest(env, report(env, [grow(env, g)]))
    assert recon(env).status == "PASS"
    g2 = issue(env, acct, "10.000001")
    ingest(
        env, report(env, [grow(env, g), grow(env, g2, amount="10.000002")], seq=2, ledger_max_id=11)
    )
    assert (mr.GRANT_AMOUNT, mr.CRITICAL) in codes(recon(env))


# =============================================================================================================
# grant-et: missing / unexpected / mismatch
# =============================================================================================================


def test_missing_grant_is_warn_while_the_consumer_catches_up_then_fail(env):
    acct, gids, rows = ready(env)
    late = issue(env, acct, "5", at=NOW - 5 * MIN)  # i ri; kursori pas tij
    ingest(env, report(env, rows, cursor=seq_of(env, late) - 1))
    res = recon(env)
    assert (mr.CURSOR_BEHIND, mr.WARN) in codes(res) and not has(res, mr.MISSING_GRANT)
    assert res.status == "WARN"
    old = issue(env, acct, "5", at=NOW - 40 * MIN)  # i vjetër (> grace) dhe ende i pakonsumuar
    ingest(env, report(env, rows, seq=2, cursor=seq_of(env, old) - 1, ledger_max_id=11))
    assert (mr.MISSING_GRANT, mr.FAIL) in codes(recon(env))


def test_missing_grant_when_the_cursor_is_already_past_it_is_a_fail(env):
    acct, gids, rows = ready(env)
    ingest(env, report(env, rows[:1]))  # cursor = latest, grant i dytë mungon
    res = recon(env)
    assert (mr.MISSING_GRANT, mr.FAIL) in codes(res) and res.status == "FAIL"
    d = next(x for x in res.discrepancies if x.code == mr.MISSING_GRANT)
    assert d.subject == str(gids[1]) and d.expected == "30.000000" and d.reported == "(absent)"


def test_unexpected_grant_is_critical(env):
    acct, gids, rows = ready(env)
    ghost = {**rows[0], "grant_id": str(uuid.uuid4()), "amount": "9.000000"}
    ingest(
        env, report(env, [*rows, ghost], gross="89.000000", flows={"grants_applied": "89.000000"})
    )
    res = recon(env)
    assert (mr.UNEXPECTED_GRANT, mr.CRITICAL) in codes(res) and res.status == "CRITICAL"


@pytest.mark.parametrize("field,code,val", [("amount", mr.GRANT_AMOUNT, "51.000000"), ("currency", mr.GRANT_CURRENCY, "USD"),
                                            ("product_id", mr.GRANT_PRODUCT, str(uuid.UUID(int=77)))])  # fmt: skip
def test_amount_currency_and_product_mismatches_are_critical(env, field, code, val):
    acct, gids, rows = ready(env)
    bad = [{**rows[0], field: val}, rows[1]]
    ingest(env, report(env, bad, gross="80.000000", flows={"grants_applied": "80.000000"}))
    assert (code, mr.CRITICAL) in codes(recon(env))


def test_wrong_currency_report_against_a_eur_account_is_flagged(env):
    acct, gids, rows = ready(env)
    ingest(env, report(env, rows, cur="USD"))  # raport për (sms, USD): Central ka vetëm llogari EUR
    res = recon(env)
    assert (mr.GRANT_CURRENCY, mr.CRITICAL) in codes(res)
    assert any(k.currency == "USD" for k in res.keys)


def test_state_mismatch_when_enterprise_reverses_a_grant_central_did_not(env):
    acct, gids, rows = ready(env)
    ingest(env, report(env, [{**rows[0], "status": "reversed", "reversed_seq": 99}, rows[1]], gross="30.000000",
                       flows={"grants_applied": "80.000000", "grant_reversals": "50.000000"}))  # fmt: skip
    assert (mr.GRANT_STATE, mr.CRITICAL) in codes(recon(env))


# =============================================================================================================
# reversal
# =============================================================================================================


def test_central_reversal_not_yet_consumed_is_warn_then_missing_reversal_fail(env):
    acct, gids, rows = ready(env)
    reverse(env, gids[0], at=NOW - 5 * MIN)
    ingest(env, report(env, rows, cursor=seq_of(env, gids[0], EVENT_GRANT_REVERSED) - 1))
    assert (mr.CURSOR_BEHIND, mr.WARN) in codes(recon(env))
    ingest(
        env, report(env, rows, seq=2, ledger_max_id=11)
    )  # kursori e ka kaluar por reversal-i s'është regjistruar
    assert (mr.MISSING_REVERSAL, mr.FAIL) in codes(recon(env))


def test_applied_reversal_is_clean(env):
    acct, gids, rows = ready(env)
    reverse(env, gids[0])
    ingest(env, report(env, [grow(env, gids[0], "reversed"), rows[1]]))
    assert recon(env).status == "PASS"


def test_unresolved_reversal_is_surfaced_with_evidence_and_ages_into_fail(env):
    acct, gids, rows = ready(env)
    reverse(env, gids[0])
    stuck = grow(env, gids[0], "reconciliation_required", detail="insufficient available funds for reversal: available=1 amount=50",
                 updated_at="2030-01-01T11:50:00.000000+00:00")  # fmt: skip
    d = report(
        env,
        [stuck, rows[1]],
        gross="80.000000",
        held="29.000000",
        flows={"grants_applied": "80.000000", "grant_reversals": "0.000000"},
    )
    d["wallet"]["active_hold_total"] = "29.000000"
    ingest(env, d)
    res = recon(env)
    (u,) = [x for x in res.discrepancies if x.code == mr.UNRESOLVED_REVERSAL]
    assert u.severity == mr.WARN and u.subject == str(gids[0])
    assert u.extra["reversal_amount"] == "50.000000" and u.extra["reversed_seq"] == seq_of(
        env, gids[0], EVENT_GRANT_REVERSED
    )
    assert (
        u.extra["available"] == "51.000000"
        and u.extra["held"] == "29.000000"
        and u.extra["age_seconds"] == 600
    )
    assert "insufficient available" in u.detail
    # Central NUK e shënon si të rakorduar: pas pragut bëhet FAIL
    late = recon(
        env,
        now=NOW + timedelta(hours=2),
        thresholds=replace(T, report_fresh_s=10**6, report_stale_s=10**7),
    )
    assert (mr.UNRESOLVED_REVERSAL, mr.FAIL) in codes(late)
    assert res.keys[0].unresolved_reversal_total == "50.000000"


# =============================================================================================================
# invariantet e wallet-it
# =============================================================================================================


def test_held_not_equal_to_active_holds_is_critical_even_in_local_mode(env):
    acct, gids, rows = ready(env)
    for mode in ("central", "local"):
        d = report(
            env,
            rows,
            mode=mode,
            gross="80.000000",
            held="5.000000",
            hold_total="4.000000",
            seq={"central": 1, "local": 2}[mode],
            ledger_max_id={"central": 10, "local": 11}[mode],
        )
        d["flows"]["grants_applied"] = "80.000000"
        ingest(env, d)
        assert (mr.HOLD_TOTAL, mr.CRITICAL) in codes(recon(env))


def test_unknown_holds_are_simply_held_money_and_the_equation_stays_valid(env):
    acct, gids, rows = ready(env)
    d = report(
        env, rows, held="25.000000"
    )  # UNKNOWN (M9-a) mban hold ACTIVE: held = Σ holds aktive
    d["wallet"]["active_hold_total"], d["wallet"]["active_hold_count"] = "25.000000", 1
    ingest(env, d)
    assert recon(env).status == "PASS"


def test_negative_balance_and_formula_mismatch_are_critical(env):
    acct, gids, rows = ready(env)
    d = report(env, rows, gross="-1.000000", flows={"grants_applied": "0.000000"})
    ingest(env, d)
    res = recon(env)
    assert (mr.NEGATIVE, mr.CRITICAL) in codes(res)
    d2 = report(
        env, rows, seq=2, ledger_max_id=11, gross="79.000000"
    )  # ekuacioni s'mbyllet (80 − 1)
    ingest(env, d2)
    assert (mr.WALLET_FORMULA, mr.CRITICAL) in codes(recon(env))
    d3 = report(env, rows, seq=3, ledger_max_id=12)
    d3["integrity"]["ledger_sum_available"] = "70.000000"  # balanca e ruajtur ≠ Σ delta ledger
    ingest(env, d3)
    assert (mr.WALLET_FORMULA, mr.CRITICAL) in codes(recon(env))


def test_unexplained_positive_credit_is_critical_in_shadow_and_central_but_not_local(env):
    acct, gids, rows = ready(env)
    for seq, mode, expect in ((1, "central", True), (2, "shadow", True), (3, "local", False)):
        d = report(
            env,
            rows,
            mode=mode,
            seq=seq,
            ledger_max_id=seq * 10,
            gross="85.000000",
            flows={"positive_local_credit": "5.000000"},
        )
        ingest(env, d)
        res = recon(env)
        assert has(res, mr.UNEXPLAINED_CREDIT, mr.CRITICAL) is expect, mode


def test_orphan_grant_credit_and_unexplained_debits(env):
    acct, gids, rows = ready(env)
    d = report(env, rows)
    d["integrity"]["orphan_grant_credit"] = "3.000000"
    d["flows"]["other_debits"] = "2.000000"
    d["flows"]["positive_local_credit"] = "2.000000"
    ingest(env, d)
    res = recon(env)
    assert (mr.UNEXPLAINED_CREDIT, mr.CRITICAL) in codes(res) and (
        mr.UNEXPLAINED_DEBIT,
        mr.CRITICAL,
    ) in codes(res)
    d2 = report(
        env, rows, seq=2, ledger_max_id=11, gross="78.000000", flows={"invoice_debits": "2.000000"}
    )
    ingest(env, d2)
    assert (mr.UNEXPLAINED_DEBIT, mr.WARN) in codes(recon(env))  # faturë nga wallet nën central


# =============================================================================================================
# baseline
# =============================================================================================================


def boot(env, gross="12"):
    acct = new_account(env)
    g = issue(env, acct, gross, purpose="bootstrap", baseline_ref=REF)
    return acct, g


REF = "ab" * 32
BASE = {
    "baseline_ref": REF,
    "gross_at_cutover": "12.000000",
    "ledger_max_id": 3,
    "status": "active",
}


def test_baseline_and_bootstrap_reconcile_without_comparing_current_balance(env):
    acct, g = boot(env)
    row = grow(env, g, "matched_to_existing_balance")
    # bilanci aktual (9.5) ≠ 12: trafik normal pas cutover-it; bootstrap krahasohet VETËM me gross_at_cutover
    d = report(env, [row], mode="shadow", gross="9.500000", baseline=BASE)
    ingest(env, d)
    res = recon(env)
    assert res.status == "PASS", codes(res, min_sev=mr.INFO)


@pytest.mark.parametrize("case", ["amount", "ref", "no_baseline", "status"])
def test_baseline_mismatches_are_critical(env, case):
    acct, g = boot(env)
    row = grow(env, g, "matched_to_existing_balance")
    base = dict(BASE)
    if case == "amount":
        base["gross_at_cutover"] = "13.000000"
    elif case == "ref":
        base["baseline_ref"] = "cd" * 32
    elif case == "status":
        row = {**row, "status": "baseline_mismatch", "detail": "amount_mismatch"}
    d = report(
        env,
        [row],
        mode="shadow",
        gross="12.000000",
        baseline=None if case == "no_baseline" else base,
    )
    ingest(env, d)
    assert (mr.BASELINE_MISMATCH, mr.CRITICAL) in codes(recon(env))


def test_enterprise_baseline_without_a_central_bootstrap_is_pending_not_failed(env):
    new_account(env)
    ingest(env, report(env, [], mode="shadow", gross="12.000000", baseline=BASE))
    res = recon(env)
    assert (mr.BASELINE_PENDING, mr.WARN) in codes(res) and res.status == "WARN"


# =============================================================================================================
# freskia, kursori, modalitetet
# =============================================================================================================


def test_stale_report_thresholds_info_warn_fail(env):
    acct, gids, rows = ready(env)
    ingest(env, report(env, rows), at=NOW - 5 * MIN)
    assert not has(recon(env), mr.STALE_REPORT)  # 5 min ≤ 10
    assert (mr.STALE_REPORT, mr.WARN) in codes(recon(env, now=NOW + 10 * MIN))  # 15 min
    assert (mr.STALE_REPORT, mr.FAIL) in codes(recon(env, now=NOW + 40 * MIN))  # 45 min > 30
    custom = replace(T, report_fresh_s=100000, report_stale_s=200000)
    assert not has(
        recon(env, now=NOW + 40 * MIN, thresholds=custom), mr.STALE_REPORT
    )  # konfigurueshme


def test_report_missing_for_an_account_with_grants(env):
    new_account(env)
    acct = new_account(env, which="e2")
    issue(env, acct, "5", at=NOW - 5 * MIN)
    assert (mr.REPORT_MISSING, mr.WARN) in codes(recon(env))
    assert (mr.REPORT_MISSING, mr.FAIL) in codes(recon(env, now=NOW + 2 * 3600 * MIN // 60))


def test_money_cursor_stale_epoch_mismatch_ahead_and_error(env):
    acct, gids, rows = ready(env)
    ingest(
        env, report(env, rows, last_success="2030-01-01T11:40:00.000000+00:00")
    )  # 20 min para raportit
    assert (mr.CURSOR_STALE, mr.WARN) in codes(recon(env))
    ingest(
        env,
        report(env, rows, seq=2, ledger_max_id=11, last_success="2030-01-01T10:00:00.000000+00:00"),
    )
    assert (mr.CURSOR_STALE, mr.FAIL) in codes(recon(env))
    d = report(env, rows, seq=3, ledger_max_id=12)
    d["cursor"]["epoch"] = str(uuid.uuid4())
    ingest(env, d)
    assert (mr.EPOCH_MISMATCH, mr.CRITICAL) in codes(recon(env))
    d = report(env, rows, seq=4, ledger_max_id=13, cursor=10**6)
    ingest(env, d)
    assert (mr.CURSOR_AHEAD, mr.CRITICAL) in codes(recon(env))
    d = report(env, rows, seq=5, ledger_max_id=14)
    d["cursor"]["has_error"] = True
    ingest(env, d)
    assert (mr.CURSOR_STALE, mr.FAIL) in codes(recon(env))


def test_shadow_mode_shows_what_central_authorizes_without_failing_deferred_grants(env):
    acct, gids, rows = ready(env)
    deferred = [{**r, "status": "deferred_shadow"} for r in rows]
    d = report(
        env,
        deferred,
        mode="shadow",
        gross="12.000000",
        flows={
            "grants_applied": "0.000000",
            "positive_local_credit": "0.000000",
            "baseline_gross": "12.000000",
        },
    )
    d["baseline"] = BASE
    ingest(env, d)
    res = recon(env)
    (k,) = res.keys
    assert k.shadow_projection == {
        "current_gross": "12.000000", "current_available": "12.000000", "deferred_grants_total": "80.000000",
        "projected_gross_after_cutover": "92.000000", "projected_available_after_cutover": "92.000000"}  # fmt: skip
    assert not {
        c
        for c, s in codes(res)
        if c in (mr.MISSING_GRANT, mr.GRANT_DEFERRED, mr.UNEXPLAINED_CREDIT)
    }
    # i njëjti në central = grant-e ende të paaplikuara
    d2 = {
        **d,
        "report_id": str(uuid.uuid4()),
        "report_seq": 2,
        "authority_mode": "central",
        "ledger_max_id": 11,
    }
    ingest(env, d2)
    assert (mr.GRANT_DEFERRED, mr.WARN) in codes(recon(env))


def test_local_mode_is_informational_for_authority_findings(env):
    acct, gids, rows = ready(env)
    ingest(
        env,
        report(
            env,
            [],
            mode="local",
            gross="3.000000",
            flows={"grants_applied": "0.000000", "positive_local_credit": "3.000000"},
        ),
    )
    res = recon(env, now=NOW + 40 * MIN)
    sev = {(d.code, d.severity) for d in res.discrepancies}
    assert (mr.MISSING_GRANT, mr.INFO) in sev and (mr.STALE_REPORT, mr.INFO) in sev
    assert (mr.MODE_MISMATCH, mr.WARN) in sev
    assert not [d for d in res.discrepancies if d.severity in (mr.FAIL, mr.CRITICAL)]
    assert res.status == "WARN"
    assert not has(res, mr.UNEXPLAINED_CREDIT)  # kredia lokale pritet në local


def test_central_mode_is_strict_and_proves_the_authority_properties(env):
    acct, gids, rows = ready(env)
    ingest(env, report(env, rows))
    res = recon(env)
    assert res.status == "PASS" and mr.exit_code(res, strict=True) == 0
    d = report(
        env,
        rows,
        seq=2,
        ledger_max_id=11,
        gross="82.000000",
        flows={"positive_local_credit": "2.000000"},
    )
    ingest(env, d)
    res = recon(env)
    assert res.status == "CRITICAL" and mr.exit_code(res) == 1


def test_old_report_does_not_replace_the_latest_view(env):
    acct, gids, rows = ready(env)
    ingest(env, report(env, rows, seq=5, ledger_max_id=50))
    bad_old = report(env, rows[:1], seq=2, ledger_max_id=20)  # raport i vjetër i keq arrin vonë
    ingest(env, bad_old)
    res = recon(env)
    assert res.status == "PASS" and res.keys[0].report_seq == 5


# =============================================================================================================
# CLI, vetëm-lexim, kategoritë
# =============================================================================================================


def test_cli_exit_codes_json_strict_and_enterprise_filter(env, capsys):
    acct, gids, rows = ready(env)
    ingest(env, report(env, rows[:1]))  # missing grant ⇒ FAIL
    assert cli.main(["--json"], engine=env.eng) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] in ("FAIL", "CRITICAL") and out["counts"]["FAIL"] >= 1
    assert cli.main([], engine=env.eng) == 1 and "missing_grant" in capsys.readouterr().out
    assert (
        cli.main(["--enterprise-id", str(env.ids["e2"])], engine=env.eng) == 0
    )  # enterprise tjetër: asgjë për të rakorduar
    assert cli.main(["--enterprise-id", "nope"], engine=env.eng) == 2
    capsys.readouterr()
    ingest(env, report(env, rows, seq=2, ledger_max_id=11))
    assert cli.main(["--strict"], engine=env.eng) == 0
    capsys.readouterr()


def test_reconciliation_never_mutates_anything(env):
    acct, gids, rows = ready(env)
    ingest(env, report(env, rows[:1]))

    def snap():
        with Session(env.eng) as s:
            return [
                s.scalar(select(func.count()).select_from(m))
                for m in (CreditGrant, MoneyEvent, CommercialLedgerEntry, UsageReport)
            ]

    before = snap()
    recon(env)
    recon(env, which="e1")
    cli.main([], engine=env.eng)
    assert snap() == before
    src = (ROOT / "apps/central/services/money_reconciliation.py").read_text()
    tree = ast.parse(src)
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "db"}  # fmt: skip
    assert not calls & {
        "add",
        "commit",
        "flush",
        "delete",
        "merge",
        "execute",
        "begin_nested",
        "rollback",
    }, calls
    assert "unresolved_reversal_total" in src


def test_every_category_used_is_declared_and_severity_ordering_is_total(env):
    assert mr.CATEGORIES >= {"missing_grant", "unexpected_grant", "grant_amount_mismatch", "grant_currency_mismatch",
                             "grant_product_mismatch", "missing_reversal", "unresolved_reversal", "cursor_behind",
                             "cursor_ahead", "stale_report", "wallet_formula_mismatch", "hold_total_mismatch",
                             "negative_invariant", "unexplained_positive_credit", "unexplained_debit",
                             "baseline_mismatch", "authority_mode_mismatch"}  # fmt: skip
    assert mr.overall([]) == "PASS"
    mk = lambda s: mr.Discrepancy("x", s, "e", None, None)  # noqa: E731
    assert [mr.overall([mk(s)]) for s in ("INFO", "WARN", "FAIL", "CRITICAL")] == [
        "PASS",
        "WARN",
        "FAIL",
        "CRITICAL",
    ]


def test_enterprise_filter_and_multiple_enterprises(env):
    a1, _, rows1 = ready(env)
    a2 = new_account(env, which="e2")
    g2 = issue(env, a2, "7")
    ingest(env, report(env, rows1))
    ingest(env, report(env, [grow(env, g2)], which="e2"))
    res = recon(env)
    assert res.status == "PASS" and len(res.keys) == 2
    assert [k.enterprise_id for k in recon(env, which="e2").keys] == [str(env.ids["e2"])]
