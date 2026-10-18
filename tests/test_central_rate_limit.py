# ruff: noqa: F811
"""M7-g — Central: EnterpriseProduct.rate_limit_per_min (migrim, service, API, outbox, cp.v1, audit)."""

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.central.core import errors
from apps.central.models import AuditLog, EnterpriseProduct, SyncOutbox
from apps.central.services import enterprise_products as svc
from apps.central.services import enterprises as ent_svc
from apps.central.services import products as prod_svc
from apps.central.services import sync_contract
from packages.contracts.control_plane import v1
from tests.test_central import IS_PG, ROOT, central_alembic, make_db  # noqa: F401
from tests.test_central_assignments import url
from tests.test_central_auth import PW, auth_secret, bearer, mk, token_for  # noqa: F401
from tests.test_central_products import api, cdb, db  # noqa: F401  (fixtures)


def world(db):
    e = ent_svc.create(db, "Acme")
    sms = prod_svc.create(db, "sms", "SMS", "sms")
    email = prod_svc.create(db, "email", "Email", "email")
    db.commit()
    return e, sms, email


def events(db, entity_id):
    return list(db.scalars(select(SyncOutbox).where(SyncOutbox.entity_id == entity_id)
                           .order_by(SyncOutbox.seq)))  # fmt: skip


def test_new_assignment_has_null_limit_and_payload_carries_it(db):
    e, sms, _ = world(db)
    ep, _p = svc.assign_product(db, e.id, sms.id)
    db.commit()
    assert ep.rate_limit_per_min is None
    ev = events(db, ep.id)[0]
    assert ev.payload["rate_limit_per_min"] is None
    assert sync_contract.to_event(ev).data.rate_limit_per_min is None


def test_set_limit_bumps_revision_emits_outbox_and_noop_does_not(db):
    e, sms, email = world(db)
    s_ep, _ = svc.assign_product(db, e.id, sms.id)
    e_ep, _ = svc.assign_product(db, e.id, email.id)
    db.commit()
    _, _, ch = svc.set_rate_limit(db, e.id, s_ep.id, 120)
    db.commit()
    assert ch == {"before": {"rate_limit_per_min": None}, "after": {"rate_limit_per_min": 120}}
    assert s_ep.revision == 2 and e_ep.revision == 1  # produkti tjetër i paprekur
    evs = events(db, s_ep.id)
    assert [x.revision for x in evs] == [1, 2] and evs[1].payload["rate_limit_per_min"] == 120
    assert sync_contract.to_event(evs[1]).data.rate_limit_per_min == 120
    n = len(events(db, s_ep.id))
    _, _, ch2 = svc.set_rate_limit(db, e.id, s_ep.id, 120)  # no-op
    db.commit()
    assert ch2 == {} and s_ep.revision == 2 and len(events(db, s_ep.id)) == n
    svc.set_rate_limit(db, e.id, e_ep.id, 30)  # Email: fushë e veçantë për assignment
    svc.set_rate_limit(db, e.id, s_ep.id, None)  # kthim te default lokal
    db.commit()
    assert (e_ep.rate_limit_per_min, s_ep.rate_limit_per_min) == (30, None)
    assert [x.payload["rate_limit_per_min"] for x in events(db, s_ep.id)] == [None, 120, None]
    assert [x.payload["rate_limit_per_min"] for x in events(db, e_ep.id)] == [None, 30]


@pytest.mark.parametrize("bad", [0, -1, 1_000_001, True, "5", 1.5, [1]])
def test_invalid_limits_are_rejected_by_service(db, bad):
    e, sms, _ = world(db)
    ep, _p = svc.assign_product(db, e.id, sms.id)
    db.commit()
    with pytest.raises(errors.Invalid):
        svc.set_rate_limit(db, e.id, ep.id, bad)
    db.rollback()
    assert db.get(EnterpriseProduct, ep.id).rate_limit_per_min is None


def test_database_check_constraint_enforces_the_range(db):
    e, sms, _ = world(db)
    ep, _p = svc.assign_product(db, e.id, sms.id)
    db.commit()
    for bad in (0, -5, 1_000_001):
        with pytest.raises(IntegrityError):
            db.execute(text("update enterprise_products set rate_limit_per_min = :v"), {"v": bad})
            db.flush()
        db.rollback()
    for good in (1, 1_000_000):
        db.execute(text("update enterprise_products set rate_limit_per_min = :v"), {"v": good})
    db.commit()


