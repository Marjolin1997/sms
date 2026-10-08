# ruff: noqa: F811
"""M10-S0 — kanonizimi i sender-ave në Enterprise: shërbimi kanonik i autorizimit, normalizimi case-insensitive, historia append-only e vendimeve,
provenienca e mesazhit, rregullimi i garës së kërkesës, API strikte, migrimi 0028 dhe SQL i pandryshuar në hot path."""

import threading

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.db import SessionLocal, engine
from app.core.errors import Conflict
from app.core.security import ROLE_PERMS
from app.models.messaging import (
    ApprovalStatus as S,
)
from app.models.messaging import (
    SenderDecision,
    SenderDecisionImmutableError,
    SenderId,
)
from app.services import campaigns as camp
from app.services import messages as svc
from app.services import sender_authorization as sa
from app.services import sender_ids as sid
from tests.test_campaigns import NOW, audience, campaign, drive, start  # noqa: F401
from tests.test_central import IS_PG, enterprise_alembic, make_db  # noqa: F401
from tests.test_pipeline import OK, fake, world  # noqa: F401

# SQL i matur para S0 (rev. e miratuar i M9, SQLite, owner legacy `c1`): submit 20 · autorizim 1 · inbound 1 · process_one 10
BASELINE_SUBMIT, BASELINE_AUTH, BASELINE_INBOUND, BASELINE_DISPATCH = 20, 1, 1, 10


def decisions(db, sender_id):
    return list(
        db.scalars(
            select(SenderDecision)
            .where(SenderDecision.sender_id == sender_id)
            .order_by(SenderDecision.id)
        )
    )


def is_pg():
    return engine.dialect.name == "postgresql"


def count_sql(fn):
    seen = []

    def cb(*a):
        seen.append(a[2])

    event.listen(engine, "before_cursor_execute", cb)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", cb)
    return len(seen)


# =============================================================================================================
# makina e gjendjeve + historia
# =============================================================================================================


def test_transitions_follow_the_existing_state_machine_and_record_decisions(db):
    s = sid.request(db, "c1", "AL", "ACME", actor="alice")
    assert s.status == S.PENDING
    sid.approve(db, s.id, "bob")
    assert s.status == S.APPROVED
    sid.revoke(db, s.id, "bob", "abuse")
    assert s.status == S.REVOKED
    sid.request(db, "c1", "AL", "ACME", actor="alice")  # revoked → pending
    assert s.status == S.PENDING
    sid.reject(db, s.id, "bob", "brand mismatch")
    assert s.status == S.REJECTED
    sid.request(db, "c1", "AL", "ACME", actor="alice")  # rejected → pending
    assert s.status == S.PENDING
    h = decisions(db, s.id)
    assert [d.decision for d in h] == [
        "requested",
        "approved",
        "revoked",
        "resubmitted",
        "rejected",
        "resubmitted",
    ]
    assert [d.to_status for d in h] == [
        "pending",
        "approved",
        "revoked",
        "pending",
        "rejected",
        "pending",
    ]
    assert [d.decided_by for d in h] == ["alice", "bob", "bob", "alice", "bob", "alice"]
    assert s.current_decision_id == h[-1].id and all(
        d.source == "local" and d.policy_revision is None for d in h
    )


def test_illegal_transitions_still_conflict_and_write_no_decision(db):
    s = sid.request(db, "c1", "AL", "ACME")
    n = len(decisions(db, s.id))
    for fn in (lambda: sid.revoke(db, s.id, "x", "r"), lambda: sid.reject(db, s.id, "x", "")):
        with pytest.raises(Conflict):
            fn()
    sid.approve(db, s.id, "x")
    for fn in (lambda: sid.approve(db, s.id, "x"), lambda: sid.reject(db, s.id, "x", "r")):
        with pytest.raises(Conflict):
            fn()
    assert len(decisions(db, s.id)) == n + 1


