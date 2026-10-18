# ruff: noqa: F811
"""M9-g5 — mbyllja e faturimit: readiness final, invariante, alarme, observability, workflow i çështjeve manuale, politika e shadow, worker, siguri."""

import json
import threading
import uuid
from datetime import timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from apps.central.core import errors
from apps.central.core.config import settings
from apps.central.main import create_app
from apps.central.models import AuditLog
from apps.central.models.billing import BillingSubscription, Invoice
from apps.central.models.billing_import import (
    BillingImportBatch,
    BillingImportIssue,
    BillingImportItem,
    BillingShadowComparison,
    BillingUsageBaseline,
)
from apps.central.models.money import Payment
from apps.central.models.settlement import InvoicePaymentAllocation
from apps.central.services import (
    billing,
    billing_authority,
    billing_closure,
    billing_import,
    retention,
)
from apps.central.tools import billing_final_readiness as cli_final
from apps.central.tools import billing_import as cli_import
from apps.central.tools import billing_run as cli_run
from apps.central.tools import billing_shadow as cli_shadow
from tests.test_central import IS_PG, central_alembic, make_db  # noqa: F401
from tests.test_central_auth import PW, auth_secret, bearer, mk, token_for  # noqa: F401
from tests.test_central_products import cdb  # noqa: F401
from tests.test_m9g1_billing import U, b  # noqa: F401
from tests.test_m9g4_cutover import (  # noqa: F401
    BOUNDARY,
    NOW,
    apply,
    base_doc,
    dec,
    edit,
    levels,
    n,
    plan_of,
    prep_shadow,
    ready_world,
    run_shadow,
    w,
)


def final(w, **kw):
    with w.F() as s:
        doc = billing_closure.final_readiness(s, NOW, **kw)
        doc["alerts"] = billing_closure.alerts(doc["checks"], doc["mode"])
        s.rollback()
    doc["by"] = {c["name"]: c for c in doc["checks"]}
    return doc


def tamper(w, sql, **params):
    if w.eng.dialect.name != "sqlite":
        pytest.skip("tampering needs a trigger-free database (PG triggers forbid it by design)")
    with w.eng.begin() as c:
        c.execute(text(sql), params)


def central_world(w, monkeypatch):
    monkeypatch.setattr(settings, "billing_worker_configured", True)
    ready_world(w)
    monkeypatch.setattr(settings, "billing_worker_configured", True)
    with w.F() as s:
        billing_authority.set_mode(s, U(s, w.a1), "central", ack=True, reason="cutover", now=NOW)
        s.commit()


# =============================================================================================================
# readiness final
# =============================================================================================================

REQUIRED = {
    "authority_mode", "production_ack", "legacy_import_complete", "import_conflicts_resolved", "active_subscriptions_mapped",
    "sequence_seeds_safe", "usage_opening_baseline", "latest_usage_report_healthy", "currency_pricing_mapping_valid",
    "legacy_overage_has_central_price", "shadow_comparison_acceptable", "no_dual_issuer", "enterprise_billing_frozen",
    "central_billing_worker_configured", "billing_worker_heartbeat", "billing_run_not_stalled", "billing_invoice_payments_not_stale",
    "billing_settlement_integrity", "inv_credit_notes_cumulative_within_paid_total", "inv_invoice_sequence_not_behind",
    "inv_credit_note_sequence_not_behind", "import_waivers_documented", "billing_invoice_arithmetic",
}  # fmt: skip


def test_final_readiness_aggregates_every_required_signal_and_is_read_only(w, monkeypatch):
    ready_world(w)
    tables = (AuditLog, Invoice, BillingImportItem, BillingShadowComparison, BillingUsageBaseline)
    before = [n(w, t) for t in tables]
    doc = final(w, prod_ack=True)
    with w.F() as s:
        billing_closure.observability(s, NOW)
        s.rollback()
    assert REQUIRED <= set(doc["by"]), REQUIRED - set(doc["by"])
    assert [n(w, t) for t in tables] == before
    assert doc["status"] in ("PASS", "WARN", "FAIL") and doc["mode"] == "shadow"
    assert doc["by"]["authority_mode"]["level"] == "WARN"  # cutover ende s'ka ndodhur
    assert not [c for c in doc["checks"] if c["level"] == "FAIL"], {c["name"]: c["reason"] for c in doc["checks"] if c["level"] == "FAIL"}  # fmt: skip


