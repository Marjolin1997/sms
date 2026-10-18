"""M11-I/F-02: IP e klientit pas proxy-ve të besuara: helper i përbashkët, Enterprise, Central login/internal; HSTS jo nga aplikacioni.

Modeli i synuar: nginx normalizon `X-Forwarded-For` në NJË element (real_ip) dhe `hops=1` e merr atë; `hops=0` e injoron header-in.
"""

import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import apps.central.models  # noqa: F401  (regjistron tabelat)
from apps.central.core.config import settings as central_settings
from apps.central.core.db import Base
from apps.central.main import create_app
from packages import netutil

# --- helper-i i përbashkët (pa framework) ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("peer", "xff", "hops", "expected"),
    [
        ("10.0.0.10", "1.2.3.4", 0, "10.0.0.10"),  # hops=0: header-i injorohet
        ("10.0.0.10", "6.6.6.6, 1.2.3.4", 0, "10.0.0.10"),
        ("10.0.0.10", "1.2.3.4", 1, "1.2.3.4"),  # nginx i normalizuar: një element
        ("10.0.0.10", "6.6.6.6, 1.2.3.4", 1, "1.2.3.4"),  # elementi i majtë i falsifikuar injorohet
        ("10.0.0.10", "6.6.6.6, 7.7.7.7, 1.2.3.4", 1, "1.2.3.4"),
        ("10.0.0.10", "", 1, "10.0.0.10"),  # pa header: IP e lidhjes
        ("10.0.0.10", " , ,", 1, "10.0.0.10"),
        ("10.0.0.10", "1.2.3.4", 2, "10.0.0.10"),  # varg i pamjaftueshëm: kurrë një element i majtë
        (
            "10.0.0.10",
            "6.6.6.6, 1.2.3.4",
            2,
            "6.6.6.6",
        ),  # (hops>1 vetëm kur numri përputhet saktë; jo model i synuar)
        (None, "1.2.3.4", 1, "1.2.3.4"),
        (None, "", 1, None),
    ],
)
def test_netutil_client_ip(peer, xff, hops, expected):
    assert netutil.client_ip(peer, xff, hops) == expected


def test_netutil_truncates_to_ipv6_length():
    assert len(netutil.client_ip("x" * 100, "", 0)) == 45
    assert len(netutil.client_ip(None, "y" * 100, 1)) == 45


# --- Enterprise --------------------------------------------------------------------------------------------------


def test_enterprise_client_ip_uses_the_shared_logic(client, db, monkeypatch):
    from sqlalchemy import select

    from app.core.config import settings
    from app.models.admin import AuthFailure

    bad = {"Authorization": "Bearer sms_deadbeef_wrong", "X-Forwarded-For": "6.6.6.6, 1.2.3.4"}
    client.get("/v1/me", headers=bad)
    assert {f.ip for f in db.scalars(select(AuthFailure))} == {"testclient"}  # hops=0
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    client.get("/v1/me", headers=bad)
    assert {f.ip for f in db.scalars(select(AuthFailure))} == {
        "testclient",
        "1.2.3.4",
    }  # hops=1: spoof majtas injorohet
    monkeypatch.setattr(settings, "trusted_proxy_hops", 3)
    client.get(
        "/v1/me",
        headers={"Authorization": "Bearer sms_deadbeef_wrong", "X-Forwarded-For": "1.1.1.1"},
    )
    assert "1.1.1.1" not in {f.ip for f in db.scalars(select(AuthFailure))}  # varg i pamjaftueshëm


def test_enterprise_app_does_not_emit_hsts_but_keeps_its_other_security_headers(client):
    r = client.get("/healthz")
    low = {k.lower(): v for k, v in r.headers.items()}
    assert "strict-transport-security" not in low  # HSTS e zotëron shtresa e TLS
    assert low["x-content-type-options"] == "nosniff" and low["referrer-policy"] == "no-referrer"
    assert (
        low["cache-control"] == "no-store"
        and "frame-ancestors 'none'" in low["content-security-policy"]
    )


# --- Central login/internal: CENTRAL_TRUSTED_PROXY_HOPS ka efekt real -----------------------------------------------


@pytest.fixture
def central(monkeypatch):
    eng = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(eng)
    monkeypatch.setattr(central_settings, "auth_secret", "s" * 40)
    yield TestClient(create_app(eng))
    eng.dispose()


def _ips(caplog, marker):
    return [
        r.getMessage().split("ip=")[1].split()[0]
        for r in caplog.records
        if marker in r.getMessage() and "ip=" in r.getMessage()
    ]


@pytest.mark.parametrize(
    ("hops", "xff", "expected"),
    [
        (0, "6.6.6.6, 1.2.3.4", "testclient"),
        (1, "1.2.3.4", "1.2.3.4"),  # nginx-i i normalizuar
        (1, "6.6.6.6, 1.2.3.4", "1.2.3.4"),  # i majti i falsifikuar injorohet
        (1, "", "testclient"),
        (2, "1.2.3.4", "testclient"),  # varg i pamjaftueshëm
    ],
)
def test_central_login_logs_the_configured_client_ip(
    central, monkeypatch, caplog, hops, xff, expected
):
    monkeypatch.setattr(central_settings, "trusted_proxy_hops", hops)
    headers = {"X-Forwarded-For": xff} if xff else {}
    with caplog.at_level(logging.INFO):
        r = central.post(
            "/auth/token",
            json={"email": "nobody@example.com", "password": "x" * 12},
            headers=headers,
        )
    assert r.status_code == 401
    assert _ips(caplog, "login failed") == [expected]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/internal/sync/changes"),
        ("GET", "/internal/money/state"),
        ("GET", "/internal/pricing/state"),
        ("POST", "/internal/billing/usage-reports"),
        ("GET", "/internal/sender/changes"),
        ("POST", "/internal/sender/requests"),
    ],
)
def test_central_internal_denials_log_the_configured_client_ip(
    central, monkeypatch, caplog, method, path
):
    monkeypatch.setattr(central_settings, "trusted_proxy_hops", 1)
    headers = {"Authorization": "Bearer not-a-token", "X-Forwarded-For": "6.6.6.6, 1.2.3.4"}
    with caplog.at_level(logging.INFO):
        central.request(method, path, headers=headers, json={} if method == "POST" else None)
    msgs = [r.getMessage() for r in caplog.records if "ip=" in r.getMessage()]
    assert msgs, (method, path)
    assert all("ip=1.2.3.4" in m for m in msgs), msgs


def test_central_registration_ip_helper_matches_the_shared_derivation(monkeypatch):
    from starlette.requests import Request

    from apps.central.api import registration_public as rp

    def req(xff):
        h = [(b"x-forwarded-for", xff.encode())] if xff else []
        return Request({"type": "http", "headers": h, "client": ("10.0.0.10", 1234)})

    monkeypatch.setattr(central_settings, "trusted_proxy_hops", 1)
    assert rp.client_ip(req("6.6.6.6, 1.2.3.4")) == "1.2.3.4"
    monkeypatch.setattr(central_settings, "trusted_proxy_hops", 0)
    assert rp.client_ip(req("6.6.6.6, 1.2.3.4")) == "10.0.0.10"
