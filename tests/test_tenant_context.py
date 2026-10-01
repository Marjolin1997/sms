"""M1c-a: konteksti TENANT / SYSTEM / WORKER: jetëgjatësia, pandryshueshmëria, mungesa e rrjedhjes
mes kërkesave dhe mes job-eve të workerit. Asnjë gjendje globale."""

import dataclasses
import threading
import uuid

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.tenant import system, tenant
from app.core import scope
from app.core.context import (
    SystemContext,
    TenantContext,
    TenantUnresolved,
    for_owner,
    for_row,
)
from app.core.db import get_db
from app.core.security import Principal, current_principal
from app.models.admin import AuditLog
from app.models.contacts import Contact
from app.models.enterprise import Enterprise
from app.services import audit as audit_svc
from app.services import contacts as contacts_svc
from app.services import enterprises
from app.services import messages as msgsvc
from tests.test_pipeline import world  # noqa: F401

BOOT = {"X-Admin-Key": "test-key"}


def _ent(db, owner):
    return db.scalar(select(Enterprise).where(Enterprise.owner_ref == owner))


# --- Objekti ------------------------------------------------------------------------------------


def test_tenant_context_is_immutable_and_validated():
    c = TenantContext(uuid.uuid4(), "acme")
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.owner_ref = "evil"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.enterprise_id = uuid.uuid4()  # type: ignore[misc]
    with pytest.raises(TypeError):
        TenantContext("not-a-uuid", "acme")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        TenantContext(uuid.uuid4(), "")


def test_system_context_requires_actor_and_reason():
    with pytest.raises(ValueError):
        SystemContext("", "why")
    with pytest.raises(ValueError):
        SystemContext("staff", "")


def test_client_can_never_build_a_system_context():
    with pytest.raises(Exception) as e:
        system(Principal("key:x", "client", "acme"), "peek")
    assert getattr(e.value, "status_code", None) == 403
    assert system(Principal("key:s", "superadmin"), "support").actor == "key:s"


# --- Fabrikat -----------------------------------------------------------------------------------


def test_for_owner_lookup_does_not_create_but_create_does(db):
    with pytest.raises(TenantUnresolved):
        for_owner(db, "ghost")
    assert enterprises.count(db) == 0
    c = for_owner(db, "ghost", create=True)
    db.commit()
    assert c.enterprise_id == _ent(db, "ghost").id and c.owner_ref == "ghost"


@pytest.mark.parametrize("bad", ["", " x", "x ", "a\nb"])
def test_anomalous_owner_ref_has_no_tenant_identity(db, bad):
    with pytest.raises(TenantUnresolved):
        for_owner(db, bad, create=True)


def test_for_row_takes_identity_from_the_row_not_from_state(db):
    a, _ = contacts_svc.upsert(db, "A", phone="+355691230003")
    b, _ = contacts_svc.upsert(db, "B", phone="+355691230004")
    db.commit()
    ca, cb = for_row(db, a), for_row(db, b)
    assert ca.owner_ref == "A" and cb.owner_ref == "B" and ca.enterprise_id != cb.enterprise_id
    assert ca.origin == "worker"


def test_for_row_legacy_row_without_enterprise_id_uses_resolver(db):
    a, _ = contacts_svc.upsert(db, "A", phone="+355691230003")
    db.commit()
    eid = a.enterprise_id
    a.enterprise_id = None  # rresht legacy (para backfill)
    ctx = for_row(db, a)
    assert ctx.enterprise_id == eid


# --- owned() / cross_tenant() -------------------------------------------------------------------


def test_owned_scopes_by_enterprise_and_fails_closed_on_legacy_null(db):
    a, _ = contacts_svc.upsert(db, "A", phone="+355691230003")
    contacts_svc.upsert(db, "B", phone="+355691230003")
    db.commit()
    ctx = for_owner(db, "A")
    seen = db.scalars(select(Contact).where(scope.owned(Contact, ctx))).all()
    assert [c.owner_ref for c in seen] == ["A"]
    a.enterprise_id = None  # legacy pa backfill → i padukshëm për tenant-in (fail-closed)
    db.commit()
    assert db.scalars(select(Contact).where(scope.owned(Contact, ctx))).all() == []


def test_owned_requires_both_enterprise_and_owner_ref_to_agree(db):
    a, _ = contacts_svc.upsert(db, "A", phone="+355691230003")
    _b, _ = contacts_svc.upsert(db, "B", phone="+355691230004")
    db.commit()
    ctx_b = for_owner(db, "B")
    a.enterprise_id = ctx_b.enterprise_id  # rresht i korruptuar: owner_ref=A, enterprise=B
    db.commit()
    for ctx in (for_owner(db, "A"), ctx_b):
        ids = [c.phone for c in db.scalars(select(Contact).where(scope.owned(Contact, ctx)))]
        assert "+355691230003" not in ids


