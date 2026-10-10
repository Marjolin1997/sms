# ruff: noqa: F811
"""M10-S5 — çështjet e bootstrap-it dhe zgjidhja, prova e cutover-it, ACK i lidhur me provën, gatishmëria FINALE, rikthimi, canary, alertat, migrimi 0032, gara PG."""

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.models.admin import AuditLog
from app.models.messaging import ApprovalStatus
from app.models.sender_authority import (
    SenderAuthorityComparison,
    SenderAuthorityImmutableError,
    SenderBootstrapIssue,
    SenderBootstrapState,
    SenderCutoverEvidence,
)
from app.models.sender_request import SenderRequestOutbox
from app.models.sender_sync import SenderSyncCursor, SyncedSenderAuthorization
from app.services import messages as msgs
from app.services import sender_alerts as al
from app.services import sender_authority as sau
from app.services import sender_authority_readiness as ar
from app.services import sender_bootstrap as eb
from app.services import sender_cutover as co
from app.services import sender_ids as sid
from app.services import sender_policy_readiness as pr
from tests.test_central import IS_PG, enterprise_alembic, make_db  # noqa: F401
from tests.test_m10s4_authority import _reset, fresh, healthy, mode, project  # noqa: F401
from tests.test_pipeline import OK, fake, world  # noqa: F401

NOW = datetime.now(UTC)


def mk(db, value, status="approved", country="AL"):
    s = sid.request(db, "c1", country, value)
    if status == "approved":
        sid.approve(db, s.id, "staff")
    elif status == "rejected":
        sid.reject(db, s.id, "staff", "x")
    db.commit()
    return s


def central_report(cat, sender):
    return {"schema": eb.REPORT_SCHEMA, "items": [{"sender_id": sender.id, "category": cat}]}


@pytest.fixture
def ready(db, monkeypatch, tmp_path):
    """Gjendje e shëndetshme shadow: sync, bootstrap i përfunduar, mostra, migrime aktuale."""
    healthy(db, monkeypatch, tmp_path, "shadow", samples=3)
    monkeypatch.setattr(pr, "migration_state", lambda: ("0032", "0032"))
    monkeypatch.setattr(settings, "sender_evidence_min_samples", 3)
    return {"central": {"status": "PASS"}}


def fails(db, **kw):
    return {c.name for c in pr.checks(db, min_samples=3, **kw) if c.level == "FAIL"}


# =============================================================================================================
# çështjet e bootstrap-it dhe zgjidhja
# =============================================================================================================


def test_policy_denied_becomes_a_blocking_issue_and_is_resolved_only_by_an_explicit_audited_decision(
    db, world
):
    s = mk(db, "POLDN1", country="XK")
    rep = eb.reconcile(db, record=True, central_report=central_report("policy_denied", s))
    db.commit()
    assert [i["category"] for i in rep["items"] if i["sender_id"] == s.id] == [
        "policy_denied"
    ] and rep["unresolved"] >= 1
    st = db.get(SenderBootstrapState, 1)
    assert st.completed_at is None
    issue = db.scalar(select(SenderBootstrapIssue).where(SenderBootstrapIssue.sender_id == s.id))
    assert issue.resolved_at is None and len(issue.identity_hash) == 16 and issue.detected_at
    eb.resolve(
        db,
        sender_id=s.id,
        category="policy_denied",
        resolution="accepted_not_migrated",
        actor="ops@acme",
        reason="XK alphanumeric is banned by policy; customer informed",
        evidence_ref="TICKET-1",
    )
    db.commit()
    db.refresh(issue)
    assert (issue.resolution, issue.resolved_by, issue.evidence_ref) == (
        "accepted_not_migrated",
        "ops@acme",
        "TICKET-1",
    ) and issue.resolved_at
    audit = db.scalars(select(AuditLog).where(AuditLog.action == "sender.bootstrap_resolve")).all()
    assert len(audit) == 1 and audit[0].actor == "ops@acme" and "policy_denied" in audit[0].detail
    again = eb.reconcile(db, record=True, central_report=central_report("policy_denied", s))
    db.commit()
    assert again["accepted"] == 1 and again["unresolved"] == rep["unresolved"] - 1
    # Central s'preket: sender-i NUK u bë i miratuar në Central për ta bërë rakordimin zero
    assert db.scalar(select(func.count()).select_from(SyncedSenderAuthorization)) == 0


