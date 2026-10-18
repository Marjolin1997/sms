"""Faza 14: 2FA (TOTP) për veprimet e ndjeshme të stafit."""

import time

import pytest
from sqlalchemy import select

from app.core import crypto, totp
from app.core.config import settings
from app.models.admin import ApiKey, AuthFailure

BOOT = {"X-Admin-Key": "test-key"}


def staff_key(client, role="finance"):
    r = client.post("/v1/admin/api-keys", json={"name": role, "role": role}, headers=BOOT)
    return r.json()


def hdr(k, code=None):
    h = {"Authorization": f"Bearer {k['key']}", "X-Admin-Key": ""}
    if code:
        h["X-TOTP"] = code
    return h


def secret_of(db, k):
    db.expire_all()
    return crypto.decrypt(db.get(ApiKey, k["id"]).totp_secret_enc).decode()


def enable(client, db, k):
    r = client.post("/v1/me/2fa/enroll", headers=hdr(k))
    assert r.status_code == 201, r.text
    secret = r.json()["secret"]
    assert r.json()["otpauth_uri"].startswith("otpauth://totp/")
    step = int(time.time() // totp.STEP)
    c = client.post("/v1/me/2fa/confirm", json={"code": totp._code(secret, step)}, headers=hdr(k))
    assert c.status_code == 200, c.text
    return secret


def create_key(client, headers):
    """Veprim i ndjeshëm (keys:manage, POST): krijon një çelës stafi."""
    return client.post("/v1/admin/api-keys", json={"name": "x", "role": "support"}, headers=headers)


def code_at(secret, offset=0):
    return totp._code(secret, int(time.time() // totp.STEP) + offset)


def test_rfc6238_vector():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890"
    assert totp._code(secret, 59 // 30) == "287082"
    assert totp.verify(secret, "287082", now=59) == 1
    assert totp.verify(secret, "000000", now=59) is None


def test_verify_rejects_replay_and_out_of_window():
    s = totp.new_secret()
    now = 1_000_000.0
    step = totp.verify(s, totp._code(s, int(now // 30)), now=now)
    assert step is not None
    assert totp.verify(s, totp._code(s, int(now // 30)), last_step=step, now=now) is None
    assert totp.verify(s, totp._code(s, int(now // 30) + 5), now=now) is None
    assert totp.verify(s, "12ab56", now=now) is None


def test_secret_stored_encrypted(client, db):
    k = staff_key(client)
    client.post("/v1/me/2fa/enroll", headers=hdr(k))
    row = db.scalars(select(ApiKey).where(ApiKey.id == k["id"])).one()
    assert row.totp_enabled is False and row.totp_secret_enc
    assert secret_of(db, k) not in row.totp_secret_enc


def test_bootstrap_key_cannot_enroll(client):
    assert client.post("/v1/me/2fa/enroll", headers=BOOT).status_code == 400


def test_sensitive_action_requires_code_once_enabled(client, db):
    k = staff_key(client)
    # para regjistrimit: pa kërkesë (require_staff_2fa=False)
    assert create_key(client, hdr(k)).status_code == 403  # finance s'ka keys:manage
    sk = staff_key(client, "superadmin")
    assert create_key(client, hdr(sk)).status_code == 201
    secret = enable(client, db, sk)
    r = create_key(client, hdr(sk))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "totp_required"
    bad = create_key(client, hdr(sk, "000000"))
    assert bad.status_code == 403 and bad.json()["detail"]["code"] == "totp_invalid"
    # kodi i hapit të ardhshëm (i papërdorur) pranohet
    ok = create_key(client, hdr(sk, code_at(secret, 1)))
    assert ok.status_code == 201, ok.text
    # i njëjti kod nuk pranohet dy herë
    again = create_key(client, hdr(sk, code_at(secret, 1)))
    assert again.status_code == 403


def test_non_sensitive_actions_need_no_code(client, db):
    sk = staff_key(client, "superadmin")
    enable(client, db, sk)
    assert client.get("/v1/me", headers=hdr(sk)).json()["two_factor"] is True
    assert client.get("/v1/admin/accounts", headers=hdr(sk)).status_code == 200  # monitor:read
    assert client.get("/v1/admin/api-keys", headers=hdr(sk)).status_code == 200  # leximi: pa kod


def test_bootstrap_key_is_exempt(client):
    assert create_key(client, BOOT).status_code == 201


def test_bad_codes_are_throttled(client, db, monkeypatch):
    monkeypatch.setattr(settings, "auth_max_failures", 3)
    sk = staff_key(client, "superadmin")
    enable(client, db, sk)
    for _ in range(3):
        assert create_key(client, hdr(sk, "111111")).status_code == 403
    assert len(db.scalars(select(AuthFailure)).all()) == 3
    assert create_key(client, hdr(sk, "111111")).status_code == 429


def test_enforcement_when_required(client, monkeypatch):
    monkeypatch.setattr(settings, "require_staff_2fa", True)
    boot_created = staff_key(client, "superadmin")
    r = create_key(client, hdr(boot_created))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "totp_enrollment_required"
    assert client.get("/v1/me", headers=hdr(boot_created)).json()["two_factor_required"] is True
    # ende mund të regjistrojë 2FA
    assert client.post("/v1/me/2fa/enroll", headers=hdr(boot_created)).status_code == 201


def test_clients_are_not_affected(client, monkeypatch):
    monkeypatch.setattr(settings, "require_staff_2fa", True)
    k = client.post(
        "/v1/admin/api-keys",
        json={"name": "c", "role": "client", "owner_ref": "acme"},
        headers=BOOT,
    ).json()
    assert client.get("/v1/portal/api-keys", headers=hdr(k)).status_code == 200


def test_confirm_needs_valid_code_and_enroll_first(client, db):
    k = staff_key(client, "superadmin")
    assert (
        client.post("/v1/me/2fa/confirm", json={"code": "123456"}, headers=hdr(k)).status_code
        == 409
    )
    client.post("/v1/me/2fa/enroll", headers=hdr(k))
    r = client.post("/v1/me/2fa/confirm", json={"code": "000000"}, headers=hdr(k))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "totp_invalid"
    assert client.get("/v1/me", headers=hdr(k)).json()["two_factor"] is False


def test_reset_2fa_by_bootstrap_and_audited(client, db):
    sk = staff_key(client, "superadmin")
    enable(client, db, sk)
    r = client.post(f"/v1/admin/api-keys/{sk['id']}/reset-2fa", headers=BOOT)
    assert r.status_code == 200 and r.json()["two_factor"] is False
    assert create_key(client, hdr(sk)).status_code == 201  # ndodhet sërish pa kod
    actions = [x["action"] for x in client.get("/v1/admin/audit", headers=BOOT).json()]
    assert "apikey.reset_2fa" in actions and "2fa.enabled" in actions


@pytest.mark.parametrize("perm_route", [("post", "/v1/admin/switches/submit")])
def test_other_sensitive_routes_gated(client, db, perm_route):
    sk = staff_key(client, "superadmin")
    enable(client, db, sk)
    r = client.put("/v1/admin/switches/submit", json={"enabled": True}, headers=hdr(sk))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "totp_required"
