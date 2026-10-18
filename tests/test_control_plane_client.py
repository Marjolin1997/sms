"""M7-e: klienti i Control Plane (auth Ed25519, transport, gabime), konfigurimi dhe kufijt."""

import ast
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from sqlalchemy import select

from app.core.config import Settings
from app.core.db import SessionLocal
from app.models.control_plane import CpCursor, Entitlement
from app.services import control_plane_client as cc
from app.services import control_plane_poller as poller
from app.services import control_plane_sync as cps

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
SERVICES = ("control_plane_client", "control_plane_poller", "control_plane_sync",
            "control_plane_shadow")  # fmt: skip


def pem_private(key=None) -> bytes:
    key = key or ed25519.Ed25519PrivateKey.generate()
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


@pytest.fixture
def key():
    return ed25519.Ed25519PrivateKey.generate()


@pytest.fixture
def cfg(key):
    return cc.ControlPlaneConfig("http://central.test", "ent-main", "k1", key, 5.0)


def client_with(cfg, handler):
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return cc.ControlPlaneClient(cfg, httpx.Client(transport=httpx.MockTransport(wrapped))), seen


# --- assertion -------------------------------------------------------------------------------


def test_assertion_is_valid_eddsa_with_exact_claims_and_kid(cfg, key):
    now = datetime(2030, 1, 1, tzinfo=UTC)
    tok = cc.make_assertion(cfg, now)
    hdr = jwt.get_unverified_header(tok)
    assert hdr["alg"] == "EdDSA" and hdr["kid"] == "k1"
    claims = jwt.decode(
        tok, key.public_key(), algorithms=["EdDSA"], audience="sms-central-sync",
        options={"verify_exp": False, "verify_iat": False},
    )  # fmt: skip
    assert claims["iss"] == claims["sub"] == "ent-main"
    assert claims["aud"] == "sms-central-sync" and claims["scope"].split() == ["sync:read"]
    assert claims["iat"] == int(now.timestamp())
    assert 0 < claims["exp"] - claims["iat"] <= 300  # kufiri i Central
    assert 8 <= len(claims["jti"]) <= 64


def test_every_assertion_has_a_unique_jti_and_every_request_gets_a_fresh_one(cfg):
    assert len({jwt.decode(cc.make_assertion(cfg), options={"verify_signature": False})["jti"]
                for _ in range(50)}) == 50  # fmt: skip
    c, seen = client_with(cfg, lambda r: httpx.Response(200, json={"epoch": str(uuid.uuid4())}))
    for _ in range(3):
        c.get_snapshot()
    jtis = {
        jwt.decode(r.headers["authorization"][7:], options={"verify_signature": False})["jti"]
        for r in seen
    }
    assert len(seen) == 3 and len(jtis) == 3


def test_private_key_never_appears_in_repr_logs_or_errors(cfg, key, tmp_path, caplog):
    secret_b64 = pem_private(key).decode().splitlines()[1]
    assert secret_b64 not in repr(cfg) and "private" not in repr(cfg).lower()
    caplog.set_level(logging.DEBUG)
    c, _ = client_with(cfg, lambda r: httpx.Response(401, json={}))
    with pytest.raises(cc.CpAuthError) as ex:
        c.get_snapshot()
    assert secret_b64 not in str(ex.value) and "Bearer" not in str(ex.value)
    assert secret_b64 not in caplog.text and "Bearer" not in caplog.text
    p = tmp_path / "bad.pem"
    p.write_bytes(
        b"-----BEGIN PRIVATE KEY-----\n"
        + secret_b64[:-8].encode()
        + b"\n-----END PRIVATE KEY-----\n"
    )
    with pytest.raises(cc.ConfigError) as ex2:
        cc.load_private_key(str(p))
    assert secret_b64 not in str(ex2.value)


# --- konfigurimi / çelësi --------------------------------------------------------------------


def settings(**kw):
    base = dict(cp_sync_mode="shadow", cp_base_url="https://central.test", cp_client_id="c",
                cp_key_id="k", cp_private_key_path="")  # fmt: skip
    return Settings(**{**base, **kw}, _env_file=None)


def test_valid_key_file_loads_and_config_is_built(tmp_path):
    p = tmp_path / "k.pem"
    p.write_bytes(pem_private())
    cfg = cc.config_from_settings(
        settings(cp_private_key_path=str(p), cp_base_url="https://x.test/")
    )
    assert cfg.base_url == "https://x.test" and cfg.client_id == "c" and cfg.key_id == "k"
    assert isinstance(cfg.private_key, ed25519.Ed25519PrivateKey)


