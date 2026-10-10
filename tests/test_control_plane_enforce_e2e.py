# ruff: noqa: F811
"""M7-g — E2E me dy DB reale: M7-f bootstrap → Central → feed cp.v1 → poller M7-e → enforce në submit."""

import httpx
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.core.errors import DomainError
from app.models.control_plane import Entitlement
from app.models.sending import AccountPlan
from app.services import control_plane_client as cc
from app.services import control_plane_poller as poller
from app.services import control_plane_shadow as shadow
from app.services import entitlements
from app.services import messages as svc
from apps.central.main import create_app
from apps.central.models import EnterpriseProduct
from apps.central.services import enterprise_products as asg
from apps.central.services import enterprises as ent
from apps.central.services import products as prod
from apps.central.services import service_auth
from apps.central.tools import bootstrap_enterprise_products as bp
from tests.test_central_bootstrap_products import cen, make_db  # noqa: F401
from tests.test_central_sync_api import keypair
from tests.test_pipeline import OK, fake, world  # noqa: F401


def submit(db, key):
    try:
        svc.submit(db, "c1", key, OK, "ACME", text="hi")
        db.commit()
        return "ok"
    except DomainError as e:
        db.rollback()
        return e.code


def test_full_chain_suspend_outage_rollback_and_rate_limit(db, world, cen, monkeypatch):
    monkeypatch.setattr(shadow, "CACHE_TTL_S", 0.0)
    monkeypatch.setattr(entitlements, "CACHE_TTL_S", 0.0)
    plan = db.scalar(select(AccountPlan))
    eid = plan.enterprise_id
    owner = plan.owner_ref
    # 1) Central: enterprise me të njëjtin UUID + produktet; assignment sms krijohet nga M7-f (apply)
    eng = create_engine(cen)
    with Session(eng, expire_on_commit=False) as s:
        ent.create(s, "Acme", enterprise_id=eid)
        prod.create(s, "sms", "SMS", "sms")
        prod.create(s, "email", "Email", "email")
        private, public = keypair()
        service_auth.create_client(s, "ent-main", ["sync:read"], [eid])
        service_auth.add_key(s, "ent-main", "k1", public)
        s.commit()
    rep = bp.run(engine.url.render_as_string(hide_password=False), cen, apply=True)
    assert rep.ok and rep.written == 1  # vetëm sms (pa domen të verifikuar ⇒ email no_evidence)
    # 2) Enterprise: snapshot nga Central përmes HTTP in-process
    central = TestClient(create_app(eng))
    key = load_pem_private_key(private.encode(), password=None)
    client = cc.ControlPlaneClient(
        cc.ControlPlaneConfig("http://testserver", "ent-main", "k1", key, 5.0), http=central
    )
    out = poller.poll_once(SessionLocal, client, snapshot_interval_s=10**9)
    assert out.ok and out.snapshots == 1
    ent_row = db.scalar(select(Entitlement).where(Entitlement.channel == "sms"))
    assert ent_row.status == "active" and ent_row.rate_limit_per_min is None
    # 3) enforce: submit kalon
    monkeypatch.setattr(settings, "cp_sync_mode", "enforce")
    shadow.clear_cache()
    assert submit(db, "k1") == "ok"
    # 4-6) Central pezullon assignment-in; feed; entitlement lokal suspended
    with Session(eng, expire_on_commit=False) as s:
        ep = s.scalar(select(EnterpriseProduct))
        asg.suspend_assignment(s, eid, ep.id)
        s.commit()
        ep_id = ep.id
    out = poller.poll_once(SessionLocal, client, snapshot_interval_s=10**9)
    assert out.ok and out.applied == 1 and out.snapshots == 0
    db.expire_all()
    assert db.scalar(select(Entitlement.status)) == "suspended"
    # 7) submit refuzohet në enforce
    assert submit(db, "k2") == "product_suspended"
    # 8-9) Central i padisponueshëm: deny mbetet
    down = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: (_ for _ in ()).throw(httpx.ConnectError("down", request=r))
        )
    )
    dead = cc.ControlPlaneClient(client._cfg, down)
    assert poller.poll_once(SessionLocal, dead, snapshot_interval_s=10**9).kind == "network_error"
    assert submit(db, "k3") == "product_suspended"
    # 10) kthim në off ⇒ sjellja legacy (AccountPlan enabled), pa ndryshim DB
    monkeypatch.setattr(settings, "cp_sync_mode", "off")
    shadow.clear_cache()
    assert submit(db, "k4") == "ok"
    # kufiri/min nëpër të njëjtën zinxhir: Central aktivizon + vendos 1/min ⇒ poller ⇒ enforce
    monkeypatch.setattr(settings, "cp_sync_mode", "enforce")
    shadow.clear_cache()
    with Session(eng, expire_on_commit=False) as s:
        asg.activate_assignment(s, eid, ep_id)
        asg.set_rate_limit(s, eid, ep_id, 1)
        s.commit()
    assert poller.poll_once(SessionLocal, client, snapshot_interval_s=10**9).ok
    db.expire_all()
    e = db.scalar(select(Entitlement))
    assert (e.status, e.rate_limit_per_min) == ("active", 1)
    # k1 dhe k4 janë tashmë në minutën aktuale ⇒ kufiri 1/min i CP bllokon (legacy do lejonte 600)
    assert submit(db, "k5") == "rate_limited"
    with Session(eng, expire_on_commit=False) as s:
        asg.set_rate_limit(s, eid, ep_id, None)  # kthim te default lokal
        s.commit()
    assert poller.poll_once(SessionLocal, client, snapshot_interval_s=10**9).ok
    db.expire_all()
    assert db.scalar(select(Entitlement.rate_limit_per_min)) is None
    assert submit(db, "k6") == "ok"
    # AccountPlan e paprekur nga i gjithë procesi; Central ka vetëm audit njeri/sistem sipas veprimit
    db.expire_all()
    p = db.scalar(select(AccountPlan))
    assert p.enabled is True and p.owner_ref == owner
    eng.dispose()
