# ruff: noqa: F811
"""M8-c — E2E me dy DB reale: regjistrim → miratim → provisioning (Central) → auto-grant →
auth_generation → M7 poll/snapshot → tenant autocreate (Enterprise) → entitlement. Pa thirrje direkte."""

import httpx
import pytest
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.db import SessionLocal
from app.models.control_plane import Entitlement
from app.models.enterprise import Enterprise
from app.models.sending import AccountPlan
from app.services import control_plane_client as cc
from app.services import control_plane_poller as poller
from app.services import control_plane_sync as cps
from apps.central.main import create_app
from apps.central.models import EnterpriseProduct, ServiceClient
from apps.central.services import products as prod
from apps.central.services import provisioning as prov
from apps.central.services import registration_policy as pol
from apps.central.services import registrations as reg
from apps.central.services import service_auth, users
from tests.test_central_bootstrap_products import cen, make_db  # noqa: F401
from tests.test_central_sync_api import keypair


@pytest.fixture
def world(cen, monkeypatch):
    monkeypatch.setattr(settings, "cp_tenant_autocreate", True)
    eng = create_engine(cen)
    factory = sessionmaker(bind=eng, expire_on_commit=False)
    private, public = keypair()
    with factory() as s:
        sms = prod.create(s, "sms", "SMS", "sms")
        email = prod.create(s, "email", "Email", "email")
        admin = users.create_user(s, "adm@example.com", "pw-Long-Enough-123", "admin")
        for p in (sms, email):
            pol.set_policy(s, p.id, admin, self_registration_enabled=True)
        service_auth.create_client(s, "ent-main", ["sync:read"], [])
        service_auth.add_key(s, "ent-main", "k1", public)
        service_auth.set_auto_grant(s, "ent-main", True)
        s.commit()
        ids = (sms.id, email.id, admin.id)
    central = TestClient(create_app(eng))
    key = load_pem_private_key(private.encode(), password=None)
    client = cc.ControlPlaneClient(
        cc.ControlPlaneConfig("http://testserver", "ent-main", "k1", key, 5.0), http=central
    )
    return eng, factory, ids, client, central, key


def register(factory, ids, email="ana@example.com", codes=("sms", "email")):
    sms, em, admin_id = ids
    with factory() as s:
        r = reg.submit(
            s, enterprise_name="Acme Ltd", contact_email=email,
            product_ids=[{"sms": sms, "email": em}[c] for c in codes],
        ).request  # fmt: skip
        reg.approve(s, r.id, s.get(users.CentralUser, admin_id))
        s.commit()
        return r.id


def gen(factory):
    with factory() as s:
        return s.scalar(select(ServiceClient.auth_generation))


def poll(client):
    return poller.poll_once(SessionLocal, client, snapshot_interval_s=10**9)


def test_full_central_to_enterprise_chain_with_enterprise_unavailable_then_catch_up(db, world):
    eng, factory, ids, client, central, key = world
    assert poll(client).ok  # snapshot fillestar bosh: kursori ka epokë/generation
    g0 = gen(factory)
    rid = register(factory, ids)

    # Central provisiono ndërsa Enterprise është "i paarritshëm": s'ka asnjë varësi nga ai
    res = prov.run(factory, rid)
    assert res.status == "provisioned" and res.auto_grants == ["ent-main"]
    assert gen(factory) == g0 + 1  # auth_generation ndryshoi saktësisht një herë
    eid = res.enterprise_id
    assert db.get(Enterprise, eid) is None  # Enterprise ende s'ka dëgjuar gjë

    # Enterprise "kthehet": poll-i sheh 409 auth_generation ⇒ snapshot i plotë ⇒ tenant autocreate
    out = poll(client)
    assert out.ok and out.snapshots == 1
    db.expire_all()
    e = db.get(Enterprise, eid)
    assert e is not None and e.owner_ref == f"cp-{eid}" and e.short_name == "Acme Ltd"
    assert e.status == "active" and e.cp_revision >= 1
    assert {
        x.channel for x in db.scalars(select(Entitlement).where(Entitlement.enterprise_id == eid))
    } == {"sms", "email"}
    assert db.scalar(select(func.count()).select_from(AccountPlan)) == 0
    assert cps.get_cursor(db).authorization_generation == g0 + 1

    # retry i provisioning-ut dhe poll-i i përsëritur: asnjë dublikim
    assert prov.run(factory, rid).already_provisioned
    out = poll(client)
    assert out.ok
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Enterprise)) == 1
    assert db.scalar(select(func.count()).select_from(Entitlement)) == 2
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(EnterpriseProduct)) == 2


def test_without_the_autocreate_flag_the_enterprise_stays_unknown_locally(db, world, monkeypatch):
    eng, factory, ids, client, central, key = world
    monkeypatch.setattr(settings, "cp_tenant_autocreate", False)
    assert poll(client).ok
    res = prov.run(factory, register(factory, ids))
    out = poll(client)
    assert out.ok
    assert db.get(Enterprise, res.enterprise_id) is None
    assert db.scalar(select(func.count()).select_from(Entitlement)) == 0


def test_second_registration_arrives_via_feed_or_snapshot_and_creates_second_tenant(db, world):
    eng, factory, ids, client, central, key = world
    assert poll(client).ok
    a = prov.run(factory, register(factory, ids, "a@example.com", ("sms",)))
    assert poll(client).ok
    b = prov.run(factory, register(factory, ids, "b@example.com", ("email",)))
    assert poll(client).ok
    db.expire_all()
    assert {e.id for e in db.scalars(select(Enterprise))} == {a.enterprise_id, b.enterprise_id}
    assert {x.channel for x in db.scalars(select(Entitlement))} == {"sms", "email"}


def test_central_never_talks_to_enterprise_during_provisioning(world, monkeypatch):
    eng, factory, ids, client, central, key = world

    def forbidden(*a, **k):
        raise AssertionError("Central provisioning must not make outbound HTTP calls")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    assert prov.run(factory, register(factory, ids)).status == "provisioned"