def test_rejection_and_revocation_reasons_survive_resubmit(db):
    s = sid.request(db, "c1", "AL", "ACME")
    sid.reject(db, s.id, "adm", "no brand proof", evidence_ref="TICKET-1")
    sid.request(db, "c1", "AL", "ACME")
    assert s.status == S.PENDING and s.reason is None  # gjendja aktuale: arsyeja pastrohet
    sid.approve(db, s.id, "adm")
    sid.revoke(db, s.id, "adm", "fraud report")
    sid.request(db, "c1", "AL", "ACME")
    reasons = {d.decision: (d.reason, d.evidence_ref) for d in decisions(db, s.id) if d.reason}
    assert reasons == {
        "rejected": ("no brand proof", "TICKET-1"),
        "revoked": ("fraud report", None),
    }


def test_decision_history_is_append_only_in_the_orm(db):
    s = sid.request(db, "c1", "AL", "ACME")
    db.commit()
    d = decisions(db, s.id)[0]
    d.reason = "tamper"
    with pytest.raises(SenderDecisionImmutableError):
        db.flush()
    db.rollback()
    d = decisions(db, s.id)[0]
    db.delete(d)
    with pytest.raises(SenderDecisionImmutableError):
        db.flush()
    db.rollback()


# =============================================================================================================
# normalizim case-insensitive, unikalitet global, shtete të pavarura
# =============================================================================================================


def test_authorization_is_case_insensitive_and_display_casing_is_preserved(db):
    s = sid.request(db, "c1", "AL", "Acme")
    sid.approve(db, s.id, "adm")
    for v in ("Acme", "ACME", "acme", "aCmE"):
        a = sa.check_outbound(db, "c1", "AL", v)
        assert a.allowed and a.sender_ref == s.id and a.canonical_key == "AL:acme"
    assert s.value == "Acme" and s.norm_value == "acme" and s.approved_key == "AL:acme"
    assert sid.assert_usable(db, "c1", "al", "ACME").id == s.id


def test_case_variants_resolve_to_one_request_for_the_same_tenant_and_country(db):
    a = sid.request(db, "c1", "AL", "Acme")
    b = sid.request(db, "c1", "AL", "ACME")
    assert a.id == b.id and db.scalar(select(SenderId.id).where(SenderId.value == "ACME")) is None


def test_two_tenants_cannot_both_hold_the_same_sender_approved_whatever_the_case(db):
    x = sid.request(db, "c1", "AL", "Acme")
    y = sid.request(db, "c2", "AL", "ACME")
    sid.approve(db, x.id, "adm")
    db.commit()
    with pytest.raises(Conflict):
        sid.approve(db, y.id, "adm")
    db.rollback()
    assert db.get(SenderId, y.id).status == S.PENDING
    assert [d.decision for d in decisions(db, y.id)] == ["requested"]  # asnjë "approved" i rremë


def test_numeric_normalization_is_unchanged(db):
    s = sid.request(db, "c1", "AL", "+355691234567")
    assert (
        s.value == "355691234567" and s.norm_value == "355691234567" and s.kind.value == "numeric"
    )
    sid.approve(db, s.id, "adm")
    assert sa.check_outbound(db, "c1", "AL", "+355691234567").allowed
    assert sa.check_outbound(db, "c1", "AL", "355691234567").allowed


def test_country_states_are_independent(db):
    al = sid.request(db, "c1", "AL", "ACME")
    xk = sid.request(db, "c1", "XK", "ACME")
    sid.approve(db, al.id, "adm")
    assert sa.check_outbound(db, "c1", "AL", "ACME").allowed
    r = sa.check_outbound(db, "c1", "XK", "ACME")
    assert not r.allowed and r.category == "pending" and r.sender_ref == xk.id
    sid.reject(db, xk.id, "adm", "no")
    assert sa.check_outbound(db, "c1", "XK", "ACME").category == "rejected"
    assert sa.check_outbound(db, "c1", "ZZ", "ACME").category == "not_found"
    assert sa.check_outbound(db, "other", "AL", "ACME").category == "not_found"


