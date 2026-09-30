from datetime import UTC, datetime, timedelta

import pytest

from app.models.admin import AuditLog
from app.models.users import User, UserSession
from app.services import auth as svc
from tests.test_console_api import BOOT, raw_client  # noqa: F401

PW = "correct horse battery"


def invite(c, email="ana@example.com", role="client", owner="c1"):
    r = c.post(
        "/v1/admin/users", json={"email": email, "role": role, "owner_ref": owner}, headers=BOOT
    )
    assert r.status_code == 201, r.text
    return r.json()


def onboard(c, email="ana@example.com", pw=PW, **kw):
    tok = invite(c, email, **kw)["invite_token"]
    r = c.post(f"/v1/auth/invite/{tok}", json={"password": pw})
    assert r.status_code == 200, r.text
    return r.json()


def hdr(token):
    return {"Authorization": f"Bearer {token}"}


def login(c, email="ana@example.com", pw=PW, **kw):
    return c.post("/v1/auth/login", json={"email": email, "password": pw, **kw})


def test_invite_flow_and_me(raw_client):  # noqa: F811
    c = raw_client
    u = invite(c)
    assert u["invited"] and u["email"] == "ana@example.com"
    info = c.get(f"/v1/auth/invite/{u['invite_token']}").json()
    assert info["email"] == "ana@example.com" and info["kind"] == "invite"
    assert login(c).status_code == 401  # s'ka fjalëkalim ende
    s = c.post(f"/v1/auth/invite/{u['invite_token']}", json={"password": PW}).json()
    assert s["token"].startswith("sess_") and s["user"]["owner_ref"] == "c1"
    me = c.get("/v1/me", headers=hdr(s["token"])).json()
    assert me["email"] == "ana@example.com" and me["via"] == "password" and me["owner_ref"] == "c1"
    assert "messages:send" in me["permissions"]
    # token i përdorur nuk vlen më
    assert c.post(f"/v1/auth/invite/{u['invite_token']}", json={"password": PW}).status_code == 404
    assert c.get(f"/v1/auth/invite/{u['invite_token']}").status_code == 404
    # sesioni punon si çelës: klienti sheh vetëm llogarinë e vet
    assert c.get("/v1/inbox/unread-count", headers=hdr(s["token"])).json() == {"unread": 0}


def test_login_logout_and_bad_credentials(raw_client):  # noqa: F811
    c = raw_client
    onboard(c)
    ok = login(c, "  ANA@Example.com ", PW)
    assert ok.status_code == 200
    t = ok.json()["token"]
    assert c.get("/v1/me", headers=hdr(t)).status_code == 200
    assert c.post("/v1/auth/logout", headers=hdr(t)).status_code == 204
    assert c.get("/v1/me", headers=hdr(t)).status_code == 401
    bad = login(c, pw="wrong password")
    ghost = login(c, "nobody@example.com", PW)
    assert bad.status_code == ghost.status_code == 401
    assert bad.json() == ghost.json()  # pa zbulim të email-eve që ekzistojnë
    assert c.get("/v1/me", headers=hdr("sess_deadbeef_x")).status_code == 401


def test_lockout_after_failures_even_with_right_password(raw_client, db):  # noqa: F811
    c = raw_client
    onboard(c)
    for _ in range(svc.MAX_FAILED):
        assert login(c, pw="nope nope nope").status_code == 401
    assert login(c).status_code == 401  # e bllokuar
    u = db.query(User).one()
    assert u.locked_until is not None
    u.locked_until = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert login(c).status_code == 200  # blloku skadoi


def test_password_policy_and_change_revokes_other_sessions(raw_client, db):  # noqa: F811
    c = raw_client
    onboard(c)
    a = login(c).json()["token"]
    b = login(c).json()["token"]
    ch = lambda tok, cur, new: c.post(  # noqa: E731
        "/v1/auth/change-password",
        json={"current_password": cur, "new_password": new},
        headers=hdr(tok),
    )
    assert ch(a, "wrong", "another long pass").status_code == 401
    for weak in ("short", "password123", "ana-is-cool-ana"):
        assert ch(a, PW, weak).json()["detail"]["code"] == "weak_password"
    assert ch(a, PW, PW).status_code == 422
    assert ch(a, PW, "brand new passphrase").status_code == 204
    assert c.get("/v1/me", headers=hdr(a)).status_code == 200  # sesioni aktual mbetet
    assert c.get("/v1/me", headers=hdr(b)).status_code == 401  # tjetri u mbyll
    assert login(c).status_code == 401 and login(c, pw="brand new passphrase").status_code == 200


