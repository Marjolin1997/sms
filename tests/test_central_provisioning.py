# ruff: noqa: F811
"""M8-c — provisioning i regjistrimit të miratuar (Central; pa HTTP, pa thirrje drejt Enterprise)."""

import ast
import threading
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from apps.central.core import errors
from apps.central.models import (
    AuditLog,
    Enterprise,
    EnterpriseProduct,
    RegistrationProduct,
    RegistrationRequest,
    ServiceClient,
    ServiceClientEnterprise,
    SyncOutbox,
)
from apps.central.services import audit as audit_svc  # noqa: F401
from apps.central.services import enterprise_products as asg
from apps.central.services import enterprises as ent_svc
from apps.central.services import products as prod_svc
from apps.central.services import provisioning as prov
from apps.central.services import registration_policy as pol
from apps.central.services import registrations as reg
from apps.central.services import service_auth, users
from tests.test_central import IS_PG, ROOT, make_db  # noqa: F401
from tests.test_central_products import cdb, db  # noqa: F401  (fixtures)
from tests.test_central_sync_outbox import run_threads

T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)
LABEL = "system:registration_provisioning"


@pytest.fixture
def w(cdb):
    url, eng = cdb
    factory = sessionmaker(bind=eng, expire_on_commit=False)
    with factory() as db:
        sms = prod_svc.create(db, "sms", "SMS", "sms")
        email = prod_svc.create(db, "email", "Email", "email")
        admin = users.create_user(db, "adm@example.com", "pw-Long-Enough-123", "admin")
        for p in (sms, email):
            pol.set_policy(db, p.id, admin, self_registration_enabled=True)
        db.commit()
        ids = types_ns(sms=sms.id, email=email.id, admin=admin.id)
    return types_ns(factory=factory, eng=eng, ids=ids, url=url)


def types_ns(**kw):
    from types import SimpleNamespace

    return SimpleNamespace(**kw)


def approved(w, *codes, name="Acme Ltd", email="ana@example.com"):
    """Krijon + miraton një kërkesë (Tx1); kthen id."""
    codes = codes or ("sms",)
    with w.factory() as db:
        admin = db.get(users.CentralUser, w.ids.admin)
        r = reg.submit(
            db, enterprise_name=name, contact_email=email,
            product_ids=[getattr(w.ids, c) for c in codes],
        ).request  # fmt: skip
        reg.approve(db, r.id, admin)
        db.commit()
        return r.id


def count(db, model, *where):
    return db.scalar(select(func.count()).select_from(model).where(*where))


def row(w, rid):
    with w.factory() as db:
        return db.get(RegistrationRequest, rid)


def new_client(w, cid="ent-main", auto=True, disable=False):
    with w.factory() as db:
        service_auth.create_client(db, cid, ["sync:read"], [])
        if auto:
            service_auth.set_auto_grant(db, cid, True)
        if disable:
            service_auth.disable_client(db, cid)
        db.commit()


def gen(w, cid="ent-main"):
    with w.factory() as db:
        return db.scalar(
            select(ServiceClient.auth_generation).where(ServiceClient.client_id == cid)
        )


# --- gjendja dhe parakushtet -------------------------------------------------------------------


def test_provision_approved_creates_enterprise_and_marks_provisioned(w):
    rid = approved(w, "sms", "email")
    res = prov.run(w.factory, rid, now=T0)
    assert (res.status, res.new_enterprise, res.attempts) == ("provisioned", True, 1)
    r = row(w, rid)
    assert (r.status, r.provisioning_status, r.provisioning_error_code) == (
        "approved", "provisioned", None,
    )  # fmt: skip
    assert r.enterprise_id == res.enterprise_id and r.provisioning_attempts == 1
    with w.factory() as db:
        e = db.get(Enterprise, r.enterprise_id)
        assert (e.name, e.status) == ("Acme Ltd", "active")