def test_authorization_result_is_structured_and_not_an_orm_object(db):
    s = sid.request(db, "c1", "AL", "ACME")
    sid.approve(db, s.id, "adm")
    a = sa.check_outbound(db, "c1", "AL", "acme")
    assert (a.allowed, a.category, a.status, a.country) == (True, "approved", "approved", "AL")
    assert a.decision_ref == s.current_decision_id and a.policy_revision is None
    assert not hasattr(a, "_sa_instance_state")


# =============================================================================================================
# gara e kërkesës
# =============================================================================================================


def test_stale_read_race_yields_a_clean_conflict_not_integrity_error(db, monkeypatch):
    sid.request(db, "c1", "AL", "ACME")
    db.commit()
    monkeypatch.setattr(
        sa, "pick", lambda rows, display=None: None
    )  # kërkesa paralele "nuk e pa" rreshtin
    with pytest.raises(Conflict) as e:
        sid.request(db, "c1", "AL", "ACME")
    assert "already in progress" in str(e.value)
    db.rollback()
    assert len(list(db.scalars(select(SenderId)))) == 1


def test_api_returns_409_for_the_losing_duplicate_and_never_500(client, monkeypatch):
    body = {"owner_ref": "c1", "country": "AL", "value": "ACME"}
    assert client.post("/v1/sender-ids", json=body).status_code == 201
    monkeypatch.setattr(sa, "pick", lambda rows, display=None: None)
    r = client.post("/v1/sender-ids", json=body)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "conflict"


@pytest.mark.skipif(not IS_PG, reason="postgres concurrency")
def test_parallel_duplicate_requests_on_postgres_end_in_one_row_and_clean_outcomes(db):
    if not is_pg():
        pytest.skip("postgres parametrization only")
    out, errs = [], []
    gate = threading.Barrier(8)

    def go():
        with SessionLocal() as s:
            try:
                gate.wait()
                r = sid.request(s, "race", "AL", "RACEID")
                s.commit()
                out.append(r.id)
            except Conflict:
                s.rollback()
                out.append("conflict")
            except Exception as e:  # noqa: BLE001
                s.rollback()
                errs.append(e)

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, errs
    rows = list(db.scalars(select(SenderId).where(SenderId.owner_ref == "race")))
    assert len(rows) == 1 and any(isinstance(x, int) for x in out)
    assert [d.decision for d in decisions(db, rows[0].id)] == ["requested"]


