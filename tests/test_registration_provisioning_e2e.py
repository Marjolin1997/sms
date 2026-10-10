# ruff: noqa: F811
"""M8-c — E2E me dy DB reale: regjistrim → miratim → provisioning (Central) → auto-grant →
auth_generation → M7 poll/snapshot → tenant autocreate (Enterprise) → entitlement. Pa thirrje direkte."""

import uuid

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
from apps.central.core.config import settings as central_settings
from apps.central.main import create_app
from apps.central.models import EnterpriseProduct, ServiceClient
from apps.central.services import products as prod
from apps.central.services import provisioning as prov
from apps.central.services import registration_policy as pol
from apps.central.services import registrations as reg
from apps.central.services import service_auth, users
from tests.test_central_auth import auth_secret, bearer, token_for  # noqa: F401
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


def test_http_public_submit_admin_approve_provision_then_m7_creates_the_tenant(
    db, world, auth_secret, monkeypatch
):
    """M8-d: HTTP → Central (dy hapa admin) → outbox → poll M7 → tenant + entitlement lokal."""
    eng, factory, ids, client, central, key = world
    monkeypatch.setattr(central_settings, "public_registration_enabled", True)
    assert poll(client).ok
    g0 = gen(factory)
    admin = {**bearer(token_for(central, "adm@example.com", "pw-Long-Enough-123"))}
    sub = central.post(
        "/registration",
        json={"contact_email": "ana@example.com", "enterprise_name": "Acme Ltd",
              "product_ids": [str(ids[0]), str(ids[1])]},
        headers={"Idempotency-Key": "e2e-key-0001"},
    )  # fmt: skip
    assert sub.status_code == 202
    rid, tok = sub.json()["id"], sub.json()["access_token"]

    def st():
        r = central.get(f"/registration/{rid}/status", headers={"X-Registration-Token": tok})
        return r.json()["status"]

    assert st() == "in_review"
    assert central.post(f"/admin/registrations/{rid}/approve", headers=admin).status_code == 200
    assert st() == "activating"
    out = poll(client)  # asnjë ndryshim i dukshëm për Enterprise ende
    assert out.ok
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Enterprise)) == 0
    pr = central.post(f"/admin/registrations/{rid}/provision", headers=admin)
    assert pr.status_code == 200 and pr.json()["result"] == "provisioned"
    assert st() == "active"  # active = Central provisioned (propagimi M7 është asinkron)
    eid = pr.json()["enterprise_id"]
    assert gen(factory) == g0 + 1  # auto-grant
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Enterprise)) == 0  # ende s'ka konsumuar
    assert poll(client).ok
    db.expire_all()
    e = db.get(Enterprise, uuid.UUID(eid))
    assert e is not None and e.owner_ref == f"cp-{eid}" and e.short_name == "Acme Ltd"
    assert {x.channel for x in db.scalars(select(Entitlement))} == {"sms", "email"}
    # HTTP nuk prodhon gjendje biznesi të dyfishtë
    assert central.post(f"/admin/registrations/{rid}/provision", headers=admin).json()[
        "already_provisioned"
    ]
    assert central.post("/registration", json={"contact_email": "ana@example.com",
        "enterprise_name": "Acme Ltd", "product_ids": [str(ids[0]), str(ids[1])]},
        headers={"Idempotency-Key": "e2e-key-0001"}).json()["token_issued"] is False  # fmt: skip
    assert poll(client).ok
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Enterprise)) == 1
    assert db.scalar(select(func.count()).select_from(Entitlement)) == 2
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(EnterpriseProduct)) == 2


def test_verified_automatic_registration_flows_to_the_enterprise_via_m7(
    db, world, auth_secret, monkeypatch
):
    """M8-e: submit → email me token → verify → auto-miratim (sistemi) → provision → M7 → tenant."""
    from apps.central.core.config import settings as cs
    from apps.central.services import mailer, notifications

    eng, factory, ids, client, central, key = world
    for k, val in (("public_registration_enabled", True), ("registration_verify_key", "k" * 40),
                   ("mailer", "fake"), ("registration_verify_url_base", "https://portal.example/verify")):  # fmt: skip
        monkeypatch.setattr(cs, k, val)
    mailer._FAKE.sent.clear()
    with factory() as s:
        admin = s.get(users.CentralUser, ids[2])
        pol.set_policy(s, ids[0], admin, approval_mode="automatic")
        s.commit()
    assert poll(client).ok
    sub = central.post("/registration", json={"contact_email": "ana@example.com",
        "enterprise_name": "Acme Ltd", "product_ids": [str(ids[0])]}).json()  # fmt: skip
    assert sub["status"] == "in_review" and sub["contact_verification"] == "pending"
    assert notifications.dispatch_due(factory, mailer.get_mailer()).sent == 1
    token = mailer._FAKE.sent[-1]["token"]
    r = central.post(f"/registration/{sub['id']}/verify", json={"token": token})
    assert r.status_code == 200 and r.json()["status"] == "activating"
    res = prov.run(factory, uuid.UUID(sub["id"]))
    assert res.status == "provisioned"
    assert poll(client).ok
    db.expire_all()
    e = db.get(Enterprise, res.enterprise_id)
    assert e is not None and e.short_name == "Acme Ltd"
    assert {x.channel for x in db.scalars(select(Entitlement))} == {"sms"}