def test_invalid_resolutions_are_rejected(db, world):
    a = mk(db, "RESV01")
    eb.reconcile(db, record=True, central_report=central_report("policy_denied", a))
    db.commit()
    kw = dict(sender_id=a.id, category="policy_denied", actor="op", reason="r")
    for bad in (
        dict(resolution="corrected"),  # vetëm sistemi
        dict(resolution="whatever"),
        dict(resolution="accepted_not_migrated", actor=""),
        dict(resolution="accepted_not_migrated", reason=""),
        dict(resolution="accepted_not_migrated", reason="x" * 256),
        dict(resolution="accepted_not_migrated", evidence_ref=""),
        dict(resolution="sender_deactivated"),  # sender-i ende i miratuar lokalisht
        dict(
            resolution="accepted_not_migrated", category="identity_conflict"
        ),  # s'ka çështje të hapur
    ):
        with pytest.raises(eb.ResolutionError):
            eb.resolve(db, **{**kw, **bad})
    with pytest.raises(eb.ResolutionError):
        eb.resolve(
            db,
            sender_id=a.id,
            category="missing_in_central",
            resolution="accepted_not_migrated",
            actor="op",
            reason="r",
        )
    db.rollback()


def test_deactivated_resolution_requires_a_real_local_deactivation_and_issues_auto_correct(
    db, world
):
    a = mk(db, "DEACT1")
    eb.reconcile(db, record=True, central_report=central_report("identity_conflict", a))
    db.commit()
    sid.revoke(db, a.id, "staff", "customer left")
    db.commit()
    eb.resolve(
        db,
        sender_id=a.id,
        category="identity_conflict",
        resolution="sender_deactivated",
        actor="op",
        reason="customer left",
    )
    db.commit()
    b = mk(db, "AUTOC1")
    eb.reconcile(db, record=True, central_report=central_report("global_key_conflict", b))
    db.commit()
    assert (
        db.scalar(
            select(func.count())
            .select_from(SenderBootstrapIssue)
            .where(SenderBootstrapIssue.resolved_at.is_(None))
        )
        >= 1
    )
    sid.revoke(db, b.id, "staff", "dropped")
    db.commit()
    eb.reconcile(db, record=True)  # nuk zbulohet më ⇒ zgjidhje automatike
    db.commit()
    i = db.scalar(select(SenderBootstrapIssue).where(SenderBootstrapIssue.sender_id == b.id))
    assert (i.resolution, i.resolved_by) == ("corrected", "system:reconcile")


def test_issue_history_is_never_deleted_or_rewritten_and_a_new_open_issue_can_follow(db, world):
    s = mk(db, "HIST01")
    eb.reconcile(db, record=True, central_report=central_report("policy_denied", s))
    db.commit()
    eb.resolve(
        db,
        sender_id=s.id,
        category="policy_denied",
        resolution="accepted_not_migrated",
        actor="op",
        reason="r",
    )
    db.commit()
    i = db.scalar(select(SenderBootstrapIssue).where(SenderBootstrapIssue.sender_id == s.id))
    i.resolution = "sender_deactivated"
    with pytest.raises(SenderAuthorityImmutableError):
        db.flush()
    db.rollback()
    i = db.scalar(select(SenderBootstrapIssue).where(SenderBootstrapIssue.sender_id == s.id))
    i.category = "identity_conflict"
    with pytest.raises(SenderAuthorityImmutableError):
        db.flush()
    db.rollback()
    db.delete(db.scalar(select(SenderBootstrapIssue).where(SenderBootstrapIssue.sender_id == s.id)))
    with pytest.raises(SenderAuthorityImmutableError):
        db.flush()
    db.rollback()
    db.add(
        SenderBootstrapIssue(sender_id=s.id, category="policy_denied", identity_hash="0" * 16)
    )  # i ri i hapur pas të zgjidhurit: lejohet
    db.commit()
    db.add(
        SenderBootstrapIssue(sender_id=s.id, category="policy_denied", identity_hash="0" * 16)
    )  # dy të hapura: jo
    with pytest.raises(Exception):  # noqa: B017
        db.flush()
    db.rollback()