@pytest.mark.parametrize("kind", ["missing", "public_only", "rsa", "encrypted", "garbage", "empty"])
def test_wrong_key_configuration_fails_safely(tmp_path, kind):
    p = tmp_path / "k.pem"
    k = ed25519.Ed25519PrivateKey.generate()
    if kind == "missing":
        path = str(tmp_path / "nope.pem")
    else:
        path = str(p)
        p.write_bytes(
            {
                "public_only": k.public_key().public_bytes(
                    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
                ),
                "rsa": pem_private(rsa.generate_private_key(65537, 2048)),
                "encrypted": k.private_bytes(
                    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                    serialization.BestAvailableEncryption(b"pw"),
                ),
                "garbage": b"not a key",
                "empty": b"",
            }[kind]
        )  # fmt: skip
    with pytest.raises(cc.ConfigError):
        cc.load_private_key(path if kind != "empty" else path)
    s = settings(cp_private_key_path=path)
    with pytest.raises(cc.ConfigError):
        cc.config_from_settings(s)


def test_missing_identity_or_url_and_production_https_are_rejected(tmp_path):
    p = tmp_path / "k.pem"
    p.write_bytes(pem_private())
    for field in ("cp_base_url", "cp_client_id", "cp_key_id"):
        with pytest.raises(cc.ConfigError, match="missing"):
            cc.config_from_settings(settings(cp_private_key_path=str(p), **{field: ""}))
    with pytest.raises(cc.ConfigError, match="https"):
        cc.config_from_settings(
            settings(cp_private_key_path=str(p), cp_base_url="http://x.test", env="production")
        )
    bad = settings(cp_base_url="http://x.test", env="production").production_problems()
    assert any("SMS_CP_BASE_URL" in b for b in bad)
    assert not any("SMS_CP_" in b for b in Settings(_env_file=None).production_problems())


def test_mode_is_off_shadow_or_enforce_default_off_and_no_key_is_generated():
    with pytest.raises(ValueError):
        Settings(cp_sync_mode="bogus", _env_file=None)
    assert Settings(_env_file=None).cp_sync_mode == "off"  # default: asnjë deployment s'ndryshon
    assert Settings(cp_sync_mode="enforce", _env_file=None).cp_sync_mode == "enforce"
    src = (APP / "services/control_plane_client.py").read_text()
    assert "generate(" not in src  # asnjë çelës i gjeneruar në nisje


# --- HTTP: gabime dhe politika ---------------------------------------------------------------


@pytest.mark.parametrize(
    "status,exc",
    [(401, cc.CpAuthError), (403, cc.CpForbidden), (500, cc.CpTransportError),
     (503, cc.CpTransportError), (429, cc.CpTransportError), (422, cc.CpProtocolError)],
)  # fmt: skip
def test_status_mapping(cfg, status, exc):
    c, seen = client_with(cfg, lambda r: httpx.Response(status, json={}))
    with pytest.raises(exc):
        c.get_snapshot()
    assert len(seen) == 1  # asnjë riprovim brenda thirrjes
    assert all(r.headers["authorization"].startswith("Bearer ") for r in seen)  # asnjëherë anonim


@pytest.mark.parametrize(
    "status,code",
    [(409, "sync_epoch_mismatch"), (409, "sync_authorization_changed"),
     (409, "sync_cursor_ahead"), (410, "sync_cursor_expired")],
)  # fmt: skip
def test_snapshot_required_codes(cfg, status, code):
    body = {"detail": {"code": code, "message": "x", "action": "snapshot"}}
    c, _ = client_with(cfg, lambda r: httpx.Response(status, json=body))
    with pytest.raises(cc.CpSnapshotRequired) as ex:
        c.get_changes(0, uuid.uuid4(), 1)
    assert ex.value.code == code and ex.value.status == status


