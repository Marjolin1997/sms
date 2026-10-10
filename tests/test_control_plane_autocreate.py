"""M8-c: autocreate i tenant-it lokal nga gjendja Enterprise e Central (vetëm përmes aplikuesit M7)."""

import ast
import threading
import uuid

import pytest
from sqlalchemy import func, select

from app.core.config import Settings, settings
from app.core.db import Base, SessionLocal
from app.models.admin import AuditLog
from app.models.control_plane import Entitlement
from app.models.enterprise import Enterprise
from app.models.sending import AccountPlan
from app.services import control_plane_sync as cps
from packages.contracts.control_plane.v1 import ContractError
from tests.test_control_plane_applier import (
    ROOT,
    asg_item,
    do_snapshot,
    ent_item,
    ev_asg,
    ev_ent,
    feed,
    mk_ent,
)


@pytest.fixture
def auto(monkeypatch):
    monkeypatch.setattr(settings, "cp_tenant_autocreate", True)


def ents(db):
    return list(db.scalars(select(Enterprise)))


def snap_new(db, eid, rev=4, name="Acme Central", status="active", asgs=()):
    return do_snapshot(db, ents=[ent_item(eid, rev, name=name, status=status)], asgs=asgs)


def test_default_is_false_and_independent_from_sync_mode():
    assert Settings.model_fields["cp_tenant_autocreate"].default is False
    assert Settings(_env_file=None, cp_sync_mode="off").cp_tenant_autocreate is False
    s = Settings(_env_file=None, cp_sync_mode="enforce", cp_enforce_readiness_ack=True)
    assert s.cp_tenant_autocreate is False  # enforce nuk e nënkupton


def test_flag_false_keeps_skipping_unknown_enterprise(db):
    eid = uuid.uuid4()
    r = snap_new(db, eid)
    assert r.skipped_unknown_enterprise == 1 and ents(db) == []


def test_flag_true_creates_shell_with_exact_uuid_deterministic_owner_ref_and_mapping(db, auto):
    eid = uuid.uuid4()
    snap_new(db, eid, rev=7, name="Acme Central", status="suspended")
    (e,) = ents(db)
    assert e.id == eid and e.owner_ref == f"cp-{eid}" and len(e.owner_ref) <= 64
    assert (e.short_name, e.status, e.cp_revision) == ("Acme Central", "suspended", 7)
    assert e.legal_name is None and e.external_id is None  # legal_name e pa prekur
    a = db.scalar(select(AuditLog).where(AuditLog.action == cps.ACTION_AUTOCREATE))
    assert a.actor == cps.SYSTEM_ACTOR and f"cp-{eid}" in a.detail


def test_autocreate_creates_no_plan_wallet_keys_users_or_pricing(db, auto):
    snap_new(db, uuid.uuid4())
    tenant_tables = [
        t for t in Base.metadata.sorted_tables if "owner_ref" in t.c and t.name != "sms_enterprises"
    ]
    assert tenant_tables
    for t in tenant_tables:
        assert db.scalar(select(func.count()).select_from(t)) == 0, t.name
    assert db.scalar(select(func.count()).select_from(AccountPlan)) == 0


def test_assignment_after_enterprise_creates_the_entitlement(db, auto):
    eid, aid = uuid.uuid4(), uuid.uuid4()
    snap_new(db, eid, asgs=[asg_item(eid, aid, 2)])
    assert db.scalar(select(Entitlement.assignment_id)) == aid
    eid2, aid2 = uuid.uuid4(), uuid.uuid4()
    feed_ = [ev_ent(eid2, 12, 1), ev_asg(eid2, aid2, 13, 1)]
    cps.apply_feed_batch(db, epoch=cps.get_cursor(db).epoch, authorization_generation=1,
                         events=feed_, next_seq=13)  # fmt: skip
    db.commit()
    assert {x.enterprise_id for x in db.scalars(select(Entitlement))} == {eid, eid2}