def test_local_mode_fails_and_central_mode_with_heartbeat_passes(w, monkeypatch):
    ready_world(w)
    with w.F() as s:
        st = billing_authority.get_state(s)
        st.mode = "local"
        s.commit()
    assert final(w, prod_ack=True)["by"]["authority_mode"]["level"] == "FAIL"
    with w.F() as s:
        st = billing_authority.get_state(s)
        st.mode = "shadow"
        s.commit()
        billing_authority.set_mode(s, U(s, w.a1), "central", ack=True, reason="cutover", now=NOW)
        s.commit()
    d = final(w)
    assert (
        d["by"]["authority_mode"]["level"] == "PASS"
        and d["by"]["production_ack"]["level"] == "PASS"
    )
    assert d["by"]["billing_worker_heartbeat"]["level"] == "WARN"  # s'ka run ende
    billing.run_exclusive(w.eng, NOW, record=True)
    d = final(w)
    assert d["by"]["billing_worker_heartbeat"]["level"] == "PASS"
    later = NOW + timedelta(seconds=settings.billing_run_stale_seconds + 60)
    with w.F() as s:
        c = {x.name: x for x in billing_closure.run_checks(s, later)}
        assert (
            c["billing_worker_heartbeat"].level == "WARN"
            and "stale" in c["billing_worker_heartbeat"].reason
        )


def test_final_readiness_json_cli_exit_codes_and_no_pii(w, monkeypatch, capsys):
    ready_world(w)
    code = cli_final.main(["--json", "--observability"], engine=w.eng)
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert code in (0, 1) and {"status", "mode", "checks", "alerts", "observability"} <= set(doc)
    assert "@" not in out and "Acme" not in out and "Rr. 1" not in out
    assert cli_final.main(["--strict"], engine=w.eng) in (0, 1)
    assert cli_final.main([], engine=object()) == 2


# =============================================================================================================
# invariante
# =============================================================================================================


def test_invariants_pass_on_a_consistent_imported_and_central_issued_ledger(w, monkeypatch):
    central_world(w, monkeypatch)
    with w.F() as s:
        sid = s.scalar(select(BillingSubscription.id))
        assert billing.process_period(s, sid, NOW).kind == "invoiced"
        s.commit()
    d = final(w)
    bad = {
        k: c["reason"] for k, c in d["by"].items() if k.startswith("inv_") and c["level"] != "PASS"
    }
    assert bad == {}


def test_invoice_sequence_behind_issued_numbers_is_critical(w, monkeypatch):
    central_world(w, monkeypatch)
    apply(w)  # no-op idempotent
    tamper(w, "update invoice_number_sequence set last_number = 0")
    d = final(w)
    assert d["by"]["inv_invoice_sequence_not_behind"]["level"] == "FAIL"
    assert "sequence_collision_risk" in {
        a["code"] for a in d["alerts"] if a["severity"] == "CRITICAL"
    }


def test_imported_invoice_without_import_evidence_fails_provenance_invariant(w):
    apply(w)
    tamper(w, "delete from billing_import_items where target_type = 'invoice'")
    assert final(w)["by"]["inv_imported_rows_have_provenance_evidence"]["level"] == "FAIL"


def test_paid_invoice_without_allocation_is_a_settlement_invariant_failure(w, monkeypatch):
    central_world(w, monkeypatch)
    with w.F() as s:
        open_id = s.scalar(select(Invoice.id).where(Invoice.status == "open"))
    tamper(
        w,
        "update invoices set status='paid', paid_at=:t where id=:i",
        t=NOW.isoformat(),
        i=open_id.hex,
    )
    d = final(w)
    assert d["by"]["inv_settlement_one_allocation_exact_total"]["level"] == "FAIL"
    assert "settlement_invariant_failure" in {a["code"] for a in d["alerts"]}