def test_cp_v1_state_accepts_null_and_range_and_ignores_unknown_fields():
    base = dict(assignment_id=str(uuid.uuid4()), enterprise_id=str(uuid.uuid4()),
                product_id=str(uuid.uuid4()), product_code="sms", channel="sms", status="active")  # fmt: skip
    assert v1.EnterpriseProductStateV1(**base).rate_limit_per_min is None
    assert (
        v1.EnterpriseProductStateV1(**base, rate_limit_per_min=1_000_000).rate_limit_per_min
        == 1_000_000
    )
    d = v1.EnterpriseProductStateV1(**base, rate_limit_per_min=7).to_dict()
    assert d["rate_limit_per_min"] == 7
    old = {k: v for k, v in d.items() if k != "rate_limit_per_min"}  # prodhues i vjetër pa fushën
    assert v1.EnterpriseProductStateV1.from_dict(old).rate_limit_per_min is None
    assert v1.EnterpriseProductStateV1.from_dict({**d, "future_field": 1}).rate_limit_per_min == 7
    for bad in (0, -1, 1_000_001, True, "1"):
        with pytest.raises(v1.ContractError):
            v1.EnterpriseProductStateV1(**base, rate_limit_per_min=bad)


# --- API ---------------------------------------------------------------------------------------------


@pytest.fixture
def seeded(api):
    api.product_id = api.post("/admin/products", json={"code": "sms", "name": "SMS", "channel": "sms"},
                              headers=api.admin).json()["id"]  # fmt: skip
    with Session(api.eng) as s:
        api.enterprise_id = str(ent_svc.create(s, "Acme").id)
        s.commit()
    r = api.post(url(api.enterprise_id), json={"product_id": api.product_id}, headers=api.admin)
    api.assignment_id = r.json()["id"]
    return api


def patch(c, body, **kw):
    return c.patch(url(c.enterprise_id, c.assignment_id), json=body, headers=kw.get("h", c.admin))


def test_api_patch_sets_clears_and_audits_limit_before_after(seeded):
    c = seeded
    assert (
        c.get(url(c.enterprise_id, c.assignment_id), headers=c.admin).json()["rate_limit_per_min"]
        is None
    )
    r = patch(c, {"rate_limit_per_min": 250})
    assert r.status_code == 200 and r.json()["rate_limit_per_min"] == 250
    r = patch(c, {"rate_limit_per_min": 250})  # no-op
    assert r.status_code == 200
    r = patch(c, {"rate_limit_per_min": None})
    assert r.json()["rate_limit_per_min"] is None
    r = patch(c, {"status": "suspended", "rate_limit_per_min": 9})  # të dyja në një kërkesë
    assert r.json()["status"] == "suspended" and r.json()["rate_limit_per_min"] == 9
    with Session(c.eng) as s:
        rows = list(s.scalars(select(AuditLog).where(AuditLog.action == "enterprise_product.update")
                              .order_by(AuditLog.created_at)))  # fmt: skip
        assert len(rows) == 3  # no-op s'audiohet
        assert rows[0].detail["before"] == {"rate_limit_per_min": None}
        assert rows[0].detail["after"] == {"rate_limit_per_min": 250}
        assert rows[2].detail["before"] == {"status": "active", "rate_limit_per_min": None}
        assert rows[2].detail["after"] == {"status": "suspended", "rate_limit_per_min": 9}
        assert {r.actor_kind for r in rows} == {"user"}  # aktor njeri (JWT admin)
        ep = s.scalar(select(EnterpriseProduct))
        assert ep.revision == 5  # 1 + set + clear + (status, limit)


@pytest.mark.parametrize("bad", [0, -1, 1_000_001, True, "5", 1.5])
def test_api_rejects_invalid_limits_and_empty_patches_and_forbids_operator(seeded, bad):
    c = seeded
    assert patch(c, {"rate_limit_per_min": bad}).status_code == 422
    assert patch(c, {}).status_code == 422
    assert patch(c, {"status": None}).status_code == 422
    assert patch(c, {"rate_limit_per_min": 5}, h=c.operator).status_code == 403
    assert (
        c.get(url(c.enterprise_id, c.assignment_id), headers=c.admin).json()["rate_limit_per_min"]
        is None
    )
    with Session(c.eng) as s:
        assert s.scalar(select(EnterpriseProduct)).revision == 1  # asnjë ndryshim i pjesshëm


def test_migration_0012_is_additive_and_downgradable(make_db):
    url_ = make_db("central")
    central_alembic(url_, "upgrade", "0011")
    from sqlalchemy import create_engine, inspect

    eng = create_engine(url_)
    assert "rate_limit_per_min" not in {
        c["name"] for c in inspect(eng).get_columns("enterprise_products")
    }
    central_alembic(url_, "upgrade", "head")
    cols = {c["name"]: c for c in inspect(eng).get_columns("enterprise_products")}
    assert cols["rate_limit_per_min"]["nullable"]
    central_alembic(url_, "downgrade", "0011")
    assert "rate_limit_per_min" not in {
        c["name"] for c in inspect(eng).get_columns("enterprise_products")
    }
    central_alembic(url_, "upgrade", "head")
    eng.dispose()