def test_bootstrap_final_gate_blocks_on_open_issues_and_accepts_explained_ones(db, ready):
    s = mk(db, "GATE01")
    project(db, "GATE01", "approved")
    assert "bootstrap_issues_resolved" not in fails(db, central_readiness=ready["central"])
    db.add(SenderBootstrapIssue(sender_id=s.id, category="policy_denied", identity_hash="0" * 16))
    db.commit()
    assert "bootstrap_issues_resolved" in fails(db, central_readiness=ready["central"])
    eb.resolve(
        db,
        sender_id=s.id,
        category="policy_denied",
        resolution="accepted_not_migrated",
        actor="op",
        reason="r",
    )
    db.commit()
    assert "bootstrap_issues_resolved" not in fails(db, central_readiness=ready["central"])


# =============================================================================================================
# gatishmëria FINALE
# =============================================================================================================


def test_final_readiness_passes_when_every_prerequisite_holds(db, ready):
    items = pr.checks(db, min_samples=3, central_readiness=ready["central"])
    assert pr.overall(items) in ("PASS", "WARN") and not [c for c in items if c.level == "FAIL"], {
        c.name: c.reason for c in items if c.level == "FAIL"
    }
    names = {c.name for c in items}
    assert {
        "migrations_current",
        "bootstrap_issues_resolved",
        "dispatch_recheck_enabled",
        "review_freeze",
        "read_model_available",
        "scope_sender_read",
        "scope_sender_report",
        "production_security_config",
        "rollback_safety",
        "central_readiness",
        "sync_healthy",
        "request_transport",
        "bootstrap_complete",
        "critical_drift",
        "shadow_samples",
        "production_ack",
    } <= names
    assert len(pr.readiness_hash(items)) == 64


def test_final_readiness_fails_for_each_hard_gate(db, ready, monkeypatch):
    c = ready["central"]
    # migrime prapa
    monkeypatch.setattr(pr, "migration_state", lambda: ("0031", "0032"))
    assert "migrations_current" in fails(db, central_readiness=c)
    monkeypatch.setattr(pr, "migration_state", lambda: ("0032", "0032"))
    # bootstrap i pazgjidhur
    st = db.get(SenderBootstrapState, 1)
    st.unresolved_count, st.completed_at = 2, None
    db.commit()
    assert {"bootstrap_complete", "bootstrap_unresolved"} <= fails(db, central_readiness=c)
    st.unresolved_count, st.completed_at = 0, datetime.now(UTC) - timedelta(hours=1)
    db.commit()
    # drift kritik
    db.add(
        SenderAuthorityComparison(
            ref="d",
            country="AL",
            category="central_missing",
            local_allowed=True,
            central_allowed=False,
            central_reason="missing",
            identity_hash="0" * 16,
        )
    )
    db.commit()
    assert "critical_drift" in fails(db, central_readiness=c)
    # gap sinkronizimi
    cur = db.get(SenderSyncCursor, 1)
    cur.epoch = None
    db.commit()
    assert "sync_healthy" in fails(db, central_readiness=c)
    # recheck i çaktivizuar për central
    monkeypatch.setattr(settings, "sender_dispatch_recheck", False)
    assert "dispatch_recheck_enabled" in fails(db, target="central", central_readiness=c)
    # reporter i padëmtuar (dështim permanent)
    row = SenderRequestOutbox
    s = mk(db, "REPF01")
    r = db.scalar(select(row).where(row.sender_id == s.id))
    r.state, r.last_error_code = "failed", "conflict"
    db.commit()
    assert "request_transport" in fails(db, central_readiness=c)
    # Central readiness FAIL ose mungon
    assert "central_readiness" in fails(db, central_readiness={"status": "FAIL"})
    assert {x.name: x.level for x in pr.checks(db, min_samples=3)}["central_readiness"] == "WARN"