def test_central_invoice_outside_central_mode_is_dual_issuer_critical(w, monkeypatch):
    central_world(w, monkeypatch)
    with w.F() as s:
        billing.process_period(s, s.scalar(select(BillingSubscription.id)), NOW)
        s.commit()
    tamper(w, "update billing_authority_state set mode='shadow'")
    d = final(w)
    assert d["by"]["inv_no_central_invoice_outside_central_mode"]["level"] == "FAIL"
    assert "dual_issuer_possible" in {a["code"] for a in d["alerts"] if a["severity"] == "CRITICAL"}


# =============================================================================================================
# alarme
# =============================================================================================================


def codes(d, sev):
    return {a["code"] for a in d["alerts"] if a["severity"] == sev}


def test_critical_alerts_in_central_mode_for_unfrozen_enterprise_missing_baseline_and_pricing(
    w, monkeypatch
):
    monkeypatch.setattr(settings, "billing_worker_configured", True)
    apply(w, edit(w.doc, lambda x: (x["authority"].update(mode="shadow"), x["usage"].clear())))
    with w.F() as s:
        st = billing_authority.get_state(s)
        if st is None:
            from apps.central.models.billing_import import BillingAuthorityState

            s.add(BillingAuthorityState(id=1, mode="central", ack=True))
        else:
            st.mode = "central"
        s.commit()
    d = final(w, prod_ack=True)
    crit = codes(d, "CRITICAL")
    assert {
        "central_authority_with_enterprise_not_frozen",
        "missing_usage_baseline_in_central",
    } <= crit


def test_unresolved_import_issue_is_critical_at_central_and_warn_before(w, monkeypatch):
    central_world(w, monkeypatch)
    with w.F() as s:
        s.add(BillingImportIssue(batch_id=s.scalar(select(BillingImportBatch.id)), source_table="invoices", source_id="9",
                                 classification="requires_manual_review", reason="paid invoice without paid_at", created_at=NOW))  # fmt: skip
        s.commit()
    d = final(w, prod_ack=True)
    assert "import_conflict_unresolved_at_cutover" in codes(d, "CRITICAL")
    assert "unresolved_manual_review_item" not in codes(d, "WARN")  # no dyfishim: CRITICAL e mbulon


def test_warn_alerts_for_stale_usage_shadow_mismatch_and_pending_payments(w, monkeypatch):
    ready_world(w)
    with w.F() as s:
        cmp = s.scalar(select(BillingShadowComparison).limit(1))
        s.add(BillingShadowComparison(subscription_id=cmp.subscription_id, enterprise_id=cmp.enterprise_id, period_index=cmp.period_index,
                                      legacy_invoice_id=cmp.legacy_invoice_id, category="usage_mismatch", categories=["usage_mismatch"],
                                      central=cmp.central, legacy=cmp.legacy, comparison_hash="a" * 64, computed_at=NOW + timedelta(seconds=1)))  # fmt: skip
        s.commit()
    d = final(w, prod_ack=True)
    assert "shadow_mismatch" in codes(d, "WARN")
    assert d["by"]["shadow_comparison_acceptable"]["level"] == "WARN"


# =============================================================================================================
# politika e shadow
# =============================================================================================================


def put_cmp(w, category, legacy_total, central_total, secs=5):
    with w.F() as s:
        cmp = s.scalar(
            select(BillingShadowComparison)
            .order_by(BillingShadowComparison.period_index.desc())
            .limit(1)
        )
        s.add(BillingShadowComparison(subscription_id=cmp.subscription_id, enterprise_id=cmp.enterprise_id, period_index=cmp.period_index,
                                      legacy_invoice_id=cmp.legacy_invoice_id, category=category, categories=[category],
                                      central={**cmp.central, "total": central_total}, legacy={**cmp.legacy, "total": legacy_total},
                                      comparison_hash=uuid.uuid4().hex * 2, computed_at=NOW + timedelta(seconds=secs)))  # fmt: skip
        s.commit()


