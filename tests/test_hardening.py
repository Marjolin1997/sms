"""Faza 13: kufizim provash të dështuara, allowlist IP, rrotullim çelësash, eksport GDPR."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.security import ip_allowed
from app.models.admin import ApiKey, AuthFailure, KeyStatus
from app.services import consent
from app.services import contacts as contacts_svc

BOOT = {"X-Admin-Key": "test-key"}


def mk_key(client, **body):
    body = {"name": "k", "role": "client", "owner_ref": "acme"} | body
    r = client.post("/v1/admin/api-keys", json=body, headers=BOOT)
    assert r.status_code == 201, r.text
    return r.json()


def bearer(k):
    return {"Authorization": f"Bearer {k['key']}", "X-Admin-Key": ""}


# --- Kufizimi i provave të dështuara ------------------------------------------


def test_repeated_bad_keys_are_throttled(client, db, monkeypatch):
    monkeypatch.setattr(settings, "auth_max_failures", 3)
    good = mk_key(client)
    bad = {"Authorization": "Bearer sms_deadbeef_wrong", "X-Admin-Key": ""}
    for _ in range(3):
        assert client.get("/v1/me", headers=bad).status_code == 401
    r = client.get("/v1/me", headers=bad)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "too_many_attempts"
    assert r.headers["retry-after"] == str(settings.auth_fail_window_s)
    # edhe çelësi i vlefshëm bllokohet nga e njëjta IP gjatë dritares
    assert client.get("/v1/me", headers=bearer(good)).status_code == 429
    assert db.scalars(select(AuthFailure)).all()[0].prefix == "deadbeef"


def test_old_failures_expire(client, db, monkeypatch):
    monkeypatch.setattr(settings, "auth_max_failures", 2)
    old = datetime.now(UTC) - timedelta(seconds=settings.auth_fail_window_s + 5)
    db.add_all([AuthFailure(ip="testclient", created_at=old) for _ in range(5)])
    db.commit()
    good = mk_key(client)
    assert client.get("/v1/me", headers=bearer(good)).status_code == 200


def test_requests_without_credentials_are_not_counted(client, db):
    for _ in range(3):
        client.get("/v1/me", headers={"X-Admin-Key": ""})
    assert db.scalars(select(AuthFailure)).all() == []


def test_forwarded_for_only_trusted_with_proxy_hops(client, db, monkeypatch):
    bad = {
        "Authorization": "Bearer sms_deadbeef_wrong",
        "X-Admin-Key": "",
        "X-Forwarded-For": "9.9.9.9, 1.2.3.4",
    }
    client.get("/v1/me", headers=bad)
    assert db.scalars(select(AuthFailure)).one().ip == "testclient"  # pa proxy të besuar: injorohet
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    client.get("/v1/me", headers=bad)
    assert {f.ip for f in db.scalars(select(AuthFailure))} == {"testclient", "1.2.3.4"}


# --- Allowlist IP ---------------------------------------------------------------


def test_ip_allowed_matching():
    assert ip_allowed("203.0.113.7", None)
    assert ip_allowed("203.0.113.7", '["203.0.113.0/24"]')
    assert not ip_allowed("198.51.100.1", '["203.0.113.0/24"]')
    assert ip_allowed("2001:db8::1", '["2001:db8::/32"]')
    assert not ip_allowed("testclient", '["203.0.113.0/24"]')  # IP e palexueshme: fail closed


def test_key_bound_to_other_ip_is_refused(client):
    k = mk_key(client, allowed_cidrs=["203.0.113.0/24"])
    assert k["allowed_cidrs"] == ["203.0.113.0/24"]
    r = client.get("/v1/me", headers=bearer(k))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "ip_not_allowed"


def test_key_allowed_from_forwarded_ip(client, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    k = mk_key(client, allowed_cidrs=["203.0.113.7"])
    ok = client.get("/v1/me", headers=bearer(k) | {"X-Forwarded-For": "203.0.113.7"})
    assert ok.status_code == 200
    no = client.get("/v1/me", headers=bearer(k) | {"X-Forwarded-For": "198.51.100.1"})
    assert no.status_code == 403


@pytest.mark.parametrize("bad", ["not-an-ip", "300.1.1.1", "10.0.0.0/99"])
def test_invalid_cidr_rejected(client, bad):
    r = client.post(
        "/v1/admin/api-keys",
        json={"name": "k", "role": "client", "owner_ref": "acme", "allowed_cidrs": [bad]},
        headers=BOOT,
    )
    assert r.status_code == 422


# --- Rrotullimi -----------------------------------------------------------------


def test_rotate_key_grace_period(client, db):
    old = mk_key(client, allowed_cidrs=["127.0.0.1"])
    r = client.post(
        f"/v1/admin/api-keys/{old['id']}/rotate", json={"grace_minutes": 30}, headers=BOOT
    )
    assert r.status_code == 201, r.text
    new = r.json()
    assert (
        new["key"] != old["key"]
        and new["owner_ref"] == "acme"
        and new["allowed_cidrs"] == ["127.0.0.1/32"]
    )
    row = db.get(ApiKey, old["id"])
    end = row.expires_at.replace(tzinfo=UTC) if row.expires_at.tzinfo is None else row.expires_at
    assert row.status == KeyStatus.ACTIVE and timedelta(minutes=29) < end - datetime.now(
        UTC
    ) <= timedelta(minutes=30)


def test_rotate_zero_grace_revokes_old(client, db):
    old = mk_key(client)
    new = client.post(
        f"/v1/admin/api-keys/{old['id']}/rotate", json={"grace_minutes": 0}, headers=BOOT
    ).json()
    assert db.get(ApiKey, old["id"]).status == KeyStatus.REVOKED
    assert client.get("/v1/me", headers=bearer(old)).status_code == 401
    assert client.get("/v1/me", headers=bearer(new)).status_code == 200


def test_rotate_revoked_key_conflicts(client):
    k = mk_key(client)
    client.post(f"/v1/admin/api-keys/{k['id']}/revoke", headers=BOOT)
    assert client.post(f"/v1/admin/api-keys/{k['id']}/rotate", headers=BOOT).status_code == 409


def test_self_service_rotate_is_scoped_to_owner(client):
    a, b = mk_key(client, owner_ref="acme"), mk_key(client, owner_ref="globex")
    r = client.post(f"/v1/portal/api-keys/{b['id']}/rotate", headers=bearer(a))
    assert r.status_code == 404  # nuk zbulohet ekzistenca e çelësit të tjetrit
    r = client.post(
        f"/v1/portal/api-keys/{a['id']}/rotate", json={"grace_minutes": 5}, headers=bearer(a)
    )
    assert r.status_code == 201
    assert r.json()["replaces"]["id"] == a["id"]


def test_rotate_writes_audit(client):
    k = mk_key(client)
    client.post(f"/v1/admin/api-keys/{k['id']}/rotate", headers=BOOT)
    logs = client.get("/v1/admin/audit", headers=BOOT).json()
    assert any(x["action"] == "apikey.rotate" for x in logs)


# --- Eksport GDPR ---------------------------------------------------------------


def test_contact_export_includes_profile_lists_and_consent(client, db):
    c, _ = contacts_svc.upsert(
        db, "acme", phone="+355691230011", email="ana@example.com", first_name="Ana"
    )
    lst = contacts_svc.create_list(db, "acme", "Buletini")
    contacts_svc.add_members(db, "acme", lst.id, [c.id])
    consent.record(
        db, "acme", "sms", "+355691230011", "opt_in", "signup", "web", "tester", "ticked box"
    )
    db.commit()
    k = mk_key(client)
    out = client.get(f"/v1/contacts/{c.id}/export", headers=bearer(k)).json()
    assert out["contact"]["email"] == "ana@example.com" and out["contact"]["first_name"] == "Ana"
    assert [x["name"] for x in out["lists"]] == ["Buletini"]
    assert out["consent_history"][0]["evidence"] == "ticked box"
    assert out["sms"] == [] and out["emails"] == []


def test_contact_export_is_owner_scoped_and_audited(client, db):
    c, _ = contacts_svc.upsert(db, "acme", phone="+355691230012")
    db.commit()
    other = mk_key(client, owner_ref="globex")
    assert client.get(f"/v1/contacts/{c.id}/export", headers=bearer(other)).status_code == 404
    mine = mk_key(client)
    assert client.get(f"/v1/contacts/{c.id}/export", headers=bearer(mine)).status_code == 200
    assert any(
        x["action"] == "contact.export" for x in client.get("/v1/admin/audit", headers=BOOT).json()
    )