@pytest.mark.parametrize("state", ["submitted", "rejected"])
def test_submitted_or_rejected_cannot_be_provisioned_and_nothing_changes(w, state):
    with w.factory() as db:
        admin = db.get(users.CentralUser, w.ids.admin)
        r = reg.submit(
            db, enterprise_name="X", contact_email="x@example.com", product_ids=[w.ids.sms]
        ).request
        if state == "rejected":
            reg.reject(db, r.id, admin, "no")
        db.commit()
        rid = r.id
    with pytest.raises(prov.NotProvisionable):
        prov.run(w.factory, rid)
    r = row(w, rid)
    assert (r.provisioning_status, r.provisioning_attempts, r.enterprise_id) == (None, 0, None)
    with w.factory() as db:
        assert (
            count(db, Enterprise) == 0
            and count(db, AuditLog, AuditLog.action == prov.ACTION_PROVISION) == 0
        )


def test_unknown_request_is_not_found_without_side_effects(w):
    with pytest.raises(errors.NotFound):
        prov.run(w.factory, uuid.uuid4())


def test_enterprise_is_created_once_and_name_comes_from_the_registration(w):
    rid = approved(w, "sms", name="  Zeta SH.P.K  ")
    a = prov.run(w.factory, rid)
    b = prov.run(w.factory, rid)
    assert b.already_provisioned and b.enterprise_id == a.enterprise_id
    with w.factory() as db:
        assert count(db, Enterprise) == 1


def test_no_fuzzy_matching_two_same_name_requests_get_two_enterprises(w):
    a = prov.run(w.factory, approved(w, "sms", email="a@example.com"))
    b = prov.run(w.factory, approved(w, "email", email="b@example.com"))
    assert a.enterprise_id != b.enterprise_id


# --- lidhje eksplicite --------------------------------------------------------------------------


def make_enterprise(w, name="Existing"):
    with w.factory() as db:
        e = ent_svc.create(db, name)
        db.commit()
        return e.id


def test_explicit_link_to_existing_enterprise_no_new_enterprise_no_auto_grant(w):
    new_client(w)
    eid = make_enterprise(w)
    rid = approved(w, "sms", "email")
    g0 = gen(w)
    with w.factory() as db:
        admin = db.get(users.CentralUser, w.ids.admin)
    res = prov.run(w.factory, rid, enterprise_id=eid, actor=admin)
    assert (res.enterprise_id, res.new_enterprise, res.auto_grants) == (eid, False, [])
    assert gen(w) == g0  # Enterprise ekzistues: asnjë auto-grant, asnjë bump
    with w.factory() as db:
        assert count(db, Enterprise) == 1
        assert count(db, AuditLog, AuditLog.action == prov.ACTION_LINK) == 1
        assert count(db, EnterpriseProduct, EnterpriseProduct.enterprise_id == eid) == 2


def test_invalid_link_target_is_an_input_error_and_leaves_no_trace(w):
    rid = approved(w, "sms")
    with w.factory() as db:
        admin = db.get(users.CentralUser, w.ids.admin)
    with pytest.raises(errors.NotFound):
        prov.run(w.factory, rid, enterprise_id=uuid.uuid4(), actor=admin)
    with pytest.raises(errors.Invalid):
        prov.run(w.factory, rid, enterprise_id="not-a-uuid", actor=admin)
    with pytest.raises(errors.Invalid):  # lidhja kërkon aktor njeri
        prov.run(w.factory, rid, enterprise_id=make_enterprise(w))
    r = row(w, rid)
    assert (r.provisioning_status, r.provisioning_attempts, r.enterprise_id) == ("pending", 0, None)


