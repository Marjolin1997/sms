import base64
import time

import pytest

from app.models.users import User
from app.services import mfa
from tests.test_auth import BOOT, PW, hdr, login, onboard, raw_client  # noqa: F401


def secret_of(db) -> bytes:
    db.expire_all()
    return mfa._secret(db.query(User).filter_by(email="ana@example.com").one())


def code_now(secret: bytes, shift: int = 0) -> str:
    return mfa.totp_at(secret, int(time.time() // 30) + shift)


def enable_2fa(c, db, token):
    r = c.post("/v1/auth/2fa/setup", json={"password": PW}, headers=hdr(token))
    assert r.status_code == 200, r.text
    b32 = r.json()["secret"]
    assert base64.b32decode(b32 + "=" * (-len(b32) % 8)) == secret_of(db)
    assert r.json()["uri"].startswith("otpauth://totp/") and f"secret={b32}" in r.json()["uri"]
    e = c.post("/v1/auth/2fa/enable", json={"code": code_now(secret_of(db))}, headers=hdr(token))
    assert e.status_code == 200, e.text
    return e.json()["recovery_codes"]


def test_rfc6238_vectors():
    key = b"12345678901234567890"
    assert mfa.totp_at(key, 59 // 30, 8) == "94287082"
    assert mfa.totp_at(key, 1111111109 // 30, 8) == "07081804"
    assert mfa.totp_at(key, 20000000000 // 30, 8) == "65353130"
    step = mfa.verify_totp(key, mfa.totp_at(key, 100), 0, now=100 * 30 + 5)
    assert step == 100
    assert mfa.verify_totp(key, mfa.totp_at(key, 100), 100, now=100 * 30) is None  # ripërdorim
    assert mfa.verify_totp(key, mfa.totp_at(key, 98), 0, now=100 * 30) is None  # jashtë dritares
    assert mfa.verify_totp(key, mfa.totp_at(key, 99), 0, now=100 * 30) == 99  # ±1 hap


def test_enable_then_login_needs_code(raw_client, db):  # noqa: F811
    c = raw_client
    tok = onboard(c)["token"]
    other = login(c).json()["token"]
    codes = enable_2fa(c, db, tok)
    assert len(codes) == 10 and all(len(x) == 19 for x in codes)
    assert c.get("/v1/me", headers=hdr(tok)).json()["mfa_enabled"] is True
    assert c.get("/v1/me", headers=hdr(other)).status_code == 401  # sesionet e tjera mbyllen
    assert c.get("/v1/auth/2fa", headers=hdr(tok)).json() == {
        "enabled": True,
        "required": False,
        "recovery_left": 10,
    }

    r = login(c)
    assert r.status_code == 200 and r.json()["mfa_required"] is True and "token" not in r.json()
    mt = r.json()["mfa_token"]
    assert c.get("/v1/me", headers=hdr(mt)).status_code == 401  # tokeni 2FA s'është sesion
    assert (
        c.post(f"/v1/auth/invite/{mt}", json={"password": "x" * 12}).status_code == 404
    )  # as ftesë
    assert c.post("/v1/auth/login/mfa", json={"mfa_token": mt, "code": "000000"}).status_code == 401
    ok = c.post("/v1/auth/login/mfa", json={"mfa_token": mt, "code": code_now(secret_of(db), 1)})
    assert ok.status_code == 200 and ok.json()["token"].startswith("sess_")
    assert ok.json()["used_recovery"] is False
    assert c.get("/v1/me", headers=hdr(ok.json()["token"])).status_code == 200
    # tokeni përdoret një herë
    again = c.post("/v1/auth/login/mfa", json={"mfa_token": mt, "code": code_now(secret_of(db), 1)})
    assert again.status_code == 401 and again.json()["detail"]["code"] == "mfa_expired"


def test_totp_code_cannot_be_replayed(raw_client, db):  # noqa: F811
    c = raw_client
    enable_2fa(c, db, onboard(c)["token"])  # enable përdori kodin e këtij hapi
    mt = login(c).json()["mfa_token"]
    same = c.post("/v1/auth/login/mfa", json={"mfa_token": mt, "code": code_now(secret_of(db))})
    assert same.status_code == 401
    nxt = c.post("/v1/auth/login/mfa", json={"mfa_token": mt, "code": code_now(secret_of(db), 1)})
    assert nxt.status_code == 200


def test_recovery_code_single_use_and_counts(raw_client, db):  # noqa: F811
    c = raw_client
    codes = enable_2fa(c, db, onboard(c)["token"])
    mt = login(c).json()["mfa_token"]
    r = c.post("/v1/auth/login/mfa", json={"mfa_token": mt, "code": codes[0].upper()})
    assert r.status_code == 200 and r.json()["used_recovery"] and r.json()["recovery_left"] == 9
    mt2 = login(c).json()["mfa_token"]
    assert (
        c.post("/v1/auth/login/mfa", json={"mfa_token": mt2, "code": codes[0]}).status_code == 401
    )
    assert (
        c.post(
            "/v1/auth/login/mfa", json={"mfa_token": mt2, "code": codes[1].replace("-", " ")}
        ).status_code
        == 200
    )


def test_lockout_covers_second_step_and_relogin_does_not_reset(raw_client, db):  # noqa: F811
    c = raw_client
    enable_2fa(c, db, onboard(c)["token"])
    for _ in range(2):  # 2 cikle x 3 gabime; fjalëkalimi i saktë nuk zeron numëruesin
        mt = login(c).json()["mfa_token"]
        for _ in range(3):
            c.post("/v1/auth/login/mfa", json={"mfa_token": mt, "code": "111111"})
    assert login(c).status_code == 401  # tashmë e bllokuar (5 gabime gjithsej)
    good = code_now(secret_of(db), 1)
    assert c.post("/v1/auth/login/mfa", json={"mfa_token": mt, "code": good}).status_code == 401


def test_disable_regenerate_and_password_required(raw_client, db):  # noqa: F811
    c = raw_client
    tok = onboard(c)["token"]
    assert (
        c.post("/v1/auth/2fa/enable", json={"code": "123456"}, headers=hdr(tok)).status_code == 409
    )
    assert (
        c.post(
            "/v1/auth/2fa/setup", json={"password": "nope nope nope"}, headers=hdr(tok)
        ).status_code
        == 401
    )
    codes = enable_2fa(c, db, tok)
    assert c.post("/v1/auth/2fa/setup", json={"password": PW}, headers=hdr(tok)).status_code == 409
    step = mfa.totp_at(secret_of(db), int(time.time() // 30) + 1)
    bad = c.post("/v1/auth/2fa/disable", json={"password": "wrong", "code": step}, headers=hdr(tok))
    assert bad.status_code == 401
    new = c.post(
        "/v1/auth/2fa/recovery-codes", json={"password": PW, "code": step}, headers=hdr(tok)
    )
    assert new.status_code == 200 and new.json()["recovery_codes"] != codes
    mt = login(c).json()["mfa_token"]  # kodet e vjetra nuk vlejnë më
    assert c.post("/v1/auth/login/mfa", json={"mfa_token": mt, "code": codes[2]}).status_code == 401
    step2 = mfa.totp_at(secret_of(db), int(time.time() // 30) + 1)  # hapi +1 u përdor tani
    assert step2 == step
    rc = new.json()["recovery_codes"][0]
    off = c.post("/v1/auth/2fa/disable", json={"password": PW, "code": rc}, headers=hdr(tok))
    assert off.status_code == 204
    assert login(c).json().get("mfa_required") is None
    assert c.get("/v1/auth/2fa", headers=hdr(tok)).json() == {
        "enabled": False,
        "required": False,
        "recovery_left": 0,
    }


def test_password_reset_does_not_bypass_2fa(raw_client, db):  # noqa: F811
    c = raw_client
    enable_2fa(c, db, onboard(c)["token"])
    uid = db.query(User).one().id
    link = c.post(f"/v1/admin/users/{uid}/reset", headers=BOOT).json()["invite_token"]
    r = c.post(f"/v1/auth/invite/{link}", json={"password": "a whole new passphrase"})
    assert r.status_code == 200 and r.json()["mfa_required"] and "token" not in r.json()
    done = c.post(
        "/v1/auth/login/mfa",
        json={"mfa_token": r.json()["mfa_token"], "code": code_now(secret_of(db), 1)},
    )
    assert done.status_code == 200


def test_admin_reset_2fa_and_listing(raw_client, db):  # noqa: F811
    c = raw_client
    tok = enable_2fa(c, db, onboard(c)["token"]) and login(c).json()["mfa_token"]
    uid = db.query(User).one().id
    assert c.get("/v1/admin/users", headers=BOOT).json()[0]["mfa"] is True
    r = c.post(f"/v1/admin/users/{uid}/reset-2fa", headers=BOOT)
    assert r.status_code == 200 and r.json()["mfa"] is False
    assert (
        c.post("/v1/auth/login/mfa", json={"mfa_token": tok, "code": "123456"}).status_code == 401
    )
    assert login(c).json()["token"].startswith("sess_")
    client = login(c).json()["token"]
    assert c.post(f"/v1/admin/users/{uid}/reset-2fa", headers=hdr(client)).status_code == 403


def test_notices_are_emailed(raw_client, db, monkeypatch):  # noqa: F811
    from app import providers
    from app.core.config import settings
    from app.providers.email import FakeEmailProvider

    box = FakeEmailProvider()
    monkeypatch.setitem(providers._email_registry, "fake", box)
    monkeypatch.setattr(settings, "system_from_email", "no-reply@platform.example")
    c = raw_client
    tok = onboard(c)["token"]
    enable_2fa(c, db, tok)
    assert any(b"turned on" in m.raw for m in box.calls)


def test_setup_unavailable_without_secrets_key(raw_client, monkeypatch):  # noqa: F811
    from app.core.config import settings

    c = raw_client
    tok = onboard(c)["token"]
    monkeypatch.setattr(settings, "secrets_key", "")
    r = c.post("/v1/auth/2fa/setup", json={"password": PW}, headers=hdr(tok))
    assert r.status_code == 503
    pytest.importorskip("cryptography")


# --- Detyrimi për stafin -------------------------------------------------------------------


def staff(c, db, email="sam@example.com"):
    tok = onboard(c, email, role="support", owner=None)["token"]
    return tok


def enable_for(c, db, token, email):
    c.post("/v1/auth/2fa/setup", json={"password": PW}, headers=hdr(token))
    db.expire_all()
    sec = mfa._secret(db.query(User).filter_by(email=email).one())
    r = c.post("/v1/auth/2fa/enable", json={"code": code_now(sec)}, headers=hdr(token))
    assert r.status_code == 200, r.text
    return sec


def test_staff_must_set_up_2fa_before_anything_else(raw_client, db):  # noqa: F811
    c = raw_client
    tok = staff(c, db)
    me = c.get("/v1/me", headers=hdr(tok)).json()
    assert me["mfa_setup_required"] is True and me["mfa_enabled"] is False
    blocked = c.get("/v1/admin/stats", headers=hdr(tok))
    assert blocked.status_code == 403 and blocked.json()["detail"]["code"] == "mfa_setup_required"
    assert c.get("/v1/auth/2fa", headers=hdr(tok)).json()["required"] is True  # rrugët e 2FA hapen
    assert c.get("/v1/auth/sessions", headers=hdr(tok)).status_code == 200
    enable_for(c, db, tok, "sam@example.com")
    assert c.get("/v1/admin/stats", headers=hdr(tok)).status_code == 200
    assert c.get("/v1/me", headers=hdr(tok)).json()["mfa_setup_required"] is False


def test_staff_cannot_turn_2fa_off_but_client_can(raw_client, db):  # noqa: F811
    c = raw_client
    tok = staff(c, db)
    sec = enable_for(c, db, tok, "sam@example.com")
    step = mfa.totp_at(sec, int(time.time() // 30) + 1)
    r = c.post("/v1/auth/2fa/disable", json={"password": PW, "code": step}, headers=hdr(tok))
    assert r.status_code == 409 and "required" in r.json()["detail"]["message"]
    assert c.get("/v1/auth/2fa", headers=hdr(tok)).json()["enabled"] is True


def test_admin_reset_puts_staff_back_into_setup(raw_client, db):  # noqa: F811
    c = raw_client
    tok = staff(c, db)
    enable_for(c, db, tok, "sam@example.com")
    uid = db.query(User).filter_by(email="sam@example.com").one().id
    assert c.post(f"/v1/admin/users/{uid}/reset-2fa", headers=BOOT).status_code == 200
    again = login(c, "sam@example.com").json()["token"]
    assert c.get("/v1/admin/stats", headers=hdr(again)).status_code == 403


def test_enforcement_modes_and_keys(raw_client, db, monkeypatch):  # noqa: F811
    from app.core.config import settings

    c = raw_client
    tok = staff(c, db)
    cl = onboard(c)["token"]
    assert c.get("/v1/me", headers=hdr(cl)).json()["mfa_setup_required"] is False  # klient
    key = c.post("/v1/admin/api-keys", json={"name": "k", "role": "support"}, headers=BOOT).json()[
        "key"
    ]
    assert c.get("/v1/admin/stats", headers=hdr(key)).status_code == 200  # çelësat API s'preken
    assert c.get("/v1/admin/stats", headers=BOOT).status_code == 200

    monkeypatch.setattr(settings, "require_2fa", "none")
    assert c.get("/v1/admin/stats", headers=hdr(tok)).status_code == 200
    monkeypatch.setattr(settings, "require_2fa", "all")
    assert c.get("/v1/me", headers=hdr(cl)).json()["mfa_setup_required"] is True
    monkeypatch.setattr(settings, "require_2fa", "staff")
    monkeypatch.setattr(settings, "secrets_key", "")  # pa çelës s'mund të konfigurohet: s'detyrohet
    assert c.get("/v1/admin/stats", headers=hdr(tok)).status_code == 200