@pytest.mark.skipif(not IS_PG, reason="postgres concurrency")
def test_concurrent_approve_vs_reject_and_case_variant_approvals_are_deterministic(db):
    if not is_pg():
        pytest.skip("postgres parametrization only")
    a = sid.request(db, "o1", "AL", "Brand")
    b = sid.request(db, "o2", "AL", "BRAND")
    c = sid.request(db, "o3", "AL", "Other")
    db.commit()
    res = {}
    gate = threading.Barrier(3)

    def run(name, fn):
        with SessionLocal() as s:
            try:
                gate.wait()
                fn(s)
                s.commit()
                res[name] = "ok"
            except Conflict:
                s.rollback()
                res[name] = "conflict"
            except Exception as e:  # noqa: BLE001
                s.rollback()
                res[name] = repr(e)

    ts = [
        threading.Thread(target=run, args=("approve_a", lambda s: sid.approve(s, a.id, "adm"))),
        threading.Thread(target=run, args=("approve_b", lambda s: sid.approve(s, b.id, "adm"))),
        threading.Thread(target=run, args=("reject_c", lambda s: sid.reject(s, c.id, "adm", "no"))),
    ]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted([res["approve_a"], res["approve_b"]]) == ["conflict", "ok"], res
    assert res["reject_c"] == "ok"
    db.expire_all()
    assert sum(db.get(SenderId, i).status == S.APPROVED for i in (a.id, b.id)) == 1
    assert (
        db.scalar(
            select(func.count()).select_from(SenderId).where(SenderId.approved_key == "AL:brand")
        )
        == 1
    )
    # approve vs reject i të njëjtit rresht: saktësisht një fiton, tjetri Conflict
    d = sid.request(db, "o4", "AL", "Racer")
    db.commit()
    res2 = {}
    gate2 = threading.Barrier(2)

    def run2(name, fn):
        with SessionLocal() as s:
            try:
                gate2.wait()
                fn(s)
                s.commit()
                res2[name] = "ok"
            except Conflict:
                s.rollback()
                res2[name] = "conflict"

    ts = [
        threading.Thread(target=run2, args=("a", lambda s: sid.approve(s, d.id, "adm"))),
        threading.Thread(target=run2, args=("r", lambda s: sid.reject(s, d.id, "adm", "no"))),
    ]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(res2.values()) == ["conflict", "ok"], res2
    db.expire_all()
    h = [x.decision for x in decisions(db, d.id)]
    assert (
        h in (["requested", "approved"], ["requested", "rejected"])
        and db.get(SenderId, d.id).status.value == h[-1]
    )


# =============================================================================================================
# submit, provenienca, fushata, inbound
# =============================================================================================================


def test_submit_passes_for_authorized_and_denies_unauthorized(db, world):
    m = svc.submit(db, "c1", "k1", OK, "acme", text="hi")  # case tjetër, i njëjti sender
    db.commit()
    assert m.status.value == "queued" and m.sender == "acme"  # mesazhi ruan siç u dërgua
    with pytest.raises(sa.SenderNotAllowed):
        svc.submit(db, "c1", "k2", OK, "NOPE", text="hi")


def test_message_freezes_sender_ref_and_decision_ref_and_old_messages_are_not_rewritten(db, world):
    s = db.scalar(select(SenderId).where(SenderId.owner_ref == "c1"))
    approved_decision = s.current_decision_id
    m = svc.submit(db, "c1", "k1", OK, "ACME", text="hi")
    db.commit()
    assert (m.sender_ref, m.sender_decision_ref, m.sender_policy_revision) == (
        s.id,
        approved_decision,
        None,
    )
    sid.revoke(db, s.id, "adm", "abuse")
    sid.request(db, "c1", "AL", "ACME")
    sid.approve(db, s.id, "adm")
    db.commit()
    db.refresh(m)
    assert m.sender_decision_ref == approved_decision and s.current_decision_id != approved_decision
    first = db.get(SenderDecision, m.sender_decision_ref)
    assert first.decision == "approved" and first.sender_id == s.id


def test_historical_message_rows_may_have_null_provenance(db, world):
    m = svc.submit(db, "c1", "k1", OK, "ACME", text="hi")
    db.commit()
    db.execute(
        text(
            "UPDATE sms_messages SET sender_ref = NULL, sender_decision_ref = NULL, sender_policy_revision = NULL"
        )
    )
    db.commit()
    db.refresh(m)
    assert (m.sender_ref, m.sender_decision_ref, m.sender_policy_revision) == (None, None, None)
    assert sa.recheck_for_dispatch(db, m.sender_ref) is None  # pa provenancë ⇒ pa rikontroll


def test_dispatch_recheck_api_is_prepared_but_dispatch_behaviour_is_unchanged(db, world, fake):
    s = db.scalar(select(SenderId).where(SenderId.owner_ref == "c1"))
    m = svc.submit(db, "c1", "k1", OK, "ACME", text="hi")
    db.commit()
    assert sa.recheck_for_dispatch(db, m.sender_ref).allowed
    sid.revoke(db, s.id, "adm", "abuse")
    db.commit()
    assert sa.recheck_for_dispatch(db, m.sender_ref).category == "revoked"
    svc.process_one(db)  # S0: dispatch NUK rikontrollon — sjellja e sotme
    assert m.status.value == "sent"