@pytest.mark.parametrize(
    "category",
    [
        "period_mismatch",
        "currency_mismatch",
        "plan_mismatch",
        "tax_mismatch",
        "central_only",
        "legacy_only",
    ],
)
def test_hard_shadow_categories_fail_cutover_readiness(w, category):
    ready_world(w)
    put_cmp(w, category, "10.00", "10.00")
    assert final(w, prod_ack=True)["by"]["shadow_comparison_acceptable"]["level"] == "FAIL"


def test_amount_mismatch_is_fail_without_tolerance_and_warn_only_inside_it(w, monkeypatch):
    ready_world(w)
    put_cmp(w, "amount_mismatch", "24.00", "24.01")
    assert (
        final(w, prod_ack=True)["by"]["shadow_comparison_acceptable"]["level"] == "FAIL"
    )  # tolerancë 0 (parazgjedhje)
    monkeypatch.setattr(settings, "billing_shadow_amount_tolerance", D("0.01"))
    assert final(w, prod_ack=True)["by"]["shadow_comparison_acceptable"]["level"] == "WARN"
    put_cmp(
        w, "amount_mismatch", "24.00", "24.05", secs=9
    )  # mbi tolerancë ⇒ prapë FAIL; asgjë s'normalizohet
    assert final(w, prod_ack=True)["by"]["shadow_comparison_acceptable"]["level"] == "FAIL"


def test_missing_baseline_remains_a_hard_fail(w, monkeypatch):
    monkeypatch.setattr(settings, "billing_worker_configured", True)
    apply(w, edit(w.doc, lambda x: x["usage"].clear()))
    assert levels(w, prod_ack=True)["usage_opening_baseline"].level == "FAIL"


def test_missing_central_price_remains_a_hard_fail(w, monkeypatch):
    monkeypatch.setattr(settings, "billing_worker_configured", True)
    apply(w, base_doc(w.e2, w.email))
    assert levels(w, prod_ack=True)["legacy_overage_has_central_price"].level == "FAIL"


# =============================================================================================================
# workflow i çështjeve manuale
# =============================================================================================================


def blocked(w, mutate):
    d = edit(w.doc, mutate)
    apply(w, d)
    with w.F() as s:
        return d, [billing_import.issue_view(i) for i in billing_import.unresolved_issues(s)]


def test_categories_distinguish_partial_over_currency_and_missing_reference(w):
    _, v = blocked(w, lambda d: d["payments"][0].update(amount=dec("10")))
    assert [x["category"] for x in v] == ["partial_payment"]
    assert v[0]["allowed"] and v[0]["forbidden"] and v[0]["waivable"] is False
    for mut, cat in (
        (lambda d: d["payments"][0].update(amount=dec("99")), "overpayment"),
        (lambda d: d["payments"][0].update(currency="USD"), "currency_mismatch"),
        (lambda d: d["invoices"][0].update(paid_via="odd"), "missing_external_reference"),
    ):
        plan = plan_of(w, edit(w.doc, mut))
        assert {billing_import.categorize(d.reason) for d in plan.blocking()} == {cat}
        assert plan.report()["blocking_by_category"] == {cat: 1}


def test_issue_cannot_be_closed_with_words_only_nor_waived_when_unsupported(w):
    blocked(w, lambda d: d["payments"][0].update(amount=dec("10")))
    with w.F() as s:
        i = billing_import.unresolved_issues(s)[0]
        a = U(s, w.a1)
        with pytest.raises(errors.Conflict):
            billing_import.resolve_issue(s, a, i.id, "ok, it is fine", now=NOW)
        with pytest.raises(errors.Conflict):  # unsupported ⇒ asnjë waiver
            billing_import.resolve_issue(s, a, i.id, "ok", evidence_ref="TICKET-1234", now=NOW)
        s.rollback()
        assert len(billing_import.unresolved_issues(s)) == 1