def test_legacy_string_path_is_explicit_and_counted(db):
    contacts_svc.upsert(db, "A", phone="+355691230003")
    db.commit()
    scope.LEGACY_READS.clear()
    assert db.scalars(select(Contact).where(scope.owned(Contact, "A"))).all()
    assert scope.LEGACY_READS["sms_contacts"] == 1
    with pytest.raises(TypeError):
        scope.owned(Contact, None)  # type: ignore[arg-type]


def test_cross_tenant_needs_system_context_and_is_audited(db):
    with pytest.raises(TypeError):
        audit_svc.cross_tenant(db, for_owner(db, "x", create=True), "messages", "list")  # type: ignore[arg-type]
    audit_svc.cross_tenant(db, SystemContext("staff:1", "support ticket 42"), "messages", "list")
    db.commit()
    row = db.scalar(select(AuditLog).where(AuditLog.action == "cross_tenant.list"))
    assert row.actor == "staff:1" and "support ticket 42" in row.detail


# --- Jetëgjatësia: kërkesat ---------------------------------------------------------------------


def _probe_app():
    app = FastAPI()

    @app.get("/who")
    def who(
        owner_ref: str | None = None,
        db=Depends(get_db),
        p: Principal = Depends(current_principal),
    ):
        ctx = tenant(db, p, owner_ref)
        return {"enterprise_id": str(ctx.enterprise_id), "owner_ref": ctx.owner_ref}

    return app


@pytest.fixture
def two_keys(client):
    def mk(owner):
        r = client.post(
            "/v1/admin/api-keys",
            json={"name": owner, "role": "client", "owner_ref": owner},
            headers=BOOT,
        ).json()
        return {"Authorization": f"Bearer {r['key']}"}

    return mk("A"), mk("B")


def test_context_does_not_leak_between_requests(two_keys, db):
    ha, hb = two_keys
    c = TestClient(_probe_app())
    ea, eb = _ent(db, "A").id, _ent(db, "B").id
    for i in range(20):  # radhë e ndërthurur në të njëjtin proces
        h, want, other = (ha, ea, eb) if i % 2 == 0 else (hb, eb, ea)
        r = c.get("/who", headers=h)
        assert r.status_code == 200 and r.json()["enterprise_id"] == str(want) != str(other)


def test_context_does_not_leak_between_concurrent_requests(two_keys, db):
    ha, hb = two_keys
    ea, eb = str(_ent(db, "A").id), str(_ent(db, "B").id)
    c = TestClient(_probe_app())
    bad: list = []

    def run(h, want):
        for _ in range(15):
            r = c.get("/who", headers=h)
            if r.status_code != 200 or r.json()["enterprise_id"] != want:
                bad.append((want, r.status_code, r.text))

    ts = [threading.Thread(target=run, args=(h, w)) for h, w in ((ha, ea), (hb, eb)) * 2]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert bad == []


def test_client_cannot_switch_tenant_with_owner_ref_param(two_keys):
    ha, _ = two_keys
    r = TestClient(_probe_app()).get("/who?owner_ref=B", headers=ha)
    assert r.status_code == 404 and "B" not in r.text


def test_staff_must_name_the_tenant_explicitly(client, two_keys):
    c = TestClient(_probe_app(), headers=BOOT)
    assert c.get("/who").status_code == 422
    assert c.get("/who?owner_ref=A").json()["owner_ref"] == "A"
    assert c.get("/who?owner_ref=nobody").status_code == 404  # lexim: nuk krijon Enterprise


def test_legacy_key_without_enterprise_id_is_resolved_or_refused(two_keys, db):
    from app.models.admin import ApiKey

    ha, _ = two_keys
    c = TestClient(_probe_app())
    k = db.scalar(select(ApiKey).where(ApiKey.owner_ref == "A"))
    k.enterprise_id = None
    db.commit()
    r = c.get("/who", headers=ha)  # çelës legacy → zgjidhet me owner_ref
    assert r.status_code == 200 and r.json()["owner_ref"] == "A"
    db.execute(Enterprise.__table__.delete().where(Enterprise.owner_ref == "A"))
    db.commit()
    assert c.get("/who", headers=ha).status_code == 403  # pa identitet → refuzim, jo fallback


# --- Jetëgjatësia: workeri ----------------------------------------------------------------------


def _second_tenant(db, owner="c2"):
    from app.models.sending import AccountPlan
    from app.services import sender_ids
    from app.services import wallet as wallets
    from app.services.wallet import TopupMethod

    w = wallets.create_wallet(db, owner, "EUR")
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "10", TopupMethod.CASH).id)
    card_id = db.scalar(select(AccountPlan.rate_card_id).where(AccountPlan.owner_ref == "c1"))
    db.add(AccountPlan(owner_ref=owner, rate_card_id=card_id))
    s = sender_ids.request(db, owner, "AL", "OTHER")
    sender_ids.approve(db, s.id, "admin")
    db.commit()


