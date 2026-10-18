# ruff: noqa: F811
"""M10-S1 — autoriteti Central i sender-ave: politika e versionuar, regjistri global, vendimet append-only, API/RBAC, garat PG, gatishmëria dhe migrimi 0026."""

import re
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import DBAPIError

from apps.central.core import errors
from apps.central.main import create_app
from apps.central.models import AuditLog
from apps.central.models.sender import (
    CountrySenderPolicy,
    SenderDecision,
    SenderImmutableError,
    SenderRegistry,
)
from apps.central.services import sender_central_readiness as ready
from apps.central.services import sender_identity as ident
from apps.central.services import senders as svc
from apps.central.tools import sender_central_readiness as cli_ready
from tests.test_central import IS_PG, central_alembic, make_db  # noqa: F401
from tests.test_central_auth import PW, auth_secret, bearer, mk, token_for  # noqa: F401
from tests.test_central_products import cdb  # noqa: F401
from tests.test_m9g1_billing import U, b  # noqa: F401

T0 = datetime(2024, 1, 1, tzinfo=UTC)  # në të kaluarën: gatishmëria lexon politikën me orën reale


def at(n=0):
    return T0 + timedelta(minutes=n)


def sess(b):
    return b.F()


def mkreq(b, s, eid=None, ref=None, country="AL", value="Acme", actor=None, **kw):
    return svc.request_sender(
        s,
        actor or U(s, b.a1),
        eid or b.e1,
        ref or f"r-{uuid.uuid4().hex[:10]}",
        country,
        value,
        now=kw.pop("now", at()),
        **kw,
    )


def decisions(b, sid):
    with b.F() as s:
        return [
            (d.seq, d.decision, d.to_status, d.category, d.actor_label, d.reason)
            for d in svc.history(s, sid)
        ]


def is_pg(b):
    return b.eng.dialect.name == "postgresql"


# =============================================================================================================
# identiteti / normalizimi: parity me Enterprise (S0)
# =============================================================================================================

PARITY = [
    "Acme",
    "ACME",
    "acme",
    "aCmE",
    "My Co",
    "+355691234567",
    "355691234567",
    "+0123456",
    "AB",
    "TOOLONGSENDER1",
    "a_b_c",
    " ACME ",
    "12345678901234567890",
    "123abc!",
    "12ab",
    "+3556",
]


@pytest.mark.parametrize("value", PARITY)
def test_central_normalization_is_identical_to_enterprise_s0(value):
    from app.core.errors import DomainError
    from app.services import sender_authorization as ent

    try:
        e = ent.normalize(value)
        ent_res = (e.display, e.kind.value, e.norm)
    except DomainError:
        ent_res = None
    try:
        d, k = ident.classify(value)
        cen_res = (d, k, ident.norm_of(d, k))
    except errors.Invalid:
        cen_res = None
    assert ent_res == cen_res
    assert (
        ident.norm_of(value, "alphanumeric") == ent.norm_of(value)
        or ent_res is None
        or ent_res[1] == "numeric"
    )


def test_central_sources_do_not_import_enterprise_runtime():
    root = Path(__file__).resolve().parents[1] / "apps" / "central"
    for f in (
        "services/senders.py",
        "services/sender_identity.py",
        "services/sender_central_readiness.py",
        "models/sender.py",
        "api/admin_senders.py",
    ):
        src = (root / f).read_text()
        assert not re.search(r"^\s*(from|import)\s+app(\.|\s|$)", src, re.M), f
        assert "SMS_" not in src, f


# =============================================================================================================
# politika: default, versionim, lookup, imutabilitet
# =============================================================================================================


def test_missing_policy_is_default_allowed_and_requires_approval(b):
    with sess(b) as s:
        v = svc.effective_policy(s, "al", "alphanumeric", at())
        assert (v.source, v.allowed, v.requires_approval, v.revision, v.policy_id) == (
            "default",
            True,
            True,
            None,
            None,
        )
        assert (
            s.scalar(select(func.count()).select_from(CountrySenderPolicy)) == 0
        )  # parazgjedhja nuk materializohet


def test_policy_revisions_are_versioned_effective_ranges_are_derived_and_lookup_is_deterministic(b):
    with sess(b) as s:
        a = U(s, b.a1)
        p1 = svc.set_policy(
            s, a, "AL", "alphanumeric", True, False, "open market", now=at(1)
        ).policy
        p2 = svc.set_policy(
            s, a, "AL", "alphanumeric", False, True, "regulator ban", now=at(5)
        ).policy
        p3 = svc.set_policy(s, a, "al", "alphanumeric", True, True, "ban lifted", now=at(9)).policy
        s.commit()
        assert [p.revision for p in (p1, p2, p3)] == [1, 2, 3]
        before = svc.effective_policy(s, "AL", "alphanumeric", at(0))
        assert before.source == "default"
        v1 = svc.effective_policy(s, "AL", "alphanumeric", at(2))
        assert (v1.source, v1.revision, v1.allowed, v1.requires_approval, v1.effective_to) == (
            "explicit",
            1,
            True,
            False,
            at(5),
        )
        assert (
            svc.effective_policy(s, "AL", "alphanumeric", at(5)).revision == 2
        )  # kufiri i brendshëm: revizioni i ri
        assert (
            svc.effective_policy(s, "AL", "alphanumeric", at(100)).revision == 3
            and svc.effective_policy(s, "AL", "alphanumeric", at(100)).effective_to is None
        )
        assert (
            svc.effective_policy(s, "AL", "numeric", at(7)).source == "default"
        )  # shtet+lloj të pavarur
        assert svc.effective_policy(s, "XK", "alphanumeric", at(7)).source == "default"