def test_waiver_needs_evidence_ref_is_audited_and_surfaces_as_warn(w):
    blocked(w, lambda d: d["invoices"][0].update(paid_via="odd"))
    with w.F() as s:
        i = billing_import.unresolved_issues(s)[0]
        a = U(s, w.a1)
        for bad in ("short", "x;y;zzzzzzzz", "a" * 201, 5):
            with pytest.raises((errors.Invalid, AttributeError)):
                billing_import.resolve_issue(s, a, i.id, "documented", evidence_ref=bad, now=NOW)
            s.rollback()
        billing_import.resolve_issue(
            s, a, i.id, "settled off-system", evidence_ref="TICKET-4711", now=NOW
        )
        s.commit()
        row = s.get(BillingImportIssue, i.id)
        assert row.resolution.startswith("kind=operator_waiver; ref=TICKET-4711; ")
        assert [
            r.action
            for r in s.scalars(
                select(AuditLog).where(AuditLog.action == "billing.import_issue_resolve")
            )
        ] == ["billing.import_issue_resolve"]
        assert len(billing_import.waivers(s)) == 1
    assert final(w, prod_ack=True)["by"]["import_waivers_documented"]["level"] == "WARN"


def test_baseline_issue_is_never_waivable(w):
    _, v = blocked(
        w, lambda d: d["usage"][0].update(capture_active_since="2030-02-01T00:00:00.000000+00:00")
    )
    assert v[0]["category"] == "usage_baseline_insufficient" and v[0]["waivable"] is False
    with w.F() as s:
        with pytest.raises(errors.Conflict):
            billing_import.resolve_issue(
                s, U(s, w.a1), v[0]["id"], "x", evidence_ref="TICKET-1234", now=NOW
            )


def test_issue_is_superseded_only_after_a_later_batch_imports_the_object(w):
    d, v = blocked(w, lambda x: x["payments"][0].update(amount=dec("10")))
    iid = v[0]["id"]
    fixed = edit(
        w.doc, lambda x: x.update(export_id=str(uuid.uuid4()))
    )  # burimi u korrigjua ⇒ ri-eksport
    with w.F() as s:  # ende i bllokuar para batch-it të ri
        with pytest.raises(errors.Conflict):
            billing_import.resolve_issue(s, U(s, w.a1), iid, "fixed at source", now=NOW)
    apply(w, fixed, now=NOW + timedelta(hours=1))
    with w.F() as s:
        row = billing_import.resolve_issue(
            s, U(s, w.a1), iid, "fixed at source and re-exported", now=NOW + timedelta(hours=2)
        )
        s.commit()
        assert row.resolution.startswith("kind=superseded; ")
        assert billing_import.unresolved_issues(s) == []


def test_cli_lists_issues_with_policy_and_requires_evidence_flag(w, capsys):
    blocked(w, lambda d: d["invoices"][0].update(paid_via="odd"))
    assert cli_import.main(["--issues"], engine=w.eng) == 0
    out = json.loads(capsys.readouterr().out)
    assert (
        out["unresolved"][0]["category"] == "missing_external_reference"
        and out["unresolved"][0]["waivable"] is True
    )
    iid = out["unresolved"][0]["id"]
    assert (
        cli_import.main(
            ["--resolve", iid, "--reason", "fine", "--actor-email", "a1@example.com"], engine=w.eng
        )
        == 1
    )  # pa evidencë
    assert (
        cli_import.main(
            [
                "--resolve",
                iid,
                "--reason",
                "fine",
                "--evidence-ref",
                "TICKET-99999",
                "--actor-email",
                "a1@example.com",
            ],
            engine=w.eng,
        )
        == 0
    )


# =============================================================================================================
# dry-run i prodhimit (JSON) — pa shkrim
# =============================================================================================================


def test_dry_run_json_exposes_counts_conflicts_seeds_and_proposed_baselines(w, tmp_path, capsys):
    f = tmp_path / "a.json"
    f.write_text(json.dumps(edit(w.doc, lambda d: d["payments"][0].update(amount=dec("10")))))
    before = [
        n(w, t) for t in (BillingImportBatch, BillingImportItem, BillingUsageBaseline, Invoice)
    ]
    assert cli_import.main(["--artifact", str(f), "--json"], engine=w.eng) == 1
    rep = json.loads(capsys.readouterr().out)
    assert rep["sequence_seeds"] == {"2029": 7, "2030": 3} and rep["blocking_by_category"] == {
        "partial_payment": 1
    }
    assert rep["proposed_baselines"] == [
        {
            "enterprise_id": str(w.e1),
            "boundary": BOUNDARY,
            "cumulative": 10,
            "watermark": 14,
            "classification": "importable",
        }
    ]
    assert [
        n(w, t) for t in (BillingImportBatch, BillingImportItem, BillingUsageBaseline, Invoice)
    ] == before