def test_sessions_list_revoke_and_isolation(raw_client):  # noqa: F811
    c = raw_client
    onboard(c)
    onboard(c, "bob@example.com", owner="c2")
    a = login(c).json()["token"]
    b = login(c).json()["token"]
    other = login(c, "bob@example.com").json()["token"]
    rows = c.get("/v1/auth/sessions", headers=hdr(a)).json()
    assert len(rows) == 3 and sum(r["current"] for r in rows) == 1  # + sesioni i pranimit të ftesës
    bid = next(r["id"] for r in rows if not r["current"])
    bobs = c.get("/v1/auth/sessions", headers=hdr(other)).json()
    assert c.delete(f"/v1/auth/sessions/{bobs[0]['id']}", headers=hdr(a)).status_code == 404
    assert c.delete(f"/v1/auth/sessions/{bid}", headers=hdr(a)).status_code == 204
    assert c.get("/v1/me", headers=hdr(b)).status_code == 401
    # çelësat API s'kanë sesion
    k = c.post("/v1/admin/api-keys", json={"name": "k", "role": "support"}, headers=BOOT).json()[
        "key"
    ]
    assert c.get("/v1/auth/sessions", headers=hdr(k)).status_code == 400


def test_expiry_disable_reset_and_admin_rules(raw_client, db):  # noqa: F811
    c = raw_client
    onboard(c)
    t = login(c).json()["token"]
    for s in db.query(UserSession).all():
        s.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert c.get("/v1/me", headers=hdr(t)).status_code == 401

    t = login(c).json()["token"]
    uid = db.query(User).one().id
    assert c.post(f"/v1/admin/users/{uid}/disable", headers=BOOT).json()["status"] == "disabled"
    assert c.get("/v1/me", headers=hdr(t)).status_code == 401
    assert login(c).status_code == 401
    assert c.post(f"/v1/admin/users/{uid}/reset", headers=BOOT).status_code == 409
    c.post(f"/v1/admin/users/{uid}/enable", headers=BOOT)
    tok = c.post(f"/v1/admin/users/{uid}/reset", headers=BOOT).json()["invite_token"]
    assert c.get(f"/v1/auth/invite/{tok}").json()["kind"] == "reset"
    assert (
        c.post(f"/v1/auth/invite/{tok}", json={"password": "a whole new passphrase"}).status_code
        == 200
    )
    assert login(c).status_code == 401  # fjalëkalimi i vjetër s'vlen më

    # rregullat e krijimit
    bad = lambda **kw: c.post("/v1/admin/users", json=kw, headers=BOOT).status_code  # noqa: E731
    assert bad(email="x@example.com", role="client") == 422  # klient pa llogari
    assert bad(email="x@example.com", role="support", owner_ref="c1") == 422
    assert bad(email="not-an-email", role="support") == 422
    assert bad(email="ana@example.com", role="support") == 409  # dublikatë
    client = c.post(
        "/v1/auth/login", json={"email": "ana@example.com", "password": "a whole new passphrase"}
    ).json()
    assert c.get("/v1/admin/users", headers=hdr(client["token"])).status_code == 403
    assert c.get("/v1/admin/users", headers=BOOT).status_code == 200
    users = c.get("/v1/admin/users", headers=BOOT).json()
    assert "password" not in str(users) and "hash" not in str(users)
    assert db.query(AuditLog).filter_by(action="auth.login").count() >= 1


def test_password_hash_format():
    h = svc.hash_password(PW)
    assert h.startswith("scrypt$") and PW not in h
    assert svc.verify_password(PW, h) and not svc.verify_password("x", h)
    assert svc.hash_password(PW) != h  # kripë e ndryshme
    assert not svc.verify_password(PW, None) and not svc.verify_password(PW, "garbage")
    with pytest.raises(svc.WeakPassword):
        svc.check_password_policy("aaaaaaaaaaaa")