def test_identical_content_creates_no_new_revision_and_overlap_is_impossible(b):
    with sess(b) as s:
        a = U(s, b.a1)
        p1 = svc.set_policy(s, a, "AL", "alphanumeric", True, False, "x", now=at(1))
        s.commit()  # (SQLite/pysqlite: SAVEPOINT i pari në transaksion e commit-on; PG s'e ka këtë kufizim)
        same = svc.set_policy(s, a, "AL", "alphanumeric", True, False, "again", now=at(2))
        assert same.created is False and same.policy.id == p1.policy.id
        with pytest.raises(
            errors.Conflict
        ):  # revizion jo-rritës në kohë ⇒ asnjë mbivendosje e mundshme
            svc.set_policy(s, a, "AL", "alphanumeric", False, True, "late", now=at(1))
        s.rollback()
        assert s.scalar(select(func.count()).select_from(CountrySenderPolicy)) == 1


def test_denied_policy_must_keep_requires_approval_and_inputs_are_validated(b):
    with sess(b) as s:
        a = U(s, b.a1)
        for args in (
            ("AL", "alphanumeric", False, False, "x"),
            ("ALB", "alphanumeric", True, True, "x"),
            ("AL", "weird", True, True, "x"),
            ("AL", "numeric", 1, True, "x"),
            ("AL", "numeric", True, True, "   "),
        ):
            with pytest.raises(errors.Invalid):
                svc.set_policy(s, a, *args, now=at())
        with pytest.raises(errors.Forbidden):
            svc.set_policy(s, U(s, b.op), "AL", "numeric", True, True, "x", now=at())
        with pytest.raises(errors.Invalid):  # aktor sistemi nuk ndryshon politika
            svc.set_policy(s, "system:sender-policy", "AL", "numeric", True, True, "x", now=at())


def test_policy_and_decision_rows_are_immutable_in_the_orm(b):
    with sess(b) as s:
        a = U(s, b.a1)
        svc.set_policy(s, a, "AL", "alphanumeric", True, False, "x", now=at(1))
        r = mkreq(b, s, now=at(2)).sender
        s.commit()
        for model in (CountrySenderPolicy, SenderDecision):
            row = s.scalar(select(model))
            row.reason = "tamper" if model is CountrySenderPolicy else "tamper"
            with pytest.raises(SenderImmutableError):
                s.flush()
            s.rollback()
            s.delete(s.scalar(select(model)))
            with pytest.raises(SenderImmutableError):
                s.flush()
            s.rollback()
        s.delete(s.get(SenderRegistry, r.id))
        with pytest.raises(SenderImmutableError):
            s.flush()
        s.rollback()


# =============================================================================================================
# regjistri: kërkesë, vendime, makina e gjendjeve, historia
# =============================================================================================================


def test_request_approve_reject_revoke_resubmit_follow_the_state_machine_and_keep_history(b):
    with sess(b) as s:
        a = U(s, b.a1)
        r = mkreq(b, s, value="Acme", now=at(1)).sender
        s.commit()
        assert (r.current_status, r.display_value, r.norm_value, r.sender_kind, r.approved_key) == (
            "pending",
            "Acme",
            "acme",
            "alphanumeric",
            None,
        )
        with pytest.raises(errors.Conflict):
            svc.revoke(s, a, r.id, "x", now=at(2))
        s.rollback()
        r = svc.get_sender(s, r.id)
        svc.approve(s, a, r.id, "TICKET-1", now=at(3))
        s.commit()
        assert r.current_status == "approved" and r.approved_key == "AL:acme"
        with pytest.raises(errors.Conflict):
            svc.approve(s, a, r.id, now=at(4))
        with pytest.raises(errors.Conflict):
            svc.reject(s, a, r.id, "no", now=at(4))
        s.rollback()
        r = svc.get_sender(s, r.id)
        svc.revoke(s, a, r.id, "abuse report", now=at(5))
        assert r.current_status == "revoked" and r.approved_key is None
        svc.resubmit(s, a, r.id, now=at(6))
        svc.reject(s, a, r.id, "brand mismatch", now=at(7))
        svc.resubmit(s, a, r.id, now=at(8))
        s.commit()
        h = svc.history(s, r.id)
        assert [(d.seq, d.decision, d.to_status) for d in h] == [
            (1, "requested", "pending"),
            (2, "approved", "approved"),
            (3, "revoked", "revoked"),
            (4, "resubmitted", "pending"),
            (5, "rejected", "rejected"),
            (6, "resubmitted", "pending"),
        ]
        assert [d.reason for d in h if d.reason] == [
            "abuse report",
            "brand mismatch",
        ]  # arsyet mbijetojnë te resubmit
        assert h[1].evidence_ref == "TICKET-1" and all(
            d.policy_source == "default" and d.policy_id is None and d.policy_revision is None
            for d in h
        )
        assert r.current_decision_id == h[-1].id


def test_reject_and_revoke_require_a_reason_and_bounds_hold(b):
    with sess(b) as s:
        a = U(s, b.a1)
        r = mkreq(b, s).sender
        for bad in (None, "", "   ", "x" * 501, "bad\x00ctrl"):
            with pytest.raises(errors.Invalid):
                svc.reject(s, a, r.id, bad, now=at(2))
        for bad in ("", "E" * 129, "bad\x01"):
            with pytest.raises(errors.Invalid):
                svc.approve(s, a, r.id, bad, now=at(2))
        with pytest.raises(errors.Invalid):
            svc.approve(s, "not-a-label", r.id)
        s.rollback()