def test_shadow_summary_flag_is_read_only(w, capsys):
    ready_world(w)
    before = n(w, BillingShadowComparison)
    assert cli_shadow.main(["--summary", "--json"], engine=w.eng) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["compared"] >= 1 and n(w, BillingShadowComparison) == before


# =============================================================================================================
# worker
# =============================================================================================================


def test_run_exclusive_records_a_pii_free_heartbeat_and_is_idempotent(w, monkeypatch):
    central_world(w, monkeypatch)
    out = billing.run_exclusive(w.eng, NOW)
    again = billing.run_exclusive(w.eng, NOW + timedelta(minutes=1))
    assert (
        out.invoiced == 1
        and again.invoiced == 0
        and n(w, Invoice, Invoice.provenance == "central") == 1
    )
    with w.F() as s:
        rows = list(
            s.scalars(
                select(AuditLog)
                .where(AuditLog.action == "billing.run")
                .order_by(AuditLog.created_at)
            )
        )
        assert (
            len(rows) == 2
            and rows[0].actor_kind == "system"
            and rows[0].actor_label == "system:billing_run"
        )
        assert set(rows[0].detail) >= {"due", "invoiced", "failed"} and "@" not in json.dumps(
            rows[0].detail
        )
        obs = billing_closure.observability(s, NOW)
    assert (
        obs["worker"]["invoices_issued_last_run"] == 0
        and obs["authority"]["mode"] == "central"
        and obs["authority"]["central_invoices"] == 1
    )


def test_crash_before_commit_leaves_no_invoice_and_no_number_gap(w, monkeypatch):
    central_world(w, monkeypatch)
    with w.F() as s:
        sid = s.scalar(select(BillingSubscription.id))
        assert billing.process_period(s, sid, NOW).kind == "invoiced"
        s.rollback()  # «crash» para commit-it
    assert n(w, Invoice, Invoice.provenance == "central") == 0
    billing.run_exclusive(w.eng, NOW)
    with w.F() as s:
        nums = list(s.scalars(select(Invoice.number).where(Invoice.provenance == "central")))
    assert nums == ["INV-2030-000004"]  # vazhdon pas seed-it 3, pa boshllëk


def test_crash_after_invoice_commit_is_safe_to_resume(w, monkeypatch):
    central_world(w, monkeypatch)

    real = billing.audit.record_system

    def selective(db, **k):
        if k.get("action") == billing.RUN_AUDIT_ACTION:
            raise RuntimeError("heartbeat store down")
        return real(db, **k)

    monkeypatch.setattr(billing.audit, "record_system", selective)
    out = billing.run_exclusive(
        w.eng, NOW
    )  # faturat janë commit-uar; humbja e heartbeat-it nuk e prish rezultatin
    assert out.invoiced == 1
    monkeypatch.setattr(billing.audit, "record_system", real)
    assert billing.run_exclusive(w.eng, NOW).invoiced == 0
    assert n(w, Invoice, Invoice.provenance == "central") == 1


def test_run_is_bounded_by_limit_and_cli_refuses_outside_central(w, monkeypatch, capsys):
    ready_world(w)
    assert cli_run.main(["--limit", "1"], engine=w.eng) == 3  # shadow ⇒ refuzim
    with w.F() as s:
        billing_authority.set_mode(s, U(s, w.a1), "central", ack=True, reason="go", now=NOW)
        s.commit()
    assert cli_run.main(["--limit", "1", "--json"], engine=w.eng) in (0, 1)
    assert cli_run.main(["--limit", "0"], engine=w.eng) == 2


