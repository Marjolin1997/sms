"""M4-d — autentikimi/autorizimi bazë i Central: staf, Argon2id, JWT HS256, RBAC admin/operator."""

import ast
import logging
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from apps.central.core import errors, passwords, tokens
from apps.central.core.config import settings
from apps.central.core.db import Base
from apps.central.main import create_app
from apps.central.models import CentralUser, Role, UserStatus
from apps.central.services import users
from tests.test_central import (  # noqa: F401
    IS_PG,
    ROOT,
    central_alembic,
    enterprise_alembic,
    make_db,
)

SECRET = "t" * 48
PW = "correct horse battery"


@pytest.fixture(autouse=True)
def auth_secret(monkeypatch):
    monkeypatch.setattr(settings, "auth_secret", SECRET)
    monkeypatch.setattr(settings, "auth_ttl_seconds", 900)


@pytest.fixture
def cdb(make_db):  # noqa: F811
    url = make_db()
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    yield url, eng
    eng.dispose()


@pytest.fixture
def api(cdb):
    url, eng = cdb
    return TestClient(create_app(eng)), eng


def mk(eng, email="ana@example.com", password=PW, role="admin"):
    with Session(eng, expire_on_commit=False) as s:
        u = users.create_user(s, email, password, role)
        s.commit()
        return u


def login(c, email="ana@example.com", password=PW):
    return c.post("/auth/token", json={"email": email, "password": password})


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def token_for(c, email="ana@example.com", password=PW):
    r = login(c, email, password)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


# --- skema / metadata ---------------------------------------------------------------------------


def test_users_table_exists_only_in_central_metadata():
    import app.models  # noqa: F401
    from app.core.db import Base as EnterpriseBase

    assert "users" in Base.metadata.tables and "users" not in EnterpriseBase.metadata.tables
    cols = {c.name for c in CentralUser.__table__.columns}
    assert cols == {"id", "email", "password_hash", "role", "status", "created_at", "updated_at"}
    assert CentralUser.__table__.c.id.type.__class__.__name__ == "Uuid"
    assert not any(t.startswith("sms_") for t in Base.metadata.tables)


# --- email ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,norm",
    [
        ("  Ana@Example.COM ", "ana@example.com"),
        ("A.B+tag@Sub.Example.org", "a.b+tag@sub.example.org"),
    ],
)
def test_email_normalization(raw, norm):
    assert users.normalize_email(raw) == norm


@pytest.mark.parametrize("bad", ["", "ana", "ana@", "@x.com", "a@b", "a b@x.com", "a@@x.com",
                                 "a\n@x.com", ("x" * 250) + "@x.com", None, 5])  # fmt: skip
def test_invalid_emails_rejected(bad):
    with pytest.raises(errors.Invalid):
        users.normalize_email(bad)


def test_duplicate_email_is_rejected_case_and_space_insensitively(cdb):
    _, eng = cdb
    mk(eng, "Ana@Example.com")
    with Session(eng) as s:
        for variant in ("ana@example.com", " ANA@EXAMPLE.COM "):
            with pytest.raises(errors.Conflict):
                users.create_user(s, variant, PW, "operator")
    with Session(eng) as s, pytest.raises(Exception):  # noqa: B017  (unique/CHECK në DB)
        s.add(CentralUser(email="ana@example.com", password_hash="x", role="admin"))
        s.flush()
    with Session(eng) as s, pytest.raises(Exception):  # noqa: B017
        s.add(CentralUser(email="Mixed@Case.com", password_hash="x", role="admin"))
        s.flush()


def test_database_rejects_unknown_role_and_status(cdb):
    _, eng = cdb
    for kw in ({"role": "root"}, {"role": "admin", "status": "banned"}):
        with Session(eng) as s, pytest.raises(Exception):  # noqa: B017
            s.add(CentralUser(email=f"{uuid.uuid4().hex}@x.com", password_hash="h", **kw))
            s.flush()


# --- fjalëkalimet ----------------------------------------------------------------------------------