def test_casing_parity_with_s0_display_preserved_and_global_uniqueness(b):
    with sess(b) as s:
        a = U(s, b.a1)
        x = mkreq(b, s, b.e1, value="Acme").sender
        y = mkreq(b, s, b.e2, value="ACME").sender
        assert (x.display_value, y.display_value, x.norm_value, y.norm_value) == (
            "Acme",
            "ACME",
            "acme",
            "acme",
        )
        svc.approve(s, a, x.id, now=at(2))
        s.commit()
        with pytest.raises(errors.Conflict) as e:
            svc.approve(s, a, y.id, now=at(3))
        assert "already approved for another account" in str(e.value)
        s.rollback()
        y = svc.get_sender(s, y.id)
        assert y.current_status == "pending" and y.approved_key is None
        assert [d[1] for d in decisions(b, y.id)] == ["requested"]  # asnjë "approved" i rremë


def test_numeric_normalization_country_independence_and_same_enterprise_variants(b):
    with sess(b) as s:
        a = U(s, b.a1)
        n = mkreq(b, s, value="+355691234567").sender
        assert (n.display_value, n.norm_value, n.sender_kind) == (
            "355691234567",
            "355691234567",
            "numeric",
        )
        al, xk = (
            mkreq(b, s, country="AL", value="Brand").sender,
            mkreq(b, s, country="XK", value="Brand").sender,
        )
        svc.approve(s, a, al.id, now=at(2))
        s.commit()
        assert (
            xk.current_status == "pending" and svc.get_sender(s, xk.id).approved_key is None
        )  # shtete të pavarura
        svc.approve(s, a, xk.id, now=at(3))  # i njëjti sender, shtet tjetër ⇒ lejohet
        with pytest.raises(
            errors.Conflict
        ):  # variant rase i të njëjtit enterprise/shtet ⇒ identitet i njëjtë
            mkreq(b, s, country="AL", value="BRAND")


def test_request_idempotency_same_hash_same_row_changed_payload_conflict(b):
    with sess(b) as s:
        r1 = svc.request_sender(s, U(s, b.a1), b.e1, "ext-1", "AL", "Acme", "TICKET-1", now=at(1))
        r2 = svc.request_sender(s, U(s, b.a1), b.e1, "ext-1", "al", "Acme", "TICKET-1", now=at(2))
        s.commit()
        assert r1.created and not r2.created and r1.sender.id == r2.sender.id
        assert len(svc.history(s, r1.sender.id)) == 1
        for kw in (dict(value="Other"), dict(country="XK"), dict(evidence="TICKET-2")):
            with pytest.raises(errors.Conflict):
                svc.request_sender(
                    s,
                    U(s, b.a1),
                    b.e1,
                    "ext-1",
                    kw.get("country", "AL"),
                    kw.get("value", "Acme"),
                    kw.get("evidence", "TICKET-1"),
                    now=at(3),
                )
            s.rollback()
        with pytest.raises(errors.Conflict):  # i njëjti identitet, referencë tjetër
            svc.request_sender(s, U(s, b.a1), b.e1, "ext-2", "AL", "ACME", None, now=at(4))
        s.rollback()
        with pytest.raises(errors.NotFound):
            svc.request_sender(s, U(s, b.a1), uuid.uuid4(), "ext-9", "AL", "Acme", None, now=at(4))
        for ref in ("", "x" * 65, "bad ref", None):
            with pytest.raises(errors.Invalid):
                svc.request_sender(s, U(s, b.a1), b.e1, ref, "AL", "Acme", None, now=at(5))


# =============================================================================================================
# politika në veprim: auto-approve, allowed=false, revokim atomik
# =============================================================================================================


def test_auto_approval_when_policy_says_no_approval_is_recorded_as_a_system_decision(b):
    with sess(b) as s:
        a = U(s, b.a1)
        svc.set_policy(s, a, "AL", "alphanumeric", True, False, "open", now=at(1))
        res = mkreq(b, s, value="Acme", now=at(2))
        s.commit()
        r = res.sender
        assert (
            res.auto == "approved"
            and r.current_status == "approved"
            and r.approved_key == "AL:acme"
        )
        h = svc.history(s, r.id)
        assert [(d.decision, d.actor_label, d.decided_by_id, d.category, d.policy_source, d.policy_revision) for d in h] == [
            ("requested", None, a.id, "request", "explicit", 1), ("approved", "system:sender-policy", None, "policy_auto_approved", "explicit", 1)]  # fmt: skip
        actions = [
            x.action for x in s.scalars(select(AuditLog).where(AuditLog.resource_id == str(r.id)))
        ]
        assert sorted(actions) == ["sender.auto_approve", "sender.request"]


def test_auto_approval_still_respects_global_uniqueness_and_leaves_the_request_pending(b):
    with sess(b) as s:
        a = U(s, b.a1)
        first = mkreq(b, s, b.e1, value="Acme").sender
        svc.approve(s, a, first.id, now=at(1))
        svc.set_policy(s, a, "AL", "alphanumeric", True, False, "open", now=at(2))
        res = mkreq(b, s, b.e2, value="ACME", now=at(3))
        s.commit()
        assert (
            res.auto == "blocked"
            and res.sender.current_status == "pending"
            and res.sender.approved_key is None
        )
        assert [d.decision for d in svc.history(s, res.sender.id)] == ["requested"]