def test_enterprise_is_immutable_once_provisioned(w):
    rid = approved(w, "sms")
    prov.run(w.factory, rid)
    other = make_enterprise(w, "Other")
    with w.factory() as db:
        admin = db.get(users.CentralUser, w.ids.admin)
        with pytest.raises(errors.Conflict):
            prov.link_enterprise(db, rid, other, admin)
        db.rollback()
    res = prov.run(
        w.factory, rid, enterprise_id=other, actor=admin
    )  # provisioned ⇒ no-op idempotent
    assert res.already_provisioned and res.enterprise_id != other


# --- assignments --------------------------------------------------------------------------------


def test_active_assignments_created_and_ids_stored_for_every_product(w):
    rid = approved(w, "sms", "email")
    res = prov.run(w.factory, rid)
    with w.factory() as db:
        eps = {e.product_id: e for e in db.scalars(select(EnterpriseProduct))}
        assert {e.status for e in eps.values()} == {"active"} and len(eps) == 2
        links = db.scalars(
            select(RegistrationProduct).where(RegistrationProduct.request_id == rid)
        ).all()
        assert len(links) == 2 and all(link.assignment_id for link in links)
        assert {link.assignment_id for link in links} == {e.id for e in eps.values()}
    assert {a["product_code"] for a in res.assignments} == {"sms", "email"}


def test_existing_active_assignment_is_satisfied_not_duplicated(w):
    eid = make_enterprise(w)
    with w.factory() as db:
        ep, _ = asg.assign_product(db, eid, w.ids.sms)
        db.commit()
        epid = ep.id
    with w.factory() as db:
        admin = db.get(users.CentralUser, w.ids.admin)
    rid = approved(w, "sms", "email")
    res = prov.run(w.factory, rid, enterprise_id=eid, actor=admin)
    assert {a["product_code"]: a["created"] for a in res.assignments} == {
        "sms": False,
        "email": True,
    }
    with w.factory() as db:
        assert count(db, EnterpriseProduct) == 2
        sms_link = db.scalar(
            select(RegistrationProduct).where(RegistrationProduct.product_id == w.ids.sms)
        )
        assert sms_link.assignment_id == epid


def test_existing_suspended_assignment_conflicts_and_is_never_silently_activated(w):
    eid = make_enterprise(w)
    with w.factory() as db:
        ep, _ = asg.assign_product(db, eid, w.ids.sms)
        asg.suspend_assignment(db, eid, ep.id)
        db.commit()
        epid = ep.id
        admin = db.get(users.CentralUser, w.ids.admin)
    rid = approved(w, "sms", "email")
    res = prov.run(w.factory, rid, enterprise_id=eid, actor=admin)
    assert (res.status, res.error_code) == ("failed", "assignment_exists_suspended")
    with w.factory() as db:
        assert db.get(EnterpriseProduct, epid).status == "suspended"
        assert count(db, EnterpriseProduct) == 1  # email NUK u krijua (all-or-nothing)
    assert (
        row(w, rid).enterprise_id is None
    )  # lidhja është pjesë e Tx2: rikthehet; retry e ridërgon


# --- status, attempts, retry ---------------------------------------------------------------------


def test_attempts_count_each_started_attempt_exactly_once(w):
    rid = approved(w, "sms")
    with w.factory() as db:  # bllokim: politika e çaktivizuar
        admin = db.get(users.CentralUser, w.ids.admin)
        pol.set_policy(db, w.ids.sms, admin, self_registration_enabled=False)
        db.commit()
    assert prov.run(w.factory, rid).attempts == 1
    assert prov.run(w.factory, rid).attempts == 2
    with w.factory() as db:
        pol.set_policy(db, w.ids.sms, admin, self_registration_enabled=True)
        db.commit()
    ok = prov.run(w.factory, rid)
    assert (ok.status, ok.attempts) == ("provisioned", 3)
    again = prov.run(w.factory, rid)  # no-op: nuk numërohet
    assert (again.already_provisioned, again.attempts) == (True, 3)
    assert row(w, rid).provisioning_attempts == 3