def test_password_is_hashed_with_argon2id_and_verifies():
    h = passwords.hash_password(PW)
    assert h != PW and PW not in h and h.startswith("$argon2id$")
    assert passwords.hash_password(PW) != h  # kripë e rastit
    assert passwords.verify_password(h, PW) is True
    assert passwords.verify_password(h, PW + "x") is False
    assert passwords.verify_password(h, "") is False


@pytest.mark.parametrize(
    "bad", ["", "garbage", "$argon2id$broken", "$2b$12$abc", None, 5, "x" * 500]
)
def test_malformed_hashes_never_raise_and_never_verify(bad):
    assert passwords.verify_password(bad, PW) is False
    assert passwords.needs_rehash(bad) is False


@pytest.mark.parametrize("pw", ["short", "x" * 129, None, 5])
def test_password_policy_bounds(pw):
    with pytest.raises(errors.Invalid):
        passwords.hash_password(pw)
    assert passwords.hash_password("x" * 12) and passwords.hash_password("x" * 128)


def test_created_user_stores_only_the_hash(cdb):
    _, eng = cdb
    u = mk(eng)
    with eng.connect() as c:
        stored = c.execute(text("select password_hash from users")).scalar()
    assert stored == u.password_hash and stored.startswith("$argon2id$") and PW not in stored


def test_authenticate_rehashes_when_parameters_are_outdated(cdb):
    _, eng = cdb
    from argon2 import PasswordHasher

    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PW)
    with Session(eng) as s:
        u = users.create_user(s, "old@example.com", PW, "operator")
        u.password_hash = weak
        s.commit()
        assert passwords.needs_rehash(weak)
        users.authenticate(s, "old@example.com", PW)
        s.commit()
        assert s.get(CentralUser, u.id).password_hash != weak
        assert users.authenticate(s, "old@example.com", PW).id == u.id


# --- login ------------------------------------------------------------------------------------------


def test_valid_login_issues_a_bearer_token(api):
    c, eng = api
    u = mk(eng)
    r = login(c, "  ANA@example.com ")  # email normalizohet edhe në login
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer" and body["expires_in"] == 900
    claims = jwt.decode(
        body["access_token"], SECRET, algorithms=["HS256"], audience=tokens.AUDIENCE
    )
    assert claims["sub"] == str(u.id) and claims["iss"] == tokens.ISSUER
    assert {"exp", "iat", "jti"} <= set(claims) and "role" not in claims and "email" not in claims
    assert claims["exp"] - claims["iat"] == 900


def test_invalid_password_unknown_user_and_disabled_get_the_same_401(api):
    c, eng = api
    u = mk(eng)
    mk(eng, "off@example.com")
    with Session(eng) as s:
        users.disable(s, users.get_by_email(s, "off@example.com").id)
        s.commit()
    responses = [
        login(c, "ana@example.com", "wrong password!!"),
        login(c, "nobody@example.com", PW),
        login(c, "off@example.com", PW),  # fjalëkalim i saktë, por i çaktivizuar
        login(c, "not-an-email", PW),
    ]
    assert {r.status_code for r in responses} == {401}
    assert len({r.text for r in responses}) == 1  # pa dallim që zbulon ekzistencën/statusin
    assert responses[0].json()["detail"]["code"] == "invalid_credentials"
    assert responses[0].headers["www-authenticate"] == "Bearer"
    assert login(c).status_code == 200 and u.status == "active"


def test_login_is_unavailable_without_a_configured_secret(api, monkeypatch):
    c, eng = api
    mk(eng)
    monkeypatch.setattr(settings, "auth_secret", "short")
    r = login(c)
    assert r.status_code == 503 and r.json()["detail"]["code"] == "auth_not_configured"
    assert c.get("/auth/me", headers=bearer("x")).status_code == 503


def test_production_refuses_to_start_without_an_auth_secret(monkeypatch):
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "auth_secret", "")
    with pytest.raises(RuntimeError, match="CENTRAL_AUTH_SECRET"):
        create_app(create_engine("sqlite://"))
    monkeypatch.setattr(settings, "auth_secret", SECRET)
    create_app(create_engine("sqlite://"))


# --- token-at ---------------------------------------------------------------------------------------