def test_campaign_schedule_is_country_free_and_recipient_submit_checks_the_country(db, world):
    sid.approve(db, sid.request(db, "c1", "XK", "OTHERX").id, "adm")  # miratuar vetëm për XK
    db.commit()
    lst, _ = audience(db, 1)
    c = campaign(db, lst, sender="OTHERX")
    start(db, c)  # schedule: kontroll pa shtet ⇒ kalon
    drive(db)
    from app.models.campaigns import CampaignRecipient

    rows = list(db.scalars(select(CampaignRecipient).where(CampaignRecipient.campaign_id == c.id)))
    assert rows and all(
        x.status.value == "skipped" and x.reason == "sender_not_allowed" for x in rows
    ), [(x.status, x.reason) for x in rows]


def test_campaign_schedule_rejects_a_sender_not_approved_anywhere_and_is_case_insensitive(
    db, world
):
    lst, _ = audience(db, 1)
    with pytest.raises(camp.InvalidCampaign):
        camp.schedule(db, "c1", campaign(db, lst, sender="NOBODY").id, None, now=NOW)
    db.rollback()
    c2 = campaign(db, lst, name="p2", sender="acme")  # miratuar si "ACME"
    camp.schedule(db, "c1", c2.id, None, now=NOW)


def test_inbound_lookup_uses_canonical_state_and_is_functionally_unchanged(db):
    s = sid.request(db, "c1", "AL", "+355690000001")
    assert sid.owners_of_number(db, "+355690000001") == []  # ende pending
    sid.approve(db, s.id, "adm")
    assert [o.owner_ref for o in sid.owners_of_number(db, "+355690000001")] == ["c1"]
    assert [o.owner_ref for o in sa.owners_of_numeric(db, "355690000001")] == ["c1"]
    alnum = sid.request(db, "c1", "AL", "BRAND")
    sid.approve(db, alnum.id, "adm")
    assert sa.owners_of_numeric(db, "BRAND") == []  # vetëm numerikët rrugëzojnë SMS hyrës
    sid.revoke(db, s.id, "adm", "x")
    assert sid.owners_of_number(db, "+355690000001") == []


# =============================================================================================================
# API: skema strikte, RBAC, audit atomik
# =============================================================================================================


def test_sender_schemas_forbid_extra_fields_and_bound_inputs(client):
    base = {"owner_ref": "c1", "country": "AL", "value": "ACME"}
    assert client.post("/v1/sender-ids", json={**base, "status": "approved"}).status_code == 422
    assert client.post("/v1/sender-ids", json={**base, "country": "ALB"}).status_code == 422
    assert client.post("/v1/sender-ids", json={**base, "country": "A1"}).status_code == 422
    sid_id = client.post("/v1/sender-ids", json=base).json()["id"]
    assert client.post(f"/v1/sender-ids/{sid_id}/approve", json={"actor": "x"}).status_code == 422
    assert (
        client.post(f"/v1/sender-ids/{sid_id}/reject", json={"reason": "x" * 256}).status_code
        == 422
    )
    assert (
        client.post(
            f"/v1/sender-ids/{sid_id}/reject", json={"reason": "r", "evidence_ref": ""}
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/v1/sender-ids/{sid_id}/reject", json={"reason": "r", "evidence_ref": "E" * 129}
        ).status_code
        == 422
    )
    ok = client.post(
        f"/v1/sender-ids/{sid_id}/reject", json={"reason": "r", "evidence_ref": "TICKET-77"}
    )
    assert ok.status_code == 200 and ok.json()["status"] == "rejected"