def test_retired_product_blocks_provisioning_and_request_stays_approved(w):
    rid = approved(w, "sms", "email")
    with w.factory() as db:
        prod_svc.update(db, w.ids.email, status="retired")
        db.commit()
    res = prov.run(w.factory, rid)
    assert (res.status, res.error_code) == ("failed", "product_retired")
    r = row(w, rid)
    assert (r.status, r.provisioning_status, r.provisioning_error_code) == (
        "approved", "failed", "product_retired",
    )  # fmt: skip
    with w.factory() as db:
        assert count(db, Enterprise) == 0 and count(db, EnterpriseProduct) == 0


def test_disabled_policy_blocks_provisioning(w):
    rid = approved(w, "sms")
    with w.factory() as db:
        admin = db.get(users.CentralUser, w.ids.admin)
        pol.set_policy(db, w.ids.sms, admin, self_registration_enabled=False)
        db.commit()
    res = prov.run(w.factory, rid)
    assert (res.status, res.error_code) == ("failed", "policy_disabled")
    assert row(w, rid).status == "approved"


def test_retry_from_failed_succeeds_and_clears_the_error_code(w):
    rid = approved(w, "sms")
    with w.factory() as db:
        prod_svc.update(db, w.ids.sms, status="retired")
        db.commit()
    assert prov.run(w.factory, rid).error_code == "product_retired"
    with w.factory() as db:
        prod_svc.update(db, w.ids.sms, status="active")
        db.commit()
    res = prov.run(w.factory, rid)
    r = row(w, rid)
    assert (res.status, r.provisioning_status, r.provisioning_error_code) == (
        "provisioned", "provisioned", None,
    )  # fmt: skip
    assert r.provisioning_attempts == 2


def test_failure_audit_is_a_stable_code_only_never_raw_exception_text(w, monkeypatch):
    rid = approved(w, "sms")

    def boom(*a, **k):
        raise RuntimeError("password=hunter2 host=db.internal")

    monkeypatch.setattr(ent_svc, "create", boom)
    res = prov.run(w.factory, rid)
    assert res.error_code == "unexpected_error"
    with w.factory() as db:
        a = db.scalar(select(AuditLog).where(AuditLog.action == prov.ACTION_FAILED))
        assert a.detail == {"error_code": "unexpected_error", "attempt": 1}
        assert "hunter2" not in repr(a.detail) and "hunter2" not in (
            row(w, rid).provisioning_error_code or ""
        )


# --- audit sistemi + outbox ------------------------------------------------------------------------


def test_success_writes_one_system_audit_row_and_no_fake_user(w):
    new_client(w)
    rid = approved(w, "sms", "email")
    res = prov.run(w.factory, rid, now=T0)
    with w.factory() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.action == prov.ACTION_PROVISION)).all()
        assert len(rows) == 1
        a = rows[0]
        assert (a.actor_kind, a.actor_label, a.actor_id) == ("system", LABEL, None)
        assert (a.resource_type, a.resource_id) == ("registration_request", str(rid))
        assert (
            a.detail["enterprise_id"] == str(res.enterprise_id)
            and a.detail["new_enterprise"] is True
        )
        assert a.detail["auto_grants"] == ["ent-main"] and len(a.detail["assignments"]) == 2
        assert count(db, users.CentralUser) == 1  # vetëm admini


def test_normal_outbox_events_are_emitted_for_enterprise_and_assignments(w):
    rid = approved(w, "sms", "email")
    with w.factory() as db:
        before = count(db, SyncOutbox)
    res = prov.run(w.factory, rid)
    with w.factory() as db:
        evs = db.scalars(
            select(SyncOutbox).where(SyncOutbox.enterprise_id == res.enterprise_id)
        ).all()
        assert count(db, SyncOutbox) - before == 3
        assert sorted(e.entity_type for e in evs) == [
            "enterprise",
            "enterprise_product",
            "enterprise_product",
        ]


# --- auto-grant -------------------------------------------------------------------------------------