def test_missing_and_malformed_tokens_are_401(api):
    c, eng = api
    mk(eng)
    assert c.get("/auth/me").status_code == 401
    for h in ({"Authorization": "Basic abc"}, {"Authorization": "Bearer"}, bearer(""), bearer("garbage"),
              bearer("a.b.c"), bearer("sms_deadbeef_secret")):  # fmt: skip
        r = c.get("/auth/me", headers=h)
        assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer", h


def test_forged_tokens_are_rejected(api):
    c, eng = api
    u = mk(eng)
    now = int(datetime.now(UTC).timestamp())
    good = {
        "iss": tokens.ISSUER,
        "aud": tokens.AUDIENCE,
        "sub": str(u.id),
        "iat": now,
        "exp": now + 600,
    }

    def enc(claims=None, key=SECRET, alg="HS256"):
        return jwt.encode(claims or good, key, algorithm=alg)

    assert c.get("/auth/me", headers=bearer(enc())).status_code == 200  # kontroll pozitiv
    bad = {
        "wrong secret": enc(key="x" * 48),
        "alg none": jwt.encode(good, None, algorithm="none"),
        "other alg": enc(alg="HS512"),
        "wrong aud": enc({**good, "aud": "other"}),
        "wrong iss": enc({**good, "iss": "other"}),
        "no exp": enc({k: v for k, v in good.items() if k != "exp"}),
        "no sub": enc({k: v for k, v in good.items() if k != "sub"}),
        "sub not uuid": enc({**good, "sub": "not-a-uuid"}),
        "unknown user": enc({**good, "sub": str(uuid.uuid4())}),
    }
    for name, t in bad.items():
        assert c.get("/auth/me", headers=bearer(t)).status_code == 401, name


def test_expired_token_is_rejected(api):
    c, eng = api
    u = mk(eng)
    old, _ = tokens.issue(u.id, now=datetime.now(UTC) - timedelta(hours=2), ttl=60)
    assert c.get("/auth/me", headers=bearer(old)).status_code == 401
    fresh, _ = tokens.issue(u.id, ttl=60)
    assert c.get("/auth/me", headers=bearer(fresh)).status_code == 200


def test_token_signed_with_an_enterprise_style_secret_is_not_accepted(api, monkeypatch):
    c, eng = api
    u = mk(eng)
    t, _ = tokens.issue(u.id)
    monkeypatch.setattr(settings, "auth_secret", "e" * 48)  # sekret tjetër = çelës tjetër
    assert c.get("/auth/me", headers=bearer(t)).status_code == 401


# --- /auth/me dhe RBAC -------------------------------------------------------------------------------


def test_current_user_endpoint_is_protected_and_returns_the_user(api):
    c, eng = api
    u = mk(eng)
    assert c.get("/auth/me").status_code == 401
    r = c.get("/auth/me", headers=bearer(token_for(c)))
    assert r.status_code == 200
    assert r.json() == {
        "id": str(u.id),
        "email": "ana@example.com",
        "role": "admin",
        "status": "active",
    }
    assert "password" not in r.text and "$argon2" not in r.text


def test_admin_probe_allows_admin_denies_operator_and_anonymous(api):
    c, eng = api
    mk(eng, "adm@example.com", role="admin")
    mk(eng, "op@example.com", role="operator")
    assert c.get("/admin/ping").status_code == 401
    r = c.get("/admin/ping", headers=bearer(token_for(c, "adm@example.com")))
    assert r.status_code == 200 and r.json() == {"status": "ok", "role": "admin"}
    r = c.get("/admin/ping", headers=bearer(token_for(c, "op@example.com")))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "forbidden"
    op = c.get("/auth/me", headers=bearer(token_for(c, "op@example.com")))
    assert op.status_code == 200 and op.json()["role"] == "operator"  # operator hyn te /me