def test_default_policy_never_auto_approves(b):
    with sess(b) as s:
        res = mkreq(b, s)
        assert res.auto == "not_applicable" and res.sender.current_status == "pending"


def test_allowed_false_auto_rejects_requests_blocks_approval_and_resubmit_is_rejected_again(b):
    with sess(b) as s:
        a = U(s, b.a1)
        svc.set_policy(s, a, "AL", "alphanumeric", False, True, "regulator ban", now=at(1))
        res = mkreq(b, s, value="Acme", now=at(2))
        s.commit()
        assert res.auto == "denied" and res.sender.current_status == "rejected"
        h = svc.history(s, res.sender.id)
        assert [(d.decision, d.category, d.actor_label) for d in h] == [
            ("requested", "request", None),
            ("rejected", "policy_denied", "system:sender-policy"),
        ]
        assert h[1].policy_revision == 1 and "policy_denied" in h[1].reason
        again = svc.resubmit(s, a, res.sender.id, now=at(3))
        assert again.auto == "denied" and again.sender.current_status == "rejected"
        # një sender pending (krijuar para ndalimit) nuk mund të miratohet
    with sess(b) as s:
        a = U(s, b.a1)
        svc.set_policy(s, a, "XK", "alphanumeric", True, True, "ok", now=at(1))
        p = mkreq(b, s, country="XK", value="Pend", now=at(2)).sender
        svc.set_policy(s, a, "XK", "alphanumeric", False, True, "ban", now=at(3))
        s.commit()
        with pytest.raises(errors.Conflict) as e:
            svc.approve(s, a, p.id, now=at(4))
        assert "policy denies" in str(e.value)


def test_policy_change_to_disallowed_revokes_every_approved_sender_atomically_with_history_and_audit(
    b,
):
    with sess(b) as s:
        a = U(s, b.a1)
        x, y, z = (
            mkreq(b, s, b.e1, value="Alpha").sender,
            mkreq(b, s, b.e2, value="Beta").sender,
            mkreq(b, s, b.e1, country="XK", value="Alpha").sender,
        )
        n = mkreq(b, s, b.e1, value="+355691234567").sender
        for r in (x, y, z, n):
            svc.approve(s, a, r.id, now=at(2))
        s.commit()
        ch = svc.set_policy(s, a, "AL", "alphanumeric", False, True, "regulator ban", now=at(5))
        s.commit()
        assert ch.created and ch.revoked == 2
        for r, st in (
            (x, "revoked"),
            (y, "revoked"),
            (z, "approved"),
            (n, "approved"),
        ):  # vetëm fusha (AL, alphanumeric)
            s.refresh(r)
            assert r.current_status == st and (r.approved_key is None) == (st == "revoked")
        h = svc.history(s, x.id)
        assert (h[-1].decision, h[-1].category, h[-1].actor_label, h[-1].policy_revision) == (
            "revoked",
            "policy_revoked",
            "system:sender-policy",
            1,
        )
        assert h[1].decision == "approved"  # miratimi i vjetër mbetet në histori, s'u rishkrua
        acts = [
            x.action
            for x in s.scalars(
                select(AuditLog).where(
                    AuditLog.resource_type == "sender", AuditLog.resource_id == str(x.id)
                )
            )
        ]
        assert "sender.policy_revoke" in acts
        # pas ndalimit: rikthimi i lejimit NUK rimiraton automatikisht
        svc.set_policy(s, a, "AL", "alphanumeric", True, True, "lifted", now=at(9))
        s.commit()
        s.refresh(x)
        assert x.current_status == "revoked"


# =============================================================================================================
# audit atomik
# =============================================================================================================


def test_audit_is_minimal_and_atomic_with_the_mutation(b, monkeypatch):
    with sess(b) as s:
        a = U(s, b.a1)
        r = mkreq(b, s, value="Acme").sender
        s.commit()
        svc.approve(s, a, r.id, now=at(2))
        s.commit()
        rows = list(
            s.scalars(
                select(AuditLog).where(
                    AuditLog.resource_id == str(r.id), AuditLog.action == "sender.approve"
                )
            )
        )
        assert len(rows) == 1 and set(rows[0].detail) == {
            "enterprise_id",
            "country",
            "decision_id",
            "policy_revision",
            "policy_source",
            "category",
        }
        assert "Acme" not in str(rows[0].detail)
    with sess(b) as s:
        a = U(s, b.a1)
        q = mkreq(b, s, b.e2, value="Zeta").sender
        qid = q.id
        s.commit()

        def boom(*_a, **_k):
            raise errors.Conflict("audit store down")

        monkeypatch.setattr(svc.audit, "record", boom)
        with pytest.raises(errors.Conflict):
            svc.approve(s, a, qid, now=at(3))
        s.rollback()
        monkeypatch.undo()
    with sess(b) as s:
        q = svc.get_sender(s, qid)
        assert q.current_status == "pending" and q.approved_key is None
        assert [d.decision for d in svc.history(s, qid)] == ["requested"]


# =============================================================================================================
# API / RBAC
# =============================================================================================================


@pytest.fixture
def api(b):
    mk(b.eng, "ro@example.com", role="operator")
    c = TestClient(create_app(b.eng))
    return c, bearer(token_for(c, "a1@example.com")), bearer(token_for(c, "ro@example.com"))