@pytest.mark.skipif(not IS_PG, reason="advisory lock is PostgreSQL-only")
def test_second_concurrent_run_is_refused_by_the_advisory_lock(w, monkeypatch):
    if w.eng.dialect.name != "postgresql":
        pytest.skip("postgres parametrization only")
    central_world(w, monkeypatch)
    holder = w.eng.connect()
    holder.execute(text("select pg_advisory_lock(:k)"), {"k": billing.RUN_LOCK_KEY})
    holder.commit()
    try:
        with pytest.raises(billing.RunInProgress):
            billing.run_exclusive(w.eng, NOW)
        assert cli_run.main([], engine=w.eng) == 4
    finally:
        holder.execute(text("select pg_advisory_unlock(:k)"), {"k": billing.RUN_LOCK_KEY})
        holder.commit()
        holder.close()
    assert billing.run_exclusive(w.eng, NOW).failed == 0


@pytest.mark.skipif(not IS_PG, reason="postgres concurrency")
def test_two_parallel_runs_never_duplicate_an_invoice(w, monkeypatch):
    if w.eng.dialect.name != "postgresql":
        pytest.skip("postgres parametrization only")
    central_world(w, monkeypatch)
    res, errs = [], []

    def go():
        try:
            res.append(billing.run_due(w.eng, NOW))
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and sum(r.invoiced for r in res) == 1
    assert n(w, Invoice, Invoice.provenance == "central") == 1


# =============================================================================================================
# retention, siguri, API
# =============================================================================================================


def test_retention_never_touches_financial_or_provenance_evidence(w, monkeypatch):
    central_world(w, monkeypatch)
    with w.F() as s:
        billing.process_period(s, s.scalar(select(BillingSubscription.id)), NOW)
        s.commit()
    protected = (Invoice, Payment, InvoicePaymentAllocation, BillingImportBatch, BillingImportItem, BillingImportIssue, BillingUsageBaseline,
                 BillingShadowComparison, AuditLog)  # fmt: skip
    before = [n(w, t) for t in protected]
    monkeypatch.setattr(settings, "usage_report_retention_days", 1)
    monkeypatch.setattr(settings, "usage_report_full_days", 1)
    with w.F() as s:
        retention.apply(s, retention.plan(s, now=NOW + timedelta(days=4000)))
        s.commit()
    assert [n(w, t) for t in protected] == before


def test_admin_billing_surface_has_no_delete_put_patch_and_strict_bodies():
    from apps.central.api import admin_billing, admin_settlement

    checked = 0
    for router in (admin_billing.router, admin_settlement.router):
        for r in router.routes:
            assert r.methods <= {"GET", "POST"}, (r.path, r.methods)
            for p in r.dependant.body_params:
                model = p.field_info.annotation
                assert model.model_config.get("extra") == "forbid", (r.path, model)
                checked += 1
    assert checked >= 10  # kundër kalimit bosh


def test_api_final_readiness_and_ops_are_read_only_pii_free_and_role_gated(w, monkeypatch):
    ready_world(w)
    mk(w.eng, "ro@example.com", role="operator")
    c = TestClient(create_app(w.eng))
    ro = bearer(token_for(c, "ro@example.com"))
    assert c.get("/admin/billing/final-readiness").status_code == 401
    before = [n(w, t) for t in (AuditLog, Invoice)]
    r = c.get("/admin/billing/final-readiness", headers=ro)
    o = c.get("/admin/billing/ops", headers=ro)
    assert r.status_code == 200 and o.status_code == 200
    assert {"status", "mode", "checks", "alerts"} <= set(r.json()) and {
        "authority",
        "import",
        "shadow",
        "usage",
        "worker",
        "settlement",
    } <= set(o.json())
    blob = r.text + o.text
    assert "@" not in blob and "Acme" not in blob and "Rr. 1" not in blob
    assert [n(w, t) for t in (AuditLog, Invoice)] == before
    assert c.post("/admin/billing/final-readiness", headers=ro).status_code == 405
    assert (
        c.post(
            f"/admin/billing/invoices/{uuid.uuid4()}/void", json={"reason": "xxx"}, headers=ro
        ).status_code
        == 403
    )