def test_flag_false_means_no_grant_and_no_bump(w):
    new_client(w, auto=False)
    g0 = gen(w)
    prov.run(w.factory, approved(w, "sms"))
    with w.factory() as db:
        assert count(db, ServiceClientEnterprise) == 0
    assert gen(w) == g0


def test_flag_true_grants_new_enterprise_and_bumps_auth_generation_once_per_client(w):
    new_client(w, "a-client")
    new_client(w, "b-client")
    new_client(w, "off-client", auto=False)
    ga, gb, go = gen(w, "a-client"), gen(w, "b-client"), gen(w, "off-client")
    res = prov.run(w.factory, approved(w, "sms", "email"))  # 2 produkte ⇒ prapë NJË bump
    assert res.auto_grants == ["a-client", "b-client"]
    assert (gen(w, "a-client"), gen(w, "b-client"), gen(w, "off-client")) == (ga + 1, gb + 1, go)
    with w.factory() as db:
        assert {g.enterprise_id for g in db.scalars(select(ServiceClientEnterprise))} == {
            res.enterprise_id
        }
        assert count(db, ServiceClientEnterprise) == 2


def test_disabled_client_is_not_granted(w):
    new_client(w, "dead", disable=True)
    g0 = gen(w, "dead")
    res = prov.run(w.factory, approved(w, "sms"))
    assert res.status == "provisioned" and res.auto_grants == []
    assert gen(w, "dead") == g0


def test_no_service_clients_still_provisions(w):
    res = prov.run(w.factory, approved(w, "sms"))
    assert (res.status, res.auto_grants) == ("provisioned", [])


def test_enabling_the_flag_is_not_retroactive_and_does_not_bump(w):
    prov.run(w.factory, approved(w, "sms"))
    with w.factory() as db:
        service_auth.create_client(db, "late", ["sync:read"], [])
        g0 = db.scalar(select(ServiceClient.auth_generation))
        assert service_auth.set_auto_grant(db, "late", True) is True
        assert service_auth.set_auto_grant(db, "late", True) is False
        db.commit()
        assert count(db, ServiceClientEnterprise) == 0
        assert db.scalar(select(ServiceClient.auth_generation)) == g0
        with pytest.raises(errors.Invalid):
            service_auth.set_auto_grant(db, "late", "yes")


def test_retry_after_success_does_not_grant_or_bump_again(w):
    new_client(w)
    rid = approved(w, "sms")
    prov.run(w.factory, rid)
    g = gen(w)
    prov.run(w.factory, rid)
    assert gen(w) == g
    with w.factory() as db:
        assert count(db, ServiceClientEnterprise) == 1


# --- injektim dështimesh: Tx2 rikthehet plotësisht ------------------------------------------------------


def _snapshot(w):
    with w.factory() as db:
        return {
            "ent": count(db, Enterprise), "ep": count(db, EnterpriseProduct),
            "grant": count(db, ServiceClientEnterprise), "outbox": count(db, SyncOutbox),
            "prov_audit": count(db, AuditLog, AuditLog.action == prov.ACTION_PROVISION),
            "links": count(db, RegistrationProduct, RegistrationProduct.assignment_id.is_not(None)),
        }  # fmt: skip