def test_admin_writes_operator_reads_and_operator_writes_get_403(api, b):
    c, ad, ro = api
    r = c.post(
        "/admin/senders",
        json={"enterprise_id": str(b.e1), "country": "AL", "value": "Acme"},
        headers=ad,
    )
    assert (
        r.status_code == 201
        and r.json()["status"] == "pending"
        and r.json()["display_value"] == "Acme"
    )
    sid = r.json()["id"]
    p = c.post(
        "/admin/sender-policies",
        json={
            "country": "XK",
            "sender_kind": "alphanumeric",
            "allowed": True,
            "requires_approval": True,
            "reason": "ok",
        },
        headers=ad,
    )
    assert p.status_code == 201 and p.json()["revision"] == 1 and p.json()["created"] is True
    for path in ("/admin/senders", f"/admin/senders/{sid}", f"/admin/senders/{sid}/history", "/admin/sender-policies", f"/admin/sender-policies/{p.json()['id']}",
                 "/admin/sender-policies/effective?country=AL&sender_kind=alphanumeric"):  # fmt: skip
        assert c.get(path, headers=ro).status_code == 200, path
        assert c.get(path).status_code == 401
    writes = [("/admin/senders", {"enterprise_id": str(b.e1), "country": "AL", "value": "Nope"}), (f"/admin/senders/{sid}/approve", {}),
              (f"/admin/senders/{sid}/reject", {"reason": "x"}), (f"/admin/senders/{sid}/revoke", {"reason": "x"}), (f"/admin/senders/{sid}/resubmit", {}),
              ("/admin/sender-policies", {"country": "AL", "sender_kind": "numeric", "allowed": True, "requires_approval": True, "reason": "x"})]  # fmt: skip
    for path, body in writes:
        assert c.post(path, json=body, headers=ro).status_code == 403, path
    assert (
        c.post(f"/admin/senders/{sid}/approve", json={}, headers=ad).json()["status"] == "approved"
    )
    assert (
        c.get("/admin/senders", params={"status": "approved"}, headers=ro).json()["items"][0]["id"]
        == sid
    )
    assert (
        c.delete(f"/admin/senders/{sid}", headers=ad).status_code == 405
        and c.put(f"/admin/senders/{sid}", json={}, headers=ad).status_code == 405
    )


def test_actor_cannot_be_forged_extra_fields_rejected_and_inputs_validated(api, b):
    c, ad, _ = api
    base = {"enterprise_id": str(b.e1), "country": "AL", "value": "Acme"}
    for extra in (
        {"decided_by": "someone"},
        {"actor": "x"},
        {"decided_by_id": str(uuid.uuid4())},
        {"status": "approved"},
    ):
        assert c.post("/admin/senders", json={**base, **extra}, headers=ad).status_code == 422
    sid = c.post("/admin/senders", json=base, headers=ad).json()["id"]
    for path, body in ((f"/admin/senders/{sid}/approve", {"decided_by": "x"}), (f"/admin/senders/{sid}/approve", {"reason": "x"}), (f"/admin/senders/{sid}/reject", {"reason": "r", "actor": "x"}),
                       ("/admin/sender-policies", {"country": "AL", "sender_kind": "numeric", "allowed": True, "requires_approval": True, "reason": "r", "revision": 9})):  # fmt: skip
        assert c.post(path, json=body, headers=ad).status_code == 422, (path, body)
    for bad in (
        {"country": "ALB"},
        {"country": "A1"},
        {"country": 5},
        {"value": ""},
        {"value": "x" * 17},
        {"enterprise_id": "nope"},
        {"evidence_ref": ""},
        {"evidence_ref": "e" * 129},
        {"external_ref": "bad ref"},
    ):
        assert (
            c.post("/admin/senders", json={**base, "value": "Other", **bad}, headers=ad).status_code
            == 422
        ), bad
    assert (
        c.post("/admin/senders", json={**base, "value": "a_b_c"}, headers=ad).status_code == 422
    )  # S0-compatible rules
    for bad in ("", "x" * 501):
        assert (
            c.post(f"/admin/senders/{sid}/reject", json={"reason": bad}, headers=ad).status_code
            == 422
        )
    assert (
        c.post(
            f"/admin/senders/{sid}/reject",
            json={"reason": "r" * 500, "evidence_ref": "E" * 128},
            headers=ad,
        ).status_code
        == 200
    )
    pol = {
        "country": "AL",
        "sender_kind": "numeric",
        "allowed": True,
        "requires_approval": True,
        "reason": "r",
    }
    for bad in (
        {"sender_kind": "weird"},
        {"allowed": "yes"},
        {"country": "ALB"},
        {"allowed": False, "requires_approval": False},
    ):
        assert (
            c.post("/admin/sender-policies", json={**pol, **bad}, headers=ad).status_code == 422
        ), bad
    assert c.get("/admin/senders/not-a-uuid", headers=ad).status_code == 422
    assert c.get(f"/admin/senders/{uuid.uuid4()}", headers=ad).status_code == 404
    assert (
        c.get("/admin/sender-policies/effective?country=AL&sender_kind=bad", headers=ad).status_code
        == 422
    )