def test_unknown_409_and_bad_json_and_malformed_wrapper_are_protocol_errors(cfg):
    c, _ = client_with(cfg, lambda r: httpx.Response(409, json={"detail": {"code": "conflict"}}))
    with pytest.raises(cc.CpProtocolError):
        c.get_snapshot()
    c, _ = client_with(cfg, lambda r: httpx.Response(200, content=b"<html>"))
    with pytest.raises(cc.CpProtocolError):
        c.get_snapshot()
    for body in ({}, {"events": []}, {"events": [], "has_more": "no"}):
        c, _ = client_with(cfg, lambda r, b=body: httpx.Response(200, json=b))
        with pytest.raises(cc.CpProtocolError):
            c.get_changes(0, uuid.uuid4(), 1)


def test_network_failures_map_to_transport_error(cfg):
    def boom(request):
        raise httpx.ConnectTimeout("timeout", request=request)

    c, _ = client_with(cfg, boom)
    with pytest.raises(cc.CpTransportError):
        c.get_snapshot()


def test_changes_page_is_parsed_and_uses_the_wrapper_cursor(cfg):
    ep = uuid.uuid4()
    body = {"epoch": str(ep), "authorization_generation": 3, "events": [], "next_seq": 77,
            "latest_seq": 77, "has_more": False, "oldest_available_seq": 1}  # fmt: skip
    c, seen = client_with(cfg, lambda r: httpx.Response(200, json=body))
    page = c.get_changes(5, ep, 3, 50)
    assert (page.epoch, page.authorization_generation, page.next_seq) == (ep, 3, 77)
    q = dict(seen[0].url.params)
    assert q == {"after_seq": "5", "epoch": str(ep), "generation": "3", "limit": "50"}


# --- poller: dështime që mbajnë gjendjen lokale (fail-static) ---------------------------------


def mk_state(db):
    from app.models.enterprise import Enterprise

    e = Enterprise(owner_ref="o", legal_name="L")
    db.add(e)
    db.commit()
    aid = uuid.uuid4()
    snap = {
        "epoch": str(uuid.uuid4()), "authorization_generation": 1, "snapshot_seq": 10,
        "enterprises": [{"entity": {"type": "enterprise", "id": str(e.id)}, "enterprise_id": str(e.id),
                         "revision": 2, "data": {"id": str(e.id), "name": "N", "status": "active"}}],
        "assignments": [{"entity": {"type": "enterprise_product", "id": str(aid)},
                         "enterprise_id": str(e.id), "revision": 2,
                         "data": {"assignment_id": str(aid), "enterprise_id": str(e.id),
                                  "product": {"id": str(uuid.uuid4()), "code": "sms_std", "channel": "sms"},
                                  "status": "active"}}],
    }  # fmt: skip
    cps.apply_snapshot(db, cps.parse_snapshot(snap), now=datetime(2030, 1, 1, tzinfo=UTC))
    db.commit()
    return e


def state_fingerprint(db):
    cur = db.scalar(select(CpCursor))
    db.refresh(cur)
    return (
        cur.epoch, cur.authorization_generation, cur.last_seq, cur.last_success_at,
        sorted((x.assignment_id, x.status, x.revision) for x in db.scalars(select(Entitlement))),
    )  # fmt: skip


@pytest.mark.parametrize(
    "handler,kind",
    [
        (lambda r: httpx.Response(401, json={}), "auth_error"),
        (lambda r: httpx.Response(403, json={}), "forbidden"),
        (lambda r: httpx.Response(503, json={}), "network_error"),
        (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("t", request=r)), "network_error"),
    ],
)
def test_central_failures_keep_local_state_and_make_a_single_request(db, cfg, handler, kind):
    mk_state(db)
    before = state_fingerprint(db)
    c, seen = client_with(cfg, handler)
    out = poller.poll_once(
        SessionLocal, c, snapshot_interval_s=10**9, now=datetime(2030, 1, 1, 0, 1, tzinfo=UTC)
    )
    assert out.kind == kind and not out.ok
    assert len(seen) == 1  # asnjë riprovim brenda iteracionit
    assert state_fingerprint(db) == before  # last_success_at s'u përditësua, asgjë s'u çaktivizua


# --- backoff / cikli -------------------------------------------------------------------------


def test_backoff_is_exponential_capped_jittered_and_resets():
    import random

    b = poller.Backoff(rng=random.Random(1))
    raw = [1, 2, 4, 8, 16, 30, 30, 30]
    for r in raw:
        d = b.next()
        assert r * 0.5 <= d <= r
    b.reset()
    assert 0.5 <= b.next() <= 1.0