def test_target_central_does_not_require_an_ack_but_runtime_central_in_production_does(
    db, ready, monkeypatch
):
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "cp_base_url", "https://central.example")
    assert "production_ack" not in fails(db, target="central", central_readiness=ready["central"])
    mode(monkeypatch, "central")
    monkeypatch.setattr(settings, "sender_authority_ack", "")
    assert "production_ack" in fails(db, central_readiness=ready["central"])


def test_staleness_fails_operational_readiness_but_never_denies_authorization(
    db, ready, world, fake, monkeypatch
):
    cur = db.get(SenderSyncCursor, 1)
    cur.last_success_at = datetime(2020, 1, 1, tzinfo=UTC)
    db.commit()
    assert "sync_healthy" in fails(db, central_readiness=ready["central"])  # readiness operacionale
    project(db, "ACME", "approved")
    sau.clear_cache()
    mode(monkeypatch, "central")
    m = msgs.submit(db, "c1", "k-stale", OK, "ACME", text="hi")  # autorizimi: fail-static
    assert m.sender_authority_source == "central"


def test_policy_readiness_cli_json_and_exit_codes(db, ready, tmp_path, capsys):
    from scripts import sender_policy_readiness as cli

    central = tmp_path / "c.json"
    central.write_text(json.dumps({"status": "PASS"}))
    code = cli.main(
        ["--json", "--target", "central", "--central-readiness", str(central), "--min-samples", "3"]
    )
    doc = json.loads(capsys.readouterr().out)
    assert code in (0, 1) and {"status", "readiness_hash", "checks", "metrics"} <= set(doc)
    assert cli.main(["--min-samples", "99999", "--target", "central"]) == 1
    capsys.readouterr()


# =============================================================================================================
# prova e cutover-it dhe ACK
# =============================================================================================================


def evidence(db, ready, **kw):
    p = co.build_evidence(
        db,
        kind="pre_cutover",
        actor="ops",
        code_revision="abc123",
        central_readiness=ready["central"],
        min_samples=3,
        now=kw.pop("now", NOW),
        **kw,
    )
    return co.record_evidence(db, p)


def test_evidence_is_recorded_immutable_idempotent_and_binds_the_ack(db, ready, monkeypatch):
    row, created = evidence(db, ready)
    db.commit()
    assert (
        created
        and len(row.evidence_hash) == 64
        and row.kind == "pre_cutover"
        and row.authority_version == 1
    )
    p = row.payload
    assert {
        "environment",
        "code_revision",
        "bootstrap",
        "sync",
        "shadow",
        "readiness",
        "open_issues",
        "generated_at",
        "actor",
    } <= set(p) and p["shadow"]["comparisons_total"] == 3
    again, created2 = evidence(db, ready)
    assert (
        not created2
        and again.id == row.id
        and db.scalar(select(func.count()).select_from(SenderCutoverEvidence)) == 1
    )
    row.actor = "x"
    with pytest.raises(SenderAuthorityImmutableError):
        db.flush()
    db.rollback()
    db.delete(db.scalar(select(SenderCutoverEvidence)))
    with pytest.raises(SenderAuthorityImmutableError):
        db.flush()
    db.rollback()
    monkeypatch.setattr(settings, "sender_authority_ack", row.evidence_hash)
    assert ar.ack_status(db)[0] is True