def test_api_conflicts_map_to_409_and_policy_preview_matches_the_service(api, b):
    c, ad, _ = api
    a = c.post(
        "/admin/senders",
        json={"enterprise_id": str(b.e1), "country": "AL", "value": "Acme"},
        headers=ad,
    ).json()["id"]
    d = c.post(
        "/admin/senders",
        json={"enterprise_id": str(b.e2), "country": "AL", "value": "ACME"},
        headers=ad,
    ).json()["id"]
    assert c.post(f"/admin/senders/{a}/approve", json={}, headers=ad).status_code == 200
    assert c.post(f"/admin/senders/{d}/approve", json={}, headers=ad).status_code == 409
    assert c.post(f"/admin/senders/{a}/approve", json={}, headers=ad).status_code == 409
    c.post(
        "/admin/sender-policies",
        json={
            "country": "AL",
            "sender_kind": "alphanumeric",
            "allowed": False,
            "requires_approval": True,
            "reason": "ban",
        },
        headers=ad,
    )
    assert c.get(f"/admin/senders/{a}", headers=ad).json()["status"] == "revoked"
    eff = c.get(
        "/admin/sender-policies/effective?country=al&sender_kind=alphanumeric", headers=ad
    ).json()
    assert (eff["source"], eff["allowed"], eff["revision"]) == ("explicit", False, 1)
    hist = c.get(f"/admin/senders/{a}/history", headers=ad).json()["items"]
    assert [x["decision"] for x in hist] == ["requested", "approved", "revoked"] and hist[-1][
        "actor_label"
    ] == "system:sender-policy"
    lst = c.get("/admin/sender-policies", params={"latest_only": "true"}, headers=ad).json()
    assert [x["revision"] for x in lst["items"]] == [1]


def test_admin_billing_style_surface_has_only_get_and_post(api):
    from apps.central.api import admin_senders

    checked = 0
    for r in admin_senders.router.routes:
        assert r.methods <= {"GET", "POST"}, (r.path, r.methods)
        for p in r.dependant.body_params:
            assert p.field_info.annotation.model_config.get("extra") == "forbid"
            checked += 1
    assert checked >= 6


# =============================================================================================================
# gatishmëria lokale
# =============================================================================================================


def test_readiness_is_clean_on_a_consistent_state_and_detects_corruption(b):
    with sess(b) as s:
        a = U(s, b.a1)
        svc.set_policy(s, a, "AL", "alphanumeric", True, True, "x", now=at(1))
        r = mkreq(b, s, value="Acme", now=at(2)).sender
        svc.approve(s, a, r.id, now=at(3))
        s.commit()
        res = ready.checks(s)
        assert ready.overall(res) == "PASS", [(c.name, c.reason) for c in res if c.level != "PASS"]
        s.rollback()
    if b.eng.dialect.name != "sqlite":
        return  # trigger-at PG e bëjnë të pamundur korruptimin (provuar te testi i migrimit)
    cases = {
        "registry_states_valid": "UPDATE sender_registry SET approved_key = 'AL:wrong'",
        "current_decision_matches_state": "UPDATE sender_registry SET current_decision_id = NULL",
        "no_approved_under_disallowing_policy": None,
    }
    with b.eng.begin() as c:
        c.execute(text(cases["registry_states_valid"]))
    with sess(b) as s:
        lv = {c.name: c for c in ready.checks(s)}
        assert (
            lv["registry_states_valid"].level == "FAIL"
            and ready.overall(list(lv.values())) == "FAIL"
        )
    with b.eng.begin() as c:
        c.execute(text("UPDATE sender_registry SET approved_key = 'AL:acme'"))
        c.execute(text("UPDATE sender_registry SET current_decision_id = NULL"))
    with sess(b) as s:
        assert {c.name: c for c in ready.checks(s)}[
            "current_decision_matches_state"
        ].level == "FAIL"
    with b.eng.begin() as c:
        c.execute(text("DELETE FROM audit_log WHERE resource_type = 'sender'"))
    with sess(b) as s:
        assert {c.name: c for c in ready.checks(s)}["senders_have_audit"].level == "FAIL"


def test_readiness_flags_an_approved_sender_under_a_disallowing_policy(b):
    if b.eng.dialect.name != "sqlite":
        pytest.skip("needs raw writes that PG triggers forbid")
    with sess(b) as s:
        a = U(s, b.a1)
        r = mkreq(b, s, value="Acme", now=at(2)).sender
        svc.approve(s, a, r.id, now=at(3))
        pol = svc.set_policy(s, a, "AL", "alphanumeric", False, True, "ban", now=at(4))
        s.commit()
        assert pol.revoked == 1
    with b.eng.begin() as c:  # korrupt: rikthe si i miratuar pa vendim
        c.execute(
            text("UPDATE sender_registry SET current_status = 'approved', approved_key = 'AL:acme'")
        )
    with sess(b) as s:
        assert {c.name: c for c in ready.checks(s)}[
            "no_approved_under_disallowing_policy"
        ].level == "FAIL"


def test_readiness_cli_json_and_exit_codes(b, capsys):
    import json

    with sess(b) as s:
        mkreq(b, s)
        s.commit()
    assert cli_ready.main(["--json"], engine=b.eng) in (0, 1)
    out = json.loads(capsys.readouterr().out)
    assert out["status"] in ("PASS", "WARN", "FAIL") and "@" not in json.dumps(out)
    assert cli_ready.main([], engine=object()) == 2


# =============================================================================================================
# PostgreSQL: gara/konkurrencë
# =============================================================================================================