def test_rbac_is_unchanged():
    assert "sender:request" in ROLE_PERMS["client"] and "sender:review" not in ROLE_PERMS["client"]
    assert (
        "sender:review" in ROLE_PERMS["approver"] and "sender:request" not in ROLE_PERMS["approver"]
    )
    assert {r for r, p in ROLE_PERMS.items() if "sender:review" in p or "*" in p} == {
        "approver",
        "superadmin",
    }


def test_audit_decision_and_state_are_one_atomic_unit(client, db, monkeypatch):
    sid_id = client.post(
        "/v1/sender-ids", json={"owner_ref": "c1", "country": "AL", "value": "ACME"}
    ).json()["id"]
    import app.api.messaging as api

    def boom(*a, **k):
        raise Conflict("audit failed")

    monkeypatch.setattr(api, "audit", boom)
    assert client.post(f"/v1/sender-ids/{sid_id}/approve", json={}).status_code == 409
    monkeypatch.undo()
    db.expire_all()
    s = db.get(SenderId, sid_id)
    assert s.status == S.PENDING and s.approved_key is None
    assert [d.decision for d in decisions(db, sid_id)] == ["requested"]
    assert client.post(f"/v1/sender-ids/{sid_id}/approve", json={}).status_code == 200
    db.expire_all()
    from app.models.admin import AuditLog

    actions = [
        a.action for a in db.scalars(select(AuditLog).where(AuditLog.target_type == "sender_id"))
    ]
    assert actions.count("sender.approve") == 1 and actions.count("sender.request") == 1
    assert [d.decision for d in decisions(db, sid_id)] == ["requested", "approved"]


# =============================================================================================================
# SQL në hot path: i pandryshuar
# =============================================================================================================


def test_hot_path_sql_counts_are_unchanged_from_the_pre_s0_baseline(db, world, fake):
    assert (
        count_sql(lambda: svc.submit(db, "c1", "kb", OK, "ACME", text="hello")) == BASELINE_SUBMIT
    )
    db.commit()
    assert count_sql(lambda: sa.check_outbound(db, "c1", "AL", "ACME")) == BASELINE_AUTH
    assert count_sql(lambda: sid.owners_of_number(db, "+355690000001")) == BASELINE_INBOUND
    assert count_sql(lambda: svc.process_one(db)) == BASELINE_DISPATCH
    # provenienca ripërdor rreshtin e autorizimit: asnjë SELECT shtesë për të
    assert count_sql(lambda: sa.check_outbound(db, "c1", "AL", "acme")) == 1


# =============================================================================================================
# migrimi 0028: backfill, up/down/up, compare_metadata, trigger-a PG
# =============================================================================================================

TOUCHED = ("sms_sender_ids", "sms_sender_decisions", "sms_messages")


def _drift(conn):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    import app.models  # noqa: F401
    from app.core.db import Base

    ctx = MigrationContext.configure(conn, opts={"compare_type": True})
    return [
        repr(i)
        for d in compare_metadata(ctx, Base.metadata)
        for i in (d if isinstance(d, list) else [d])
        if any(t in repr(i) for t in TOUCHED)
    ]