def test_status_and_role_are_read_from_the_database_on_every_request(api):
    c, eng = api
    u = mk(eng, role="operator")
    t = token_for(c)
    assert c.get("/admin/ping", headers=bearer(t)).status_code == 403
    with Session(eng) as s:  # promovim → vlen menjëherë, i njëjti token
        s.get(CentralUser, u.id).role = "admin"
        s.commit()
    assert c.get("/admin/ping", headers=bearer(t)).status_code == 200
    with Session(eng) as s:  # çaktivizim → token ekzistues refuzohet menjëherë
        users.disable(s, u.id)
        s.commit()
    assert c.get("/auth/me", headers=bearer(t)).status_code == 401
    assert login(c).status_code == 401
    with Session(eng) as s:
        users.enable(s, u.id)
        s.commit()
    assert c.get("/auth/me", headers=bearer(t)).status_code == 200  # i njëjti token ende i vlefshëm


def test_require_role_is_an_explicit_dependency_not_a_policy_engine():
    from apps.central.api.deps import require_role

    assert [r.value for r in Role] == ["admin", "operator"]
    assert [s.value for s in UserStatus] == ["active", "disabled"]
    dep = require_role(Role.ADMIN)
    assert callable(dep)


def test_central_exposes_no_registration_and_no_business_endpoints(api):
    c, _ = api
    paths = set(c.app.openapi()["paths"])
    assert paths == {"/healthz", "/readyz", "/auth/token", "/auth/me", "/admin/ping"}
    assert not [
        p for p in paths if "regist" in p or "signup" in p or "enterprise" in p or "product" in p
    ]


# --- sekrete jo në log ---------------------------------------------------------------------------------


def test_logs_contain_neither_passwords_hashes_nor_tokens(api, caplog):
    c, eng = api
    mk(eng)
    caplog.set_level(logging.DEBUG)
    ok = login(c)
    login(c, "ana@example.com", "wrong-password-xyz")
    login(c, "ghost@example.com", "ghost-password-xyz")
    c.get("/auth/me", headers=bearer(ok.json()["access_token"]))
    text_ = caplog.text
    assert "login ok user=" in text_ and "login failed reason=bad_password" in text_
    assert "reason=unknown_user" in text_
    for secret in (PW, "wrong-password-xyz", "ghost-password-xyz", ok.json()["access_token"],
                   "$argon2", SECRET):  # fmt: skip
        assert secret not in text_


# --- bootstrap admin -------------------------------------------------------------------------------------


def _cli(url, *args, password=PW, extra_env=None):
    env = {**os.environ, "CENTRAL_DATABASE_URL": url, **(extra_env or {})}
    env.pop("CENTRAL_ADMIN_PASSWORD", None)
    if password is not None:
        env["CENTRAL_ADMIN_PASSWORD"] = password
    return subprocess.run(
        [sys.executable, "-m", "apps.central.tools.create_admin", *args],
        env=env, cwd=ROOT, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )  # fmt: skip


def test_create_admin_creates_is_idempotent_and_fails_clean_on_conflict(cdb):
    url, eng = cdb
    r = _cli(url, "--email", " Boss@Example.com ")
    assert r.returncode == 0 and "created admin boss@example.com" in r.stdout
    with Session(eng) as s:
        u = users.get_by_email(s, "boss@example.com")
        assert (
            u.role == "admin" and u.status == "active" and u.password_hash.startswith("$argon2id$")
        )
        h = u.password_hash
    again = _cli(url, "--email", "boss@example.com")  # rerun me të njëjtat → no-op
    assert again.returncode == 0 and "already exists (unchanged)" in again.stdout
    other_pw = _cli(url, "--email", "boss@example.com", password="another long password")
    other_role = _cli(url, "--email", "boss@example.com", "--role", "operator")
    for r in (other_pw, other_role):
        assert r.returncode == 1 and "no changes made" in r.stderr
    with Session(eng) as s:
        assert users.get_by_email(s, "boss@example.com").password_hash == h  # i paprekur
        assert s.query(CentralUser).count() == 1