def test_worker_processing_two_tenants_in_one_session_keeps_identities_apart(db, world):  # noqa: F811
    """A dhe B në të njëjtin sesion/cikël: asnjë event i A nuk merr enterprise_id të B."""
    from app.models.events import Event
    from app.models.sending import Message
    from tests.test_pipeline import OK, send

    _second_tenant(db)
    send(db, key="ma")
    msgsvc.submit(db, "c2", "mb", OK, "OTHER", text="hi")
    db.commit()
    while msgsvc.process_one(db) is not None:
        pass
    db.commit()
    eids = {m.owner_ref: m.enterprise_id for m in db.scalars(select(Message))}
    assert len(set(eids.values())) == 2 and None not in eids.values()
    events = db.scalars(select(Event)).all()
    assert events and {e.owner_ref for e in events} == {"c1", "c2"}
    for e in events:
        assert e.enterprise_id == eids[e.owner_ref]


def test_worker_job_context_comes_from_the_message_row(db, world):  # noqa: F811
    from app.models.sending import Message
    from tests.test_pipeline import OK, send

    _second_tenant(db)
    send(db, key="ma")
    msgsvc.submit(db, "c2", "mb", OK, "OTHER", text="hi")
    db.commit()
    ctxs = [for_row(db, m) for m in db.scalars(select(Message).order_by(Message.id))]
    assert [c.owner_ref for c in ctxs] == ["c1", "c2"]
    assert ctxs[0].enterprise_id != ctxs[1].enterprise_id


def test_worker_has_no_request_context():
    import inspect

    import app.worker as w

    src = inspect.getsource(w)
    assert "api.tenant" not in src and "current_principal" not in src


def test_worker_path_does_not_use_legacy_owner_ref_scoping(db, world):  # noqa: F811
    """WORKER: mesazh → SENT → DLR → events/webhooks, pa asnjë skopim vetëm me owner_ref."""
    from tests.test_pipeline import send

    send(db, key="w1")
    scope.LEGACY_READS.clear()
    while msgsvc.process_one(db) is not None:
        pass
    m = db.scalar(select(msgsvc.Message))
    msgsvc.apply_dlr(db, "fake", m.provider_message_id, delivered=True)
    db.commit()
    assert dict(scope.LEGACY_READS) == {}


def test_worker_legacy_job_without_enterprise_id_still_processes(db, world):  # noqa: F811
    """Job legacy (vetëm owner_ref): resolveri vetëm për përputhshmëri; eventet marrin identitetin."""
    from app.models.events import Event
    from tests.test_pipeline import send

    m = send(db, key="legacy")
    eid = m.enterprise_id
    db.execute(msgsvc.Message.__table__.update().values(enterprise_id=None))
    db.commit()
    db.expire_all()
    while msgsvc.process_one(db) is not None:
        pass
    db.commit()
    ev = db.scalars(select(Event)).all()
    assert ev and all(e.enterprise_id == eid for e in ev)


def test_worker_anomalous_owner_ref_row_still_processes_via_explicit_legacy_path(db, world):  # noqa: F811
    """owner_ref anomal (pa identitet Enterprise) nuk ndalon SMS-in: rruga legacy e shprehur."""
    from tests.test_pipeline import send

    m = send(db, key="anom")
    db.execute(msgsvc.Message.__table__.update().values(enterprise_id=None, owner_ref="c1 "))
    db.commit()
    db.expire_all()
    scope.LEGACY_READS.clear()
    assert msgsvc.process_one(db) is not None
    assert scope.LEGACY_READS  # e numëruar (M1d), por pa përjashtim
    assert m.status.value == "sent"


# --- Çelësi i skopimit (rikthim emergjent) ---------------------------------------------------------


def test_enterprise_scoping_refuses_to_start_without_dual_write():
    from app.core.config import Settings

    with pytest.raises(ValueError, match="requires SMS_ENTERPRISE_DUAL_WRITE"):
        Settings(enterprise_dual_write=False, tenant_scoping="enterprise")
    assert Settings(enterprise_dual_write=False, tenant_scoping="owner_ref")  # rikthimi lejohet


def test_owner_ref_rollback_mode_scopes_by_owner_ref_only(db, monkeypatch):
    from app.core.config import settings

    a, _ = contacts_svc.upsert(db, "A", phone="+355691230003")
    db.commit()
    ctx = for_owner(db, "A")
    a.enterprise_id = None  # rresht pa backfill
    db.commit()
    assert db.scalars(select(Contact).where(scope.owned(Contact, ctx))).all() == []  # fail-closed
    monkeypatch.setattr(settings, "tenant_scoping", "owner_ref")
    assert len(db.scalars(select(Contact).where(scope.owned(Contact, ctx))).all()) == 1
    assert scope.belongs(a, ctx)