def test_assignment_event_before_enterprise_event_never_creates_a_tenant(db, auto):
    eid, aid = uuid.uuid4(), uuid.uuid4()
    do_snapshot(db)
    r = feed(db, [ev_asg(eid, aid, 11, 1)], 11)
    assert r.skipped_unknown_enterprise == 1 and ents(db) == []
    assert db.scalar(select(func.count()).select_from(Entitlement)) == 0
    feed(
        db, [ev_ent(eid, 12, 1)], 12
    )  # tani vjen Enterprise: krijohet, por entitlement-i vjen me snapshot
    assert [e.id for e in ents(db)] == [eid]


def test_partial_assignment_only_snapshot_does_not_create_a_tenant(db, auto):
    eid, aid = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(
        ContractError
    ):  # kontrata refuzon assignment jashtë enterprise-eve të snapshot-it
        do_snapshot(db, asgs=[asg_item(eid, aid, 1)])
    assert ents(db) == [] and db.scalar(select(func.count()).select_from(Entitlement)) == 0


def test_owner_ref_collision_with_a_different_id_fails_safely_without_overwrite(db, auto):
    eid = uuid.uuid4()
    other = mk_ent(db, owner=f"cp-{eid}")  # id tjetër, i njëjti owner_ref
    with pytest.raises(cps.ApplyError, match="owner_ref"):
        snap_new(db, eid)
    db.rollback()
    (e,) = ents(db)
    assert e.id == other.id and e.owner_ref == f"cp-{eid}" and e.short_name == "old"
    assert cps.get_cursor(db).epoch is None  # snapshot-i s'u aplikua pjesërisht


def test_owner_ref_collision_is_case_insensitive(db, auto):
    eid = uuid.uuid4()
    mk_ent(db, owner=f"CP-{eid}".upper())
    with pytest.raises(cps.ApplyError):
        snap_new(db, eid)


def test_replay_is_idempotent_and_does_not_duplicate_or_rewrite(db, auto):
    eid = uuid.uuid4()
    snap_new(db, eid, rev=5)
    n_audit = db.scalar(select(func.count()).select_from(AuditLog))
    r = snap_new(db, eid, rev=5)
    assert len(ents(db)) == 1 and r.enterprises_unchanged == 1
    assert db.scalar(select(func.count()).select_from(AuditLog)) >= n_audit
    r2 = feed(db, [ev_ent(eid, 11, 5)], 11)
    assert r2.noop == 1 and len(ents(db)) == 1


def test_existing_local_enterprise_is_updated_not_recreated_even_with_flag_on(db, auto):
    e = mk_ent(db, owner="legacy-owner")
    snap_new(db, e.id, rev=2, name="Central Name")
    (row,) = ents(db)
    assert (
        row.owner_ref == "legacy-owner"
        and row.short_name == "Central Name"
        and row.legal_name == "local-legal"
    )


def test_autocreate_is_only_reachable_from_the_m7_applier():
    callers = []
    for p in (ROOT / "app").rglob("*.py"):
        src = p.read_text()
        if "_autocreate_enterprise" in src or "cp_tenant_autocreate" in src:
            callers.append(str(p.relative_to(ROOT)))
    assert sorted(callers) == ["app/core/config.py", "app/services/control_plane_sync.py"]
    tree = ast.parse((ROOT / "app/services/control_plane_sync.py").read_text())
    users = [
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and any(isinstance(c, ast.Name) and c.id == "_autocreate_enterprise" for c in ast.walk(n))
    ]
    assert users == ["_apply_enterprise"]  # i vetmi thirrës


@pytest.mark.skipif(
    not __import__("os").environ.get("SMS_TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="needs PostgreSQL",
)
def test_concurrent_duplicate_events_create_exactly_one_row(db, auto):
    do_snapshot(db)
    eid = uuid.uuid4()
    barrier = threading.Barrier(2, timeout=20)
    results, errs = [], []

    def worker():
        try:
            with SessionLocal() as s:
                barrier.wait()
                results.append(cps.apply_event(s, ev_ent(eid, 11, 3)))
                s.commit()
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert not errs, errs
    assert sorted(results) == ["applied", "noop"]
    db.expire_all()
    assert len(ents(db)) == 1
