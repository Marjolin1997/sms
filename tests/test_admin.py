from datetime import UTC, datetime, timedelta

import pytest

from app.models.admin import AuditImmutableError, AuditLog
from app.models.sending import AccountPlan, MessageStatus
from app.services import messages as svc
from app.services import wallet as wallets
from tests.test_pipeline import OK, fake, send, world  # noqa: F401

BOOT = {"X-Admin-Key": "test-key"}


@pytest.fixture
def client():
    """Pa kredenciale të paracaktuara: çdo kërkesë deklaron identitetin e vet."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app())


def new_key(client, role, owner_ref=None, **extra):
    r = client.post(
        "/v1/admin/api-keys",
        json={"name": f"{role}-key", "role": role, "owner_ref": owner_ref, **extra},
        headers=BOOT,
    )
    assert r.status_code == 201, r.text
    return r.json()


def bearer(key):
    return {"Authorization": f"Bearer {key['key']}"}


def test_key_lifecycle(client):
    k = new_key(client, "finance")
    assert k["key"].startswith("sms_") and "key_hash" not in k
    assert client.get("/v1/admin/api-keys", headers=BOOT).json()[0].get("key") is None
    h = bearer(k)
    assert (
        client.post(
            "/v1/wallets", json={"owner_ref": "c", "currency": "EUR"}, headers=h
        ).status_code
        == 201
    )
    assert client.post(f"/v1/admin/api-keys/{k['id']}/revoke", headers=BOOT).status_code == 200
    assert (
        client.post(
            "/v1/wallets", json={"owner_ref": "d", "currency": "EUR"}, headers=h
        ).status_code
        == 401
    )
    assert client.post(f"/v1/admin/api-keys/{k['id']}/revoke", headers=BOOT).status_code == 409


def test_bad_credentials(client):
    for h in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Bearer sms_abcd1234_wrong"},
              {"X-Admin-Key": "bad"}):  # fmt: skip
        assert client.get("/v1/admin/switches", headers=h).status_code == 401
    k = new_key(client, "finance")
    tampered = {"Authorization": "Bearer " + k["key"][:-2] + "xx"}
    assert client.get("/v1/admin/switches", headers=tampered).status_code == 401


def test_expired_key_rejected(client, db):
    k = new_key(client, "support", expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat())
    from app.models.admin import ApiKey

    row = db.get(ApiKey, k["id"])
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert client.get("/v1/admin/switches", headers=bearer(k)).status_code == 401
    past = {"name": "x", "role": "support", "expires_at": "2000-01-01T00:00:00Z"}
    assert client.post("/v1/admin/api-keys", json=past, headers=BOOT).status_code == 422


def test_key_validation(client):
    bad = [{"role": "client"}, {"role": "finance", "owner_ref": "c1"}, {"role": "root"}]
    for extra in bad:
        r = client.post("/v1/admin/api-keys", json={"name": "n", **extra}, headers=BOOT)
        assert r.status_code == 422


def test_rbac_matrix(client):
    finance, approver, support = (new_key(client, r) for r in ("finance", "approver", "support"))
    # finance nuk menaxhon çelësa, tarifa apo miratime
    assert client.post("/v1/admin/api-keys", json={"name": "x", "role": "support"},
                       headers=bearer(finance)).status_code == 403  # fmt: skip
    assert client.post("/v1/rate-cards", json={"name": "a", "currency": "EUR"},
                       headers=bearer(finance)).status_code == 403  # fmt: skip
    # approver nuk prek para
    assert client.post("/v1/wallets", json={"owner_ref": "c", "currency": "EUR"},
                       headers=bearer(approver)).status_code == 403  # fmt: skip
    # support lexon, s'shkruan; por mund të ndalë dërgimin
    assert client.get("/v1/admin/audit", headers=bearer(support)).status_code == 200
    assert client.get("/v1/admin/api-keys", headers=bearer(support)).status_code == 403
    assert client.put("/v1/admin/switches/submit", json={"enabled": False, "reason": "incident"},
                      headers=bearer(support)).status_code == 200  # fmt: skip


def test_client_is_scoped_to_own_data(client, db, world):  # noqa: F811
    w, _ = world
    c1 = new_key(client, "client", "c1")
    c2 = new_key(client, "client", "c2")
    other = client.post(
        "/v1/wallets", json={"owner_ref": "c2", "currency": "EUR"}, headers=BOOT
    ).json()
    assert client.get(f"/v1/wallets/{w.id}", headers=bearer(c1)).status_code == 200
    assert client.get(f"/v1/wallets/{w.id}", headers=bearer(c2)).status_code == 404
    assert client.get(f"/v1/wallets/{other['id']}/ledger", headers=bearer(c1)).status_code == 404
    body = {"owner_ref": "c1", "to": OK, "sender": "ACME", "text": "hi"}
    h = {"Idempotency-Key": "k-scope"}
    assert client.post("/v1/messages", json=body, headers={**bearer(c2), **h}).status_code == 404
    r = client.post("/v1/messages", json=body, headers={**bearer(c1), **h})
    assert r.status_code == 202
    mid = r.json()["id"]
    assert client.get(f"/v1/messages/{mid}", headers=bearer(c2)).status_code == 404
    assert client.get(f"/v1/messages/{mid}", headers=bearer(c1)).status_code == 200
    # klienti nuk ka akses në panel
    assert client.get("/v1/admin/stats", headers=bearer(c1)).status_code == 403
    assert client.post(f"/v1/wallets/{w.id}/topups", json={"amount": "5", "method": "cash"},
                       headers=bearer(c1)).status_code == 403  # fmt: skip


def test_topup_separation_of_duties(client):
    f1, f2 = new_key(client, "finance"), new_key(client, "finance")
    w = client.post(
        "/v1/wallets", json={"owner_ref": "c", "currency": "EUR"}, headers=bearer(f1)
    ).json()
    t = client.post(f"/v1/wallets/{w['id']}/topups", json={"amount": "5", "method": "cash"},
                    headers=bearer(f1)).json()  # fmt: skip
    assert client.post(f"/v1/topups/{t['id']}/confirm", headers=bearer(f1)).status_code == 403
    assert client.post(f"/v1/topups/{t['id']}/confirm", headers=bearer(f2)).status_code == 200
    assert (
        client.get(f"/v1/wallets/{w['id']}", headers=bearer(f2)).json()["available"] == "5.000000"
    )


def test_adjustment_is_audited_and_bounded(client):
    f = new_key(client, "finance")
    w = client.post(
        "/v1/wallets", json={"owner_ref": "c", "currency": "EUR"}, headers=bearer(f)
    ).json()
    url = f"/v1/wallets/{w['id']}/adjustments"
    assert (
        client.post(
            url, json={"delta": "-1", "key": "a1", "note": "oops"}, headers=bearer(f)
        ).status_code
        == 402
    )
    ok = client.post(url, json={"delta": "3.5", "key": "a2", "note": "goodwill"}, headers=bearer(f))
    assert ok.status_code == 201
    log = client.get("/v1/admin/audit?target_type=wallet", headers=bearer(f)).json()
    assert [a["action"] for a in log] == ["wallet.create", "wallet.adjust"]
    assert log[1]["actor"].startswith("key:") and log[1]["detail"]["note"] == "goodwill"
    v = client.get(f"/v1/wallets/{w['id']}/verify", headers=bearer(f))
    assert v.json()["consistent"] is True


def test_audit_records_failed_changes_not(client):
    """Një veprim që dështon nuk lë gjurmë audit (transaksion i përbashkët)."""
    assert client.post("/v1/topups/999/confirm", headers=BOOT).status_code == 404
    assert client.get("/v1/admin/audit", headers=BOOT).json() == []


def test_audit_is_append_only(db):
    db.add(AuditLog(actor="a", role="r", action="x", target_type="t", target_id="1"))
    db.commit()
    row = db.query(AuditLog).one()
    row.action = "tamper"
    with pytest.raises(AuditImmutableError):
        db.flush()
    db.rollback()
    db.delete(db.query(AuditLog).one())
    with pytest.raises(AuditImmutableError):
        db.flush()
    db.rollback()


def test_kill_switch_submit_and_dispatch(client, db, world):  # noqa: F811
    w, _ = world
    m = send(db, key="ks1")
    assert (
        client.put("/v1/admin/switches/dispatch", json={"enabled": False}, headers=BOOT).status_code
        == 422
    )
    client.put(
        "/v1/admin/switches/dispatch",
        json={"enabled": False, "reason": "provider down"},
        headers=BOOT,
    )
    assert svc.process_one(db) is None  # worker ndalet
    db.refresh(m)
    assert m.status == MessageStatus.QUEUED
    client.put("/v1/admin/switches/dispatch", json={"enabled": True}, headers=BOOT)
    assert svc.process_one(db) is m

    client.put(
        "/v1/admin/switches/submit", json={"enabled": False, "reason": "incident"}, headers=BOOT
    )
    with pytest.raises(svc.SendingPaused):
        svc.submit(db, "c1", "ks2", OK, "ACME", text="hi")
    assert send_again_is_idempotent(db)  # replay i njëjtë kthen mesazhin ekzistues
    r = client.post("/v1/messages", json={"owner_ref": "c1", "to": OK, "sender": "ACME", "text": "x"},
                    headers={**BOOT, "Idempotency-Key": "ks3"})  # fmt: skip
    assert r.status_code == 503 and r.json()["detail"]["code"] == "sending_paused"
    assert wallets.verify_wallet(db, w.id)
    assert (
        client.put("/v1/admin/switches/nope", json={"enabled": True}, headers=BOOT).status_code
        == 404
    )


def send_again_is_idempotent(db):
    return svc.submit(db, "c1", "ks1", OK, "ACME", text="hello").idempotency_key == "ks1"


def test_rate_limit_per_account(client, db, world):  # noqa: F811
    _, card = world
    r = client.put(
        "/v1/admin/plans/c1", json={"rate_card_id": card.id, "rate_limit_per_min": 2}, headers=BOOT
    )
    assert r.status_code == 200
    db.expire_all()
    send(db, key="r1")
    send(db, key="r2")
    with pytest.raises(svc.RateLimited):
        svc.submit(db, "c1", "r3", OK, "ACME", text="hello")
    assert send(db, key="r1").idempotency_key == "r1"  # retry i vjetër nuk bllokohet
    later = datetime.now(UTC) + timedelta(minutes=2)
    assert svc.submit(db, "c1", "r4", OK, "ACME", text="hello", now=later)
    assert db.query(AccountPlan).one().rate_limit_per_min == 2


def test_routes_and_plans_admin(client, db):
    pricing = new_key(client, "pricing")
    body = {"prefix": "355", "country": "al", "provider": "fake", "priority": 5}
    assert client.put("/v1/admin/routes", json=body, headers=bearer(pricing)).status_code == 200
    assert client.put("/v1/admin/routes", json={**body, "priority": 9, "enabled": False},
                      headers=bearer(pricing)).status_code == 200  # fmt: skip
    routes = client.get("/v1/admin/routes", headers=bearer(pricing)).json()
    assert len(routes) == 1 and routes[0]["priority"] == 9 and routes[0]["country"] == "AL"
    assert (
        client.put(
            "/v1/admin/routes", json={**body, "prefix": "+355"}, headers=bearer(pricing)
        ).status_code
        == 422
    )


def test_stats_and_readiness(client, db, world):  # noqa: F811
    send(db, key="s1")
    send(db, key="s2")
    svc.process_one(db)
    s = client.get("/v1/admin/stats", headers=BOOT).json()
    assert s["messages_by_status"]["queued"] == 1 and s["messages_by_status"]["sent"] == 1
    assert s["oldest_queued_age_seconds"] is not None and s["stuck_sending"] == 0
    assert [x["enabled"] for x in s["switches"]] == [True, True]
    assert client.get("/readyz").json() == {"status": "ready"}