def _run(b, fns):
    res = [None] * len(fns)
    gate = threading.Barrier(len(fns))

    def go(i, fn):
        with b.F() as s:
            try:
                gate.wait(timeout=20)
                res[i] = ("ok", fn(s))
                s.commit()
            except errors.Conflict as e:
                s.rollback()
                res[i] = ("conflict", str(e))
            except Exception as e:  # noqa: BLE001
                s.rollback()
                res[i] = ("error", repr(e))

    ts = [threading.Thread(target=go, args=(i, f)) for i, f in enumerate(fns)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return [r[0] for r in res], res


@pytest.mark.skipif(not IS_PG, reason="postgres concurrency")
def test_two_enterprises_approving_the_same_canonical_key_concurrently_yield_one_winner(b):
    if not is_pg(b):
        pytest.skip("postgres parametrization only")
    with sess(b) as s:
        x, y = (
            mkreq(b, s, b.e1, value="Racer").sender.id,
            mkreq(b, s, b.e2, value="RACER").sender.id,
        )
        s.commit()
    kinds, res = _run(
        b, [lambda s: svc.approve(s, U(s, b.a1), x).id, lambda s: svc.approve(s, U(s, b.a2), y).id]
    )
    assert sorted(kinds) == ["conflict", "ok"], res
    with sess(b) as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(SenderRegistry)
                .where(SenderRegistry.approved_key == "AL:racer")
            )
            == 1
        )


@pytest.mark.skipif(not IS_PG, reason="postgres concurrency")
def test_approve_vs_reject_duplicate_approve_and_duplicate_revoke_are_deterministic(b):
    if not is_pg(b):
        pytest.skip("postgres parametrization only")
    with sess(b) as s:
        r = mkreq(b, s, value="Duel").sender.id
        s.commit()
    kinds, _ = _run(
        b,
        [
            lambda s: svc.approve(s, U(s, b.a1), r).id,
            lambda s: svc.reject(s, U(s, b.a2), r, "no").id,
        ],
    )
    assert sorted(kinds) == ["conflict", "ok"]
    with sess(b) as s:
        h = [d.decision for d in svc.history(s, r)]
        assert h in (["requested", "approved"], ["requested", "rejected"]) and svc.get_sender(
            s, r
        ).current_status == h[-1].replace("ed", "ed")
    with sess(b) as s:
        q = mkreq(b, s, b.e2, value="Twice").sender.id
        s.commit()
    kinds, _ = _run(
        b,
        [
            lambda s: svc.approve(s, U(s, b.a1), q).id,
            lambda s: svc.approve(s, U(s, b.a2), q).id,
            lambda s: svc.approve(s, U(s, b.a1), q).id,
        ],
    )
    assert sorted(kinds) == ["conflict", "conflict", "ok"]
    kinds, _ = _run(
        b,
        [
            lambda s: svc.revoke(s, U(s, b.a1), q, "r1").id,
            lambda s: svc.revoke(s, U(s, b.a2), q, "r2").id,
        ],
    )
    assert sorted(kinds) == ["conflict", "ok"]
    with sess(b) as s:
        assert [d.decision for d in svc.history(s, q)] == ["requested", "approved", "revoked"]


@pytest.mark.skipif(not IS_PG, reason="postgres concurrency")
def test_policy_change_vs_approval_never_leaves_an_approved_sender_under_a_disallowing_policy(b):
    if not is_pg(b):
        pytest.skip("postgres parametrization only")
    outcomes = set()
    for i in range(6):
        country = ["AL", "XK", "DE", "FR", "IT", "ES"][i]
        with sess(b) as s:
            sid = mkreq(b, s, b.e1, country=country, value="Gate").sender.id
            s.commit()
        kinds, res = _run(b, [lambda s, sid=sid: svc.approve(s, U(s, b.a1), sid).id,
                              lambda s, country=country: svc.set_policy(s, U(s, b.a2), country, "alphanumeric", False, True, "ban").revoked])  # fmt: skip
        assert "error" not in kinds, res
        with sess(b) as s:
            row = svc.get_sender(s, sid)
            pol = svc.effective_policy(s, country, "alphanumeric")
            assert not pol.allowed
            h = svc.history(s, sid)
            if kinds[0] == "ok":  # miratimi fitoi: politika e re e revokoi atomikisht
                assert row.current_status == "revoked" and [d.decision for d in h] == [
                    "requested",
                    "approved",
                    "revoked",
                ]
                assert h[1].policy_source == "default" and h[2].policy_revision == 1
                outcomes.add("approve_then_revoke")
            else:  # politika fitoi: miratimi u refuzua me politikën e re
                assert row.current_status == "pending" and [d.decision for d in h] == ["requested"]
                outcomes.add("policy_first")
            assert row.approved_key is None
    assert outcomes  # të dyja renditjet janë të vlefshme; asnjë nuk lë gjendje të pamundur


@pytest.mark.skipif(not IS_PG, reason="postgres concurrency")
def test_auto_approval_vs_manual_approval_race_has_exactly_one_approved_key(b):
    if not is_pg(b):
        pytest.skip("postgres parametrization only")
    with sess(b) as s:
        manual = mkreq(b, s, b.e2, value="Auto").sender.id
        svc.set_policy(s, U(s, b.a1), "AL", "alphanumeric", True, False, "open")
        s.commit()
    kinds, res = _run(
        b,
        [
            lambda s: svc.request_sender(s, U(s, b.a1), b.e1, "auto-1", "AL", "AUTO").auto,
            lambda s: svc.approve(s, U(s, b.a2), manual).id,
        ],
    )
    assert "error" not in kinds, res
    with sess(b) as s:
        approved = s.scalars(
            select(SenderRegistry).where(SenderRegistry.approved_key == "AL:auto")
        ).all()
        assert len(approved) == 1
        assert ready.overall(ready.checks(s)) in ("PASS", "WARN")