def test_migration_0028_backfills_preserves_rows_and_is_reversible(make_db):
    from sqlalchemy import inspect

    url = make_db("ent")
    enterprise_alembic(url, "upgrade", "0027")
    eng = create_engine(url)
    with eng.begin() as c:
        for i, (st, who, reason) in enumerate(
            (
                ("PENDING", None, None),
                ("APPROVED", "adm", None),
                ("REJECTED", "adm", "no proof"),
                ("REVOKED", "adm", "abuse"),
            )
        ):
            c.execute(
                text(
                    "INSERT INTO sms_sender_ids (owner_ref, country, value, kind, status, reviewed_by, reason, created_at, approved_key) "
                    "VALUES (:o, 'AL', :v, 'ALPHANUMERIC', :s, :w, :r, '2030-01-01 00:00:00', :k)"
                ),
                {
                    "o": f"t{i}",
                    "v": f"MiXed{i}",
                    "s": st,
                    "w": who,
                    "r": reason,
                    "k": f"AL:mixed{i}" if st == "APPROVED" else None,
                },
            )
    enterprise_alembic(url, "upgrade", "0028")
    with eng.connect() as c:
        rows = c.execute(
            text(
                "SELECT id, value, norm_value, status, current_decision_id FROM sms_sender_ids ORDER BY id"
            )
        ).all()
        assert [r.norm_value for r in rows] == ["mixed0", "mixed1", "mixed2", "mixed3"]
        assert [r.value for r in rows] == [
            "MiXed0",
            "MiXed1",
            "MiXed2",
            "MiXed3",
        ]  # display i paprekur
        dec = c.execute(
            text(
                "SELECT sender_id, decision, to_status, decided_by, reason, source FROM sms_sender_decisions ORDER BY sender_id"
            )
        ).all()
        assert [(d.decision, d.decided_by, d.source) for d in dec] == [
            ("requested", None, "backfill"),
            ("approved", None, "backfill"),
            ("rejected", None, "backfill"),
            ("revoked", None, "backfill"),
        ]
        assert [d.reason for d in dec] == [None, None, "no proof", "abuse"]
        assert all(r.current_decision_id is not None for r in rows)
        insp = inspect(c)
        cols = {x["name"]: x["nullable"] for x in insp.get_columns("sms_messages")}
        assert (
            cols["sender_ref"] and cols["sender_decision_ref"] and cols["sender_policy_revision"]
        )  # nullable: rreshtat historikë mbeten NULL
        assert _drift(c) == []
    enterprise_alembic(url, "downgrade", "0027")
    with eng.connect() as c:
        assert "sms_sender_decisions" not in inspect(c).get_table_names()
        assert "norm_value" not in {x["name"] for x in inspect(c).get_columns("sms_sender_ids")}
        assert (
            c.execute(text("SELECT count(*) FROM sms_sender_ids")).scalar() == 4
        )  # rreshtat e ruajtur
    enterprise_alembic(url, "upgrade", "head")
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM sms_sender_decisions")).scalar() == 4
        assert _drift(c) == []


def test_pg_triggers_make_decision_history_append_only_on_a_migrated_database(make_db):
    url = make_db("ent")
    if not url.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    enterprise_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    with eng.begin() as c:
        sid_ = c.execute(
            text(
                "INSERT INTO sms_sender_ids (owner_ref, country, value, kind, status, created_at, norm_value) "
                "VALUES ('t', 'AL', 'ACME', 'ALPHANUMERIC', 'PENDING', now(), 'acme') RETURNING id"
            )
        ).scalar()
        c.execute(
            text(
                "INSERT INTO sms_sender_decisions (sender_id, decision, to_status, decided_at, decided_by, source) "
                "VALUES (:s, 'requested', 'pending', now(), 'alice', 'local')"
            ),
            {"s": sid_},
        )
    for sql in (
        "UPDATE sms_sender_decisions SET reason = 'x'",
        "DELETE FROM sms_sender_decisions",
        "TRUNCATE sms_sender_decisions",
    ):
        with eng.connect() as c:
            with pytest.raises(DBAPIError):
                c.execute(text(sql))
            c.rollback()
    for tail in (
        "'rejected', 'rejected', 'bob', NULL",
        "'approved', 'approved', NULL, NULL",
    ):  # refuzim pa arsye · aktor bosh
        with eng.connect() as c:
            with pytest.raises(DBAPIError):
                c.execute(
                    text(
                        "INSERT INTO sms_sender_decisions (sender_id, decided_at, source, decision, to_status, decided_by, reason) "
                        f"VALUES (:s, now(), 'local', {tail})"
                    ),
                    {"s": sid_},
                )
            c.rollback()
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM sms_sender_decisions")).scalar() == 1