def _loop(monkeypatch, outcomes, **kw):
    """Ekzekuton run_loop me poll të simuluar dhe regjistron vonesat (pa fjetur realisht)."""
    import threading

    delays, stop = [], threading.Event()
    it = iter(outcomes)

    def poll(*a, **k):
        try:
            return next(it)
        except StopIteration:
            stop.set()
            return poller.PollOutcome()

    monkeypatch.setattr(poller, "_sleep", lambda s, d, t: delays.append(d))
    monkeypatch.setattr(poller, "check_staleness", lambda f, now=None: 0.0)
    poller.run_loop(lambda: None, None, poll_interval_s=30, snapshot_interval_s=3600, stop=stop,
                    poll=poll, **kw)  # fmt: skip
    return delays


def test_401_retries_with_bounded_backoff_and_returns_to_interval_after_success(
    monkeypatch, caplog
):
    caplog.set_level(logging.ERROR)
    bad = poller.PollOutcome(kind="auth_error")
    delays = _loop(monkeypatch, [bad] * 7 + [poller.PollOutcome()])
    assert all(d <= 30 for d in delays[:7]) and delays[0] <= 1.0 and delays[5] > 8
    assert delays[7] == 30  # kthim te intervali i konfiguruar pas suksesit
    assert "ALERT" in caplog.text  # alarm pas pragut


def test_403_is_not_retried_aggressively(monkeypatch):
    delays = _loop(monkeypatch, [poller.PollOutcome(kind="forbidden")] * 2)
    assert delays[:2] == [poller.FORBIDDEN_DELAY_S] * 2 and poller.FORBIDDEN_DELAY_S >= 60


def test_graceful_shutdown_stops_the_loop_promptly():
    import threading
    import time

    stop = threading.Event()
    calls = []
    t = threading.Thread(
        target=poller.run_loop, args=(lambda: None, None),
        kwargs=dict(poll_interval_s=3600, snapshot_interval_s=3600, stop=stop,
                    poll=lambda *a, **k: calls.append(1) or poller.PollOutcome()),
    )  # fmt: skip
    import app.services.control_plane_poller as pl

    orig = pl.check_staleness
    pl.check_staleness = lambda f, now=None: 0.0
    try:
        t.start()
        time.sleep(0.3)
        stop.set()
        t.join(5)
    finally:
        pl.check_staleness = orig
    assert not t.is_alive() and calls == [1]


# --- kufij / siguri ----------------------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    out = set()
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.Import):
            out |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            out.add(n.module)
    return out


def test_enterprise_uses_packages_contract_and_never_apps_central():
    for f in APP.rglob("*.py"):
        assert not any(m.split(".")[0] == "apps" for m in _imports(f)), f
    assert any(m.startswith("packages.contracts.control_plane") for m in _imports(
        APP / "services/control_plane_sync.py"))  # fmt: skip


def test_client_has_no_db_models_or_central_orm():
    mods = _imports(APP / "services/control_plane_client.py")
    assert not [
        m for m in mods if m.startswith(("app.models", "app.core.db", "sqlalchemy", "apps"))
    ]
    assert "httpx" in mods


def test_no_cp_module_touches_accountplan_rate_limits_or_enforces():
    for name in SERVICES:
        src = (APP / "services" / f"{name}.py").read_text()
        names = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Attribute)}
        assert not names & {"AccountPlan"}, name
        # `rate_limit_per_min` mbetet fushë e entitlement-it/gjendjes CP (M7-g), jo e AccountPlan
        assert not attrs & {"email_rate_limit_per_min", "rate_card_id", "enabled"}, name
        assert "sms_account_plans" not in src, name
    for name in (
        "messages",
        "emails",
    ):  # M7-g: i vetmi lidhje me CP në submit është `entitlements.gate`
        tree = ast.parse((APP / "services" / f"{name}.py").read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") == "gate"]  # fmt: skip
        assert len(calls) == 1, name
        assert "control_plane_shadow" not in _imports(APP / "services" / f"{name}.py"), name


def test_private_key_is_never_persisted_in_the_enterprise_schema():
    from app.core.db import Base

    banned = ("private", "pem", "secret", "token", "jwt")
    for t in ("sms_cp_cursor", "sms_entitlements"):
        assert not [c.name for c in Base.metadata.tables[t].c if any(b in c.name for b in banned)]
    src = (APP / "services/control_plane_poller.py").read_text()
    assert "private_key" not in src and "db.add(" not in src