def test_ack_is_rejected_when_unknown_wrong_env_wrong_version_or_when_bootstrap_evidence_changed(
    db, ready, monkeypatch
):
    row, _ = evidence(db, ready)
    db.commit()
    monkeypatch.setattr(settings, "sender_authority_ack", "")
    assert not ar.ack_status(db)[0]
    monkeypatch.setattr(settings, "sender_authority_ack", "a" * 64)
    assert "does not match" in ar.ack_status(db)[1]
    monkeypatch.setattr(settings, "sender_authority_ack", row.evidence_hash)
    assert ar.ack_status(db)[0]
    monkeypatch.setattr(settings, "env", "production")
    assert "different environment" in ar.ack_status(db)[1]
    monkeypatch.setattr(settings, "env", "development")
    st = db.get(SenderBootstrapState, 1)
    st.report_hash = "f" * 64  # prova e bootstrap-it ndryshoi pas ACK-ut
    db.commit()
    ok, why = ar.ack_status(db)
    assert not ok and "stale" in why
    st.report_hash = row.bootstrap_report_hash
    db.commit()
    old = dict(row.payload, authority_version=0)  # version i vjetër i autoritetit
    r2, _ = co.record_evidence(db, old)
    db.commit()
    monkeypatch.setattr(settings, "sender_authority_ack", r2.evidence_hash)
    assert "authority version" in ar.ack_status(db)[1]


def test_pre_cutover_evidence_is_refused_while_readiness_has_a_failure(db, ready):
    cur = db.get(SenderSyncCursor, 1)
    cur.epoch = None
    db.commit()
    with pytest.raises(co.EvidenceRefused) as e:
        evidence(db, ready)
    assert "sync_healthy" in str(e.value)
    assert db.scalar(select(func.count()).select_from(SenderCutoverEvidence)) == 0


def test_complete_cutover_needs_central_a_valid_reference_and_a_central_authorised_canary(
    db, world, fake, ready, monkeypatch
):
    project(db, "ACME", "approved")
    row, _ = evidence(db, ready)
    db.commit()
    with pytest.raises(co.EvidenceRefused):
        co.complete_cutover(
            db, actor="ops", ref_hash=row.evidence_hash, canary_ref="x", code_revision="r"
        )  # nuk është central
    fresh(db)
    mode(monkeypatch, "central")
    m = msgs.submit(db, "c1", "canary-1", OK, "ACME", text="canary")
    db.commit()
    with pytest.raises(co.EvidenceRefused):
        co.complete_cutover(
            db, actor="ops", ref_hash="0" * 64, canary_ref=m.public_id, code_revision="r"
        )
    with pytest.raises(co.EvidenceRefused):
        co.complete_cutover(
            db, actor="ops", ref_hash=row.evidence_hash, canary_ref="no-such", code_revision="r"
        )
    post, created = co.complete_cutover(
        db, actor="ops", ref_hash=row.evidence_hash, canary_ref=m.public_id, code_revision="r"
    )
    db.commit()
    assert (
        created
        and post.kind == "post_cutover"
        and post.canary_ref == m.public_id
        and post.ref_hash == row.evidence_hash
    )