def test_create_admin_never_prints_secrets_and_has_no_default_password(cdb):
    url, _ = cdb
    pw = "super secret passphrase 123"
    r = _cli(url, "--email", "a@example.com", password=pw)
    conflict = _cli(url, "--email", "a@example.com", password="different passphrase 456")
    for out in (r.stdout + r.stderr, conflict.stdout + conflict.stderr):
        assert pw not in out and "different passphrase" not in out and "$argon2" not in out
        assert url not in out
    no_pw = _cli(url, "--email", "b@example.com", password=None)  # pa env, pa terminal
    assert no_pw.returncode == 2 and "CENTRAL_ADMIN_PASSWORD" in no_pw.stderr
    short = _cli(url, "--email", "b@example.com", password="short")
    assert short.returncode == 2 and "password must be" in short.stderr
    bad_email = _cli(url, "--email", "nope", password=PW)
    assert bad_email.returncode == 2
    assert subprocess.run([sys.executable, "-m", "apps.central.tools.create_admin", "--help"],
                          cwd=ROOT, capture_output=True, text=True).returncode == 0  # fmt: skip
    src = (ROOT / "apps/central/tools/create_admin.py").read_text()
    assert "--password" not in src  # kurrë nga argumentet e komandës


# --- migrim, readiness, izolim ---------------------------------------------------------------------------


def test_migration_0003_up_down_up_and_readiness(make_db):  # noqa: F811
    url = make_db()
    eng = create_engine(url)
    c = TestClient(create_app(eng))
    central_alembic(url, "upgrade", "0002")
    r = c.get("/readyz")
    assert r.status_code == 503 and "not at the expected version" in r.json()["reason"]  # prapa
    central_alembic(url, "upgrade", "head")
    assert c.get("/readyz").json() == {"status": "ready"}
    assert {"users", "enterprises"} <= set(inspect(eng).get_table_names())
    central_alembic(url, "downgrade", "0002")
    tables = set(inspect(eng).get_table_names())
    assert "users" not in tables and "enterprises" in tables  # 0002 i paprekur
    assert c.get("/readyz").status_code == 503
    central_alembic(url, "upgrade", "head")
    assert c.get("/readyz").status_code == 200


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_users_schema_matches_metadata_and_stays_in_central_db(make_db):  # noqa: F811
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    ent, cen = make_db("ent"), make_db("central")
    if not ent.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    enterprise_alembic(ent, "upgrade", "head")
    central_alembic(cen, "upgrade", "head")
    with create_engine(cen).connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "version_table": "central_alembic_version"}
        )
        assert compare_metadata(ctx, Base.metadata) == []
    ent_tables = set(inspect(create_engine(ent)).get_table_names())
    cen_tables = set(inspect(create_engine(cen)).get_table_names())
    assert "users" not in ent_tables and "enterprises" not in ent_tables
    assert cen_tables == {"central_alembic_version", "enterprises", "users"}
    assert not any(t.startswith("sms_") for t in cen_tables)


# --- kufijtë (AST) -------------------------------------------------------------------------------------------


def _imports(path):
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.ImportFrom) and n.module:
            yield n.module
        elif isinstance(n, ast.Import):
            yield from (a.name for a in n.names)


def test_central_auth_imports_nothing_from_enterprise_and_shares_no_secrets():
    for f in (ROOT / "apps/central").rglob("*.py"):
        for m in _imports(f):
            assert m != "app" and not m.startswith("app."), (f, m)
    src = "".join(f.read_text() for f in (ROOT / "apps/central").rglob("*.py"))
    assert "SMS_" not in src and "admin_api_key" not in src and "ApiKey" not in src
    from apps.central.core.config import Settings

    assert not [k for k in Settings.model_fields if k.startswith("sms_")]


def test_central_runtime_never_imports_the_cli_tools():
    for f in (ROOT / "apps/central").rglob("*.py"):
        if "tools" in f.parts:
            continue
        for m in _imports(f):
            assert "apps.central.tools" not in m, (f, m)


def test_session_dependency_uses_the_apps_own_engine(cdb):
    url, eng = cdb
    other = create_engine("sqlite:///" + str(ROOT / "nonexistent-dir" / "x.db"))
    c = TestClient(create_app(eng))
    mk(eng)
    assert token_for(c)  # app lidhet me engine-in e injektuar, jo me atë global
    assert c.app.state.engine is eng and sessionmaker(bind=other)