@pytest.mark.skipif(not IS_PG, reason="postgres concurrency")
def test_parallel_duplicate_requests_are_idempotent_or_clean_conflicts(b):
    if not is_pg(b):
        pytest.skip("postgres parametrization only")
    kinds, res = _run(
        b,
        [
            lambda s: (
                svc.request_sender(s, U(s, b.a1), b.e1, "same-ref", "AL", "Dup", None).sender.id
            )
            for _ in range(6)
        ],
    )
    assert "error" not in kinds, res
    with sess(b) as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(SenderRegistry)
                .where(SenderRegistry.external_ref == "same-ref")
            )
            == 1
        )
        sid = s.scalar(select(SenderRegistry.id).where(SenderRegistry.external_ref == "same-ref"))
        assert [d.decision for d in svc.history(s, sid)] == ["requested"]


# =============================================================================================================
# migrimi 0026
# =============================================================================================================

NEW = {"country_sender_policies", "sender_registry", "sender_decisions"}


def _drift(conn):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from apps.central.core.db import Base

    ctx = MigrationContext.configure(
        conn, opts={"compare_type": True, "version_table": "central_alembic_version"}
    )
    return [
        repr(i)
        for d in compare_metadata(ctx, Base.metadata)
        for i in (d if isinstance(d, list) else [d])
        if any(t in repr(i) for t in NEW) and "sender_request_operations" not in repr(i)
    ]


def test_central_0026_is_additive_reversible_and_matches_metadata(make_db):
    url = make_db("central")
    central_alembic(url, "upgrade", "0025")
    eng = create_engine(url)
    before = {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    }
    assert not NEW & set(before)
    central_alembic(url, "upgrade", "0026")
    assert NEW <= set(inspect(eng).get_table_names())
    for t, cols in before.items():  # asnjë tabelë ekzistuese s'ndryshon
        assert {c["name"] for c in inspect(eng).get_columns(t)} == cols
    with eng.connect() as c:
        assert _drift(c) == []
    central_alembic(url, "downgrade", "0025")
    assert not NEW & set(inspect(eng).get_table_names())
    central_alembic(url, "upgrade", "head")
    assert NEW <= set(inspect(eng).get_table_names())
    with eng.connect() as c:
        assert _drift(c) == []
    eng.dispose()


def test_pg_triggers_protect_history_policies_and_registry_identity_on_a_migrated_database(make_db):
    url = make_db("central")
    if not url.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    uid, eid, rid, pid, did = (uuid.uuid4() for _ in range(5))
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO users (id, email, password_hash, role, status, created_at, updated_at) VALUES (:i, 'u@example.com', 'x', 'admin', 'active', now(), now())"
            ),
            {"i": uid},
        )
        c.execute(
            text(
                "INSERT INTO enterprises (id, name, status, revision, created_at, updated_at) VALUES (:i, 'E', 'active', 1, now(), now())"
            ),
            {"i": eid},
        )
        c.execute(text("INSERT INTO country_sender_policies (id, country, sender_kind, revision, allowed, requires_approval, effective_from, reason, content_hash, created_by_id, created_at) "
                       "VALUES (:p, 'AL', 'alphanumeric', 1, true, true, now(), 'r', 'h', :u, now())"), {"p": pid, "u": uid})  # fmt: skip
        c.execute(text("INSERT INTO sender_registry (id, enterprise_id, external_ref, country, sender_kind, display_value, norm_value, request_hash, current_status, source, created_at, updated_at) "
                       "VALUES (:r, :e, 'x', 'AL', 'alphanumeric', 'Acme', 'acme', 'h', 'pending', 'admin', now(), now())"), {"r": rid, "e": eid})  # fmt: skip
        c.execute(text("INSERT INTO sender_decisions (id, registry_id, seq, decision, to_status, category, decided_at, decided_by_id, policy_source, source, created_at) "
                       "VALUES (:d, :r, 1, 'requested', 'pending', 'request', now(), :u, 'default', 'admin', now())"), {"d": did, "r": rid, "u": uid})  # fmt: skip
    for sql in ("UPDATE country_sender_policies SET reason = 'x'", "DELETE FROM country_sender_policies", "TRUNCATE country_sender_policies",
                "UPDATE sender_decisions SET reason = 'x'", "DELETE FROM sender_decisions", "TRUNCATE sender_decisions",
                "UPDATE sender_registry SET display_value = 'Other'", "UPDATE sender_registry SET enterprise_id = gen_random_uuid()", "DELETE FROM sender_registry", "TRUNCATE sender_registry"):  # fmt: skip
        with eng.connect() as c:
            with pytest.raises(DBAPIError):
                c.execute(text(sql))
            c.rollback()
    with (
        eng.begin() as c
    ):  # projeksioni i gjendjes mbetet i ndryshueshëm (vetëm kolonat e gjendjes)
        c.execute(
            text(
                "UPDATE sender_registry SET current_status = 'approved', approved_key = 'AL:acme', updated_at = now()"
            )
        )
    for sql in ("UPDATE sender_registry SET current_status = 'approved', approved_key = NULL",  # CHECK
                "INSERT INTO sender_decisions (id, registry_id, seq, decision, to_status, category, decided_at, policy_source, source, created_at) VALUES (gen_random_uuid(), :r, 2, 'approved', 'approved', 'manual', now(), 'default', 'admin', now())",
                "INSERT INTO sender_decisions (id, registry_id, seq, decision, to_status, category, decided_at, decided_by_id, policy_source, source, created_at) VALUES (gen_random_uuid(), :r, 3, 'rejected', 'rejected', 'manual', now(), :u, 'default', 'admin', now())"):  # fmt: skip
        with eng.connect() as c:
            with pytest.raises(DBAPIError):
                c.execute(text(sql), {"r": rid, "u": uid})
            c.rollback()
    eng.dispose()