def test_cutover_cli_evidence_status_canary_and_rollback(
    db, world, fake, ready, monkeypatch, capsys
):
    from scripts import sender_cutover as cli

    project(db, "ACME", "approved")
    fresh(db)
    assert cli.main(["evidence", "--actor", "ops", "--code-revision", "r1"]) in (0, 1)
    capsys.readouterr()
    monkeypatch.setattr(settings, "sender_evidence_min_samples", 3)
    # canary dry-run kalon nga fasada normale (nuk ka përjashtim): senderi i panjohur refuzohet
    assert (
        cli.main(
            ["canary", "--owner-ref", "c1", "--country", "AL", "--sender", "NOPE1", "--to", OK]
        )
        == 1
    )
    out = json.loads(capsys.readouterr().out)
    assert out["allowed"] is False
    mode(monkeypatch, "central")
    assert (
        cli.main(
            [
                "canary",
                "--owner-ref",
                "c1",
                "--country",
                "AL",
                "--sender",
                "ACME",
                "--to",
                OK,
                "--send",
                "--key",
                "cn-1",
            ]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["allowed"] and out["authority_source"] == "central" and out["sent"]
    assert cli.main(["status"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["mode"] == "central" and "ack_valid" in st
    assert cli.main(["rollback", "--to", "shadow"]) in (0, 1)
    capsys.readouterr()


# =============================================================================================================
# rikthimi
# =============================================================================================================


def test_central_to_shadow_is_always_possible_and_keeps_central_history(
    db, world, ready, monkeypatch
):
    project(db, "ACME", "revoked")
    before = db.scalar(select(func.count()).select_from(SyncedSenderAuthorization))
    items = {c.name: c for c in ar.rollback_checks(db, "shadow")}
    assert items["data_preserved"].level == "PASS" and ar.overall(list(items.values())) != "FAIL"
    assert db.scalar(select(func.count()).select_from(SyncedSenderAuthorization)) == before


def test_unsafe_local_rollback_is_blocked_until_divergence_is_explicitly_accepted_and_recorded(
    db, world, ready, monkeypatch, capsys
):
    from scripts import sender_cutover as cli

    project(db, "ACME", "revoked")  # Central e ka revokuar; lokali ende "approved"
    items = {c.name: c for c in ar.rollback_checks(db, "local")}
    assert items["reauthorize_risk"].level == "FAIL"
    assert cli.main(["rollback", "--to", "local"]) == 1
    capsys.readouterr()
    assert (
        cli.main(["rollback", "--to", "local", "--accept-divergence"]) == 1
    )  # kërkon aktor + arsye
    capsys.readouterr()
    assert (
        cli.main(
            [
                "rollback",
                "--to",
                "local",
                "--accept-divergence",
                "--actor",
                "ops",
                "--reason",
                "incident 42; manual review",
            ]
        )
        == 0
    )
    capsys.readouterr()
    ack = db.scalars(
        select(SenderCutoverEvidence).where(SenderCutoverEvidence.kind == "rollback_ack")
    ).all()
    assert (
        len(ack) == 1
        and ack[0].payload["rollback"]["reason"].startswith("incident 42")
        and ack[0].actor == "ops"
    )


def test_reconcile_local_makes_rollback_safe_and_is_refused_under_central(
    db, world, ready, monkeypatch
):
    project(db, "ACME", "revoked")
    mode(monkeypatch, "central")
    with pytest.raises(co.EvidenceRefused):
        co.reconcile_local_for_rollback(db, "ops")  # i ngrirë nën central
    mode(monkeypatch, "shadow")
    assert co.reconcile_local_for_rollback(db, "ops") == 1
    db.commit()
    s = db.scalar(select(ar.SenderId).where(ar.SenderId.value == "ACME"))
    assert s.status == ApprovalStatus.REVOKED
    assert {c.name: c for c in ar.rollback_checks(db, "local")}["reauthorize_risk"].level == "PASS"
    mode(monkeypatch, "local")
    with pytest.raises(sau.SenderNotAllowed):
        msgs.submit(
            db, "c1", "k-rb", OK, "ACME", text="x"
        )  # rikthimi nuk ri-autorizoi senderin e revokuar


# =============================================================================================================
# alertat
# =============================================================================================================


def test_alerts_use_a_bounded_vocabulary_report_levels_and_expose_no_sender_values(
    db, ready, capsys
):
    from scripts import sender_alerts as cli

    names = {a.name for a in al.evaluate(db)}
    assert names == {
        "sender_sync_lag",
        "sender_sync_age_seconds",
        "sender_sync_gap_recoveries_total",
        "sender_request_oldest_age_seconds",
        "sender_request_permanent_failures",
        "sender_bootstrap_unresolved",
        "sender_shadow_critical_drift",
        "sender_central_deny_ratio",
        "sender_dispatch_recheck_blocked_1h",
        "sender_projection_stale",
        "sender_policy_readiness_fail",
    }
    db.add(
        SenderAuthorityComparison(
            ref="d",
            country="AL",
            category="central_missing",
            local_allowed=True,
            central_allowed=False,
            central_reason="missing",
            identity_hash="0" * 16,
        )
    )
    db.commit()
    lv = {a.name: a.level for a in al.evaluate(db)}
    assert lv["sender_shadow_critical_drift"] == "critical"
    assert cli.main(["--json"]) == 1
    out = capsys.readouterr().out
    assert all(set(x) == {"name", "level", "value", "threshold"} for x in json.loads(out))


def test_dispatch_recheck_blocks_are_counted_by_alerts(db, world, fake, monkeypatch):
    project(db, "ACME", "approved")
    fresh(db)
    mode(monkeypatch, "central")
    m = msgs.submit(db, "c1", "k-al", OK, "ACME", text="x")
    db.commit()
    db.execute(
        text(
            "UPDATE sms_synced_sender_authorizations SET status='revoked', approved_key=NULL, cp_revision=9"
        )
    )
    db.commit()
    msgs.process_one(db)
    a = {x.name: x for x in al.evaluate(db)}
    assert (
        a["sender_dispatch_recheck_blocked_1h"].value == 1
        and a["sender_dispatch_recheck_blocked_1h"].level == "warning"
        and m.status.value == "failed"
    )


# =============================================================================================================
# migrimi 0032, trigger-at PG, gara PG
# =============================================================================================================


def _drift(conn):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    import app.models  # noqa: F401
    from app.core.db import Base

    ctx = MigrationContext.configure(conn, opts={"compare_type": True})
    names = ("sms_sender_bootstrap_issues", "sms_sender_cutover_evidence")
    return [
        repr(i)
        for d in compare_metadata(ctx, Base.metadata)
        for i in (d if isinstance(d, list) else [d])
        if any(n in repr(i) for n in names)
    ]


def test_enterprise_0032_is_additive_reversible_and_matches_metadata(make_db):
    url = make_db("ent")
    enterprise_alembic(url, "upgrade", "0031")
    eng = create_engine(url)
    before = {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    }
    enterprise_alembic(url, "upgrade", "0032")
    after = {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    }
    assert set(after) - set(before) == {
        "sms_sender_bootstrap_issues",
        "sms_sender_cutover_evidence",
    }
    assert all(after[t] == cols for t, cols in before.items())
    with eng.connect() as c:
        assert _drift(c) == []
    enterprise_alembic(url, "downgrade", "0031")
    assert set(inspect(eng).get_table_names()) == set(before)
    enterprise_alembic(url, "upgrade", "head")
    with eng.connect() as c:
        assert _drift(c) == []
    eng.dispose()


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_triggers_protect_issues_and_evidence(make_db):
    url = make_db("ent")
    if not url.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    enterprise_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    with Session(eng) as s:
        x = sid.request(s, "c1", "AL", "TRIGGR")
        s.commit()
        sender = x.id
    with eng.begin() as c:
        c.execute(
            text(
                f"insert into sms_sender_bootstrap_issues (sender_id, category, identity_hash, detected_at) values ({sender}, 'policy_denied', 'h', now())"
            )
        )
        c.execute(
            text(
                "insert into sms_sender_cutover_evidence (kind, evidence_hash, authority_version, environment, code_revision, actor, readiness_status, readiness_hash, payload, created_at) values ('pre_cutover','h1',1,'development','r','a','PASS','x','{}'::json,now())"
            )
        )
        c.execute(
            text(
                "update sms_sender_bootstrap_issues set resolved_at = now(), resolution='accepted_not_migrated', resolved_by='op'"
            )
        )  # zgjidhja e parë: lejohet
    for stmt in (
        "UPDATE sms_sender_bootstrap_issues SET resolution='sender_deactivated'",  # zgjidhja është finale
        "UPDATE sms_sender_bootstrap_issues SET category='identity_conflict'",
        "DELETE FROM sms_sender_bootstrap_issues",
        "TRUNCATE sms_sender_bootstrap_issues",
        "UPDATE sms_sender_cutover_evidence SET actor='x'",
        "DELETE FROM sms_sender_cutover_evidence",
        "TRUNCATE sms_sender_cutover_evidence",
    ):
        with eng.connect() as c:
            with pytest.raises(DBAPIError):
                c.execute(text(stmt))
                c.commit()
    eng.dispose()


def _threads(fns):
    errs, gate = [], threading.Barrier(len(fns))

    def wrap(f):
        def run():
            try:
                gate.wait(timeout=20)
                f()
            except Exception as e:  # noqa: BLE001
                errs.append(repr(e))

        return run

    ts = [threading.Thread(target=wrap(f)) for f in fns]
    [t.start() for t in ts]
    [t.join(90) for t in ts]
    return errs


def need_pg():
    if engine.dialect.name != "postgresql":
        pytest.skip("postgres parametrization only")


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_cutover_evidence_concurrent_writers_leave_exactly_one_row(db, ready):
    need_pg()
    payload = co.build_evidence(
        db,
        kind="pre_cutover",
        actor="ops",
        code_revision="r",
        central_readiness=ready["central"],
        min_samples=3,
        now=NOW,
    )
    db.rollback()

    def write():
        with SessionLocal() as s:
            co.record_evidence(s, payload)
            s.commit()

    assert not _threads([write, write, write])
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(SenderCutoverEvidence)) == 1


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_authority_switch_during_traffic_and_local_review_during_the_switch_stay_coherent(
    db, world, fake, monkeypatch
):
    need_pg()
    project(db, "ACME", "approved")
    fresh(db)
    s = mk(db, "SWTCH1", "pending")
    mode(monkeypatch, "shadow")
    results = []

    def traffic(i):
        def go():
            with SessionLocal() as x:
                try:
                    msgs.submit(x, "c1", f"sw{i}", OK, "ACME", text="hi")
                    x.commit()
                    results.append("ok")
                except sau.SenderNotAllowed:
                    results.append("denied")

        return go

    def flip():
        threading.Event().wait(0.05)
        monkeypatch.setattr(settings, "sender_authority", "central")

    def review():
        with SessionLocal() as x:
            try:
                sid.approve(x, s.id, "staff")
                x.commit()
                results.append("approved")
            except sau.SenderAuthorityFrozen:
                x.rollback()
                results.append("frozen")

    errs = _threads([traffic(1), traffic(2), traffic(3), flip, review])
    assert not errs, errs
    assert (
        set(results) <= {"ok", "denied", "approved", "frozen"}
        and len(results) == 5 - 1 + 0
        or len(results) >= 4
    )
    db.expire_all()
    status = db.get(ar.SenderId, s.id).status
    assert status in (ApprovalStatus.APPROVED, ApprovalStatus.PENDING) and (
        status == ApprovalStatus.APPROVED
    ) == ("approved" in results)


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_projection_update_concurrent_with_submit_and_rollback_check_vs_new_central_decision(
    db, world, fake, monkeypatch
):
    need_pg()
    row = project(db, "ACME", "approved")
    fresh(db)
    mode(monkeypatch, "central")
    rid = row.id
    outcomes = []

    def submit():
        with SessionLocal() as x:
            try:
                msgs.submit(x, "c1", "pj1", OK, "ACME", text="hi")
                x.commit()
                outcomes.append("sent")
            except sau.SenderNotAllowed:
                outcomes.append("denied")

    def revoke():
        with SessionLocal() as x:
            x.execute(
                text(
                    f"UPDATE sms_synced_sender_authorizations SET status='revoked', approved_key=NULL, cp_revision=cp_revision+1 WHERE id={rid}"
                )
            )
            x.commit()

    def rollback_check():
        with SessionLocal() as x:
            items = ar.rollback_checks(x, "local")
            assert {c.name for c in items} >= {"reauthorize_risk", "divergence"}

    assert not _threads([submit, revoke, rollback_check])
    assert outcomes in (["sent"], ["denied"])


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_bootstrap_reconcile_and_incoming_sender_request_do_not_conflict(db, world):
    need_pg()
    mk(db, "BASE01")

    def reconcile():
        with SessionLocal() as x:
            eb.reconcile(x, record=True)
            x.commit()

    def request():
        with SessionLocal() as x:
            sid.request(x, "c1", "AL", "INCOM1")
            x.commit()

    assert not _threads([reconcile, request, reconcile])
    db.expire_all()
    assert (
        db.scalar(
            select(func.count()).select_from(ar.SenderId).where(ar.SenderId.value == "INCOM1")
        )
        == 1
    )