@pytest.mark.parametrize(
    "point",
    ["after_enterprise", "after_auto_grant", "second_assignment", "enterprise_outbox", "audit"],
)
def test_failure_injection_rolls_back_tx2_records_failed_and_retry_succeeds(w, monkeypatch, point):
    new_client(w)
    rid = approved(w, "sms", "email")
    g0 = gen(w)
    base = _snapshot(w)
    mp = monkeypatch
    calls = {"n": 0}
    orig_assign = asg.assign_product

    def assign(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected")
        return orig_assign(*a, **k)

    def boom(*a, **k):
        raise RuntimeError("injected")

    if point == "after_enterprise":
        mp.setattr(service_auth, "auto_grant_new_enterprise", boom)
    elif point == "after_auto_grant":
        mp.setattr(asg, "assign_product", boom)
    elif point == "second_assignment":
        mp.setattr(asg, "assign_product", assign)
    elif point == "enterprise_outbox":
        mp.setattr(ent_svc, "_emit", boom)
    else:
        mp.setattr(
            prov.audit,
            "record_system",
            lambda db, **k: boom() if k["action"] == prov.ACTION_PROVISION else None,
        )
    res = prov.run(w.factory, rid)
    assert (res.status, res.error_code, res.attempts) == ("failed", "unexpected_error", 1)
    after = _snapshot(w)
    assert after == base  # Tx2 u rikthye plotësisht
    assert gen(w) == g0
    r = row(w, rid)
    assert (r.status, r.provisioning_status, r.enterprise_id) == ("approved", "failed", None)
    mp.undo()
    ok = prov.run(w.factory, rid)
    assert (ok.status, ok.attempts, ok.new_enterprise) == ("provisioned", 2, True)
    s = _snapshot(w)
    assert (s["ent"], s["ep"], s["grant"], s["prov_audit"]) == (1, 2, 1, 1) and gen(w) == g0 + 1


def test_database_error_is_mapped_to_a_stable_code(w, monkeypatch):
    def boom(*a, **k):
        raise OperationalError("select 1", {}, Exception("secret dsn postgres://u:p@h/db"))

    monkeypatch.setattr(ent_svc, "create", boom)
    res = prov.run(w.factory, approved(w, "sms"))
    assert res.error_code == "database_error"
    assert "secret" not in (row(w, res.request_id).provisioning_error_code or "")


def test_error_code_vocabulary_is_closed(w):
    assert prov.ERROR_CODES >= {
        "product_retired", "policy_disabled", "enterprise_not_found", "enterprise_suspended",
        "assignment_exists_suspended", "assignment_conflict", "provisioning_conflict", "database_error",
    }  # fmt: skip
    rid = approved(w, "sms")
    with w.factory() as db:
        res = prov.record_failure(db, rid, "raw text with secrets")
        db.commit()
    assert res.error_code == "unexpected_error"


def test_suspended_explicit_enterprise_fails_with_enterprise_suspended(w):
    eid = make_enterprise(w)
    with w.factory() as db:
        ent_svc.suspend(db, eid)
        admin = db.get(users.CentralUser, w.ids.admin)
        db.commit()
    res = prov.run(w.factory, approved(w, "sms"), enterprise_id=eid, actor=admin)
    assert res.error_code == "enterprise_suspended"


# --- gara (vetëm PG) ---------------------------------------------------------------------------------------


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_two_simultaneous_provisions_produce_one_of_everything(w):
    if w.url.startswith("sqlite"):
        pytest.skip("needs PostgreSQL row locks")
    new_client(w)
    rid = approved(w, "sms", "email")
    g0 = gen(w)
    barrier = threading.Barrier(2, timeout=20)
    out = []

    def worker():
        barrier.wait()
        out.append(prov.run(w.factory, rid))

    run_threads([worker, worker])
    assert sorted(r.already_provisioned for r in out) == [False, True]
    s = _snapshot(w)
    assert (s["ent"], s["ep"], s["grant"], s["prov_audit"], s["links"]) == (1, 2, 1, 1, 2)
    assert gen(w) == g0 + 1 and row(w, rid).provisioning_attempts == 1


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_product_retired_after_a_stale_read_is_seen_as_product_retired(w):
    if w.url.startswith("sqlite"):
        pytest.skip("needs PostgreSQL row locks")
    rid = approved(w, "sms")
    stale = w.factory()
    stale.get(users.CentralUser, w.ids.admin)
    from apps.central.models import Product

    assert stale.get(Product, w.ids.sms).status == "active"  # kopje e vjetër në identity map
    with w.factory() as other:
        prod_svc.update(other, w.ids.sms, status="retired")
        other.commit()
    with pytest.raises(prov.ProvisioningError) as ex:
        prov.provision(stale, rid)
    stale.rollback()
    stale.close()
    assert ex.value.code == "product_retired"


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_policy_disable_waits_for_inflight_provisioning_then_applies(w):
    if w.url.startswith("sqlite"):
        pytest.skip("needs PostgreSQL row locks")
    rid = approved(w, "sms")
    inside, release, done = threading.Event(), threading.Event(), threading.Event()
    orig = asg.assign_product

    def slow(*a, **k):
        inside.set()
        assert release.wait(20)
        return orig(*a, **k)

    res = []

    def provision():
        res.append(prov.run(w.factory, rid))

    def disable():
        assert inside.wait(20)
        with w.factory() as db:
            admin = db.get(users.CentralUser, w.ids.admin)
            pol.set_policy(db, w.ids.sms, admin, self_registration_enabled=False)
            db.commit()
        done.set()

    mp = pytest.MonkeyPatch()
    mp.setattr(asg, "assign_product", slow)
    try:
        t1, t2 = threading.Thread(target=provision), threading.Thread(target=disable)
        t1.start(), t2.start()
        assert inside.wait(20)
        assert not done.wait(1.0)  # disable është i bllokuar nga FOR SHARE i provisioning-ut
        release.set()
        t1.join(20), t2.join(20)
    finally:
        mp.undo()
    assert res[0].status == "provisioned" and done.is_set()
    assert prov.run(w.factory, rid).already_provisioned


# --- reconcile CLI -------------------------------------------------------------------------------------------------


def test_reconcile_dry_run_changes_nothing_and_apply_retries_only_pending_or_failed(w, capsys):
    from apps.central.tools import reconcile_registrations as rr

    r_ok = approved(w, "sms", email="1@example.com")
    prov.run(w.factory, r_ok)
    r_pending = approved(w, "sms", email="2@example.com")
    r_failed = approved(w, "email", email="3@example.com")
    with w.factory() as db:
        prod_svc.update(db, w.ids.email, status="retired")
        db.commit()
    prov.run(w.factory, r_failed)
    with w.factory() as db:
        prod_svc.update(db, w.ids.email, status="active")
        db.commit()
    before = _snapshot(w)
    items = rr.reconcile(w.factory)
    assert {i["request_id"] for i in items} == {str(r_pending), str(r_failed)} and _snapshot(
        w
    ) == before
    assert row(w, r_failed).provisioning_attempts == 1
    out = rr.reconcile(w.factory, apply=True)
    assert {i["result"] for i in out} == {"provisioned"}
    assert rr.reconcile(w.factory) == []
    assert row(w, r_failed).provisioning_attempts == 2


# --- kufijtë -------------------------------------------------------------------------------------------------------


def test_central_provisioning_has_no_http_enterprise_or_direct_db_coupling():
    forbidden = {"httpx", "requests", "urllib", "aiohttp", "socket", "app"}
    for f in ("services/provisioning.py", "tools/reconcile_registrations.py"):
        tree = ast.parse((ROOT / "apps/central" / f).read_text())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                mods.add(n.module.split(".")[0])
        assert not mods & forbidden, (f, mods & forbidden)
    src = (ROOT / "apps/central/services/provisioning.py").read_text().lower()
    assert not any(
        w_ in src for w_ in ("price", "pricing", "sender", "country", "balance", "wallet")
    )


def test_only_the_admin_provision_route_exists_and_it_is_not_public(w):
    from apps.central.main import create_app

    paths = create_app(w.eng).openapi()["paths"]
    prov_paths = [p for p in paths if "provision" in p.lower()]
    assert prov_paths and all(p.startswith("/admin/") for p in prov_paths)  # M8-d: vetëm admin
