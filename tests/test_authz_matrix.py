"""Matrica e autorizimit, e nxjerrë automatikisht nga rrugët: asnjë endpoint i ri nuk mund të
dalë pa autentikim, dhe çdo rol pa lejen e kërkuar merr 403 (jo të dhëna)."""

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.core.security import ROLE_PERMS, current_principal
from app.main import create_app

BOOT = {"X-Admin-Key": "test-key"}

# Pa autentikim me çelës API, me qëllim: probat, faqet publike dhe callback-et e provider-ave
# (këto verifikojnë nënshkrimin HMAC ose token-in vetë).
PUBLIC = {"/healthz", "/readyz", "/u/{token}"}
SIGNED = re.compile(
    r"^/webhooks/((dlr|email|inbound|payments)/\{provider\}|twilio/(status|inbound))$"
)


def _deps(dependant):
    for d in dependant.dependencies:
        yield d.call
        yield from _deps(d)


def _flatten(routes):
    """FastAPI ≥0.14x mban include_router si objekte `_IncludedRouter` (të ngngarkuara dhe pa
    prefiks/varësi shtesë te main.py); i hapim me router-in origjinal."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, "original_router"):
            yield from _flatten(r.original_router.routes)


def _routes():
    app = create_app()
    routes = list(_flatten(app.routes))
    assert len(routes) > 100, "route discovery broke: the matrix would pass vacuously"
    return app, routes


def _perms(route):
    for call in _deps(route.dependant):
        if hasattr(call, "perms"):
            return call.perms
    return None


def _url(path):
    return re.sub(r"\{[^}]+\}", "1", path)


@pytest.fixture(scope="module")
def world():
    app, routes = _routes()
    return TestClient(app), routes


def test_every_private_route_requires_authentication(world):
    _, routes = world
    open_routes = []
    for r in routes:
        if r.path in PUBLIC or SIGNED.match(r.path):
            continue
        if current_principal not in set(_deps(r.dependant)):
            open_routes.append(f"{sorted(r.methods)} {r.path}")
    assert open_routes == []  # çdo rrugë tjetër duhet të varet nga current_principal


def test_every_v1_route_declares_a_permission_except_self_service_reads(world):
    _, routes = world
    allowed_without_perm = {"/v1/me", "/v1/openapi.json", "/v1/postman.json",
                            "/v1/me/2fa/enroll", "/v1/me/2fa/confirm"}  # fmt: skip
    missing = [
        f"{sorted(r.methods)} {r.path}"
        for r in routes
        if r.path.startswith("/v1/") and _perms(r) is None and r.path not in allowed_without_perm
    ]
    assert missing == []


def test_unauthenticated_requests_get_401(world):
    c, routes = world
    for r in routes:
        if r.path in PUBLIC or SIGNED.match(r.path):
            continue
        for m in r.methods - {"HEAD", "OPTIONS"}:
            resp = c.request(m, _url(r.path), headers={"X-Admin-Key": ""})
            assert resp.status_code == 401, f"{m} {r.path} -> {resp.status_code}"


def test_signed_callbacks_reject_unsigned_requests(world):
    c, routes = world
    for r in routes:
        if SIGNED.match(r.path):
            resp = c.post(_url(r.path), content=b"{}", headers={"X-Admin-Key": ""})
            assert resp.status_code == 401, f"{r.path} -> {resp.status_code}"


def _key(c, role):
    body = {"name": role, "role": role} | ({"owner_ref": "acme"} if role == "client" else {})
    r = c.post("/v1/admin/api-keys", json=body, headers=BOOT)
    return {"Authorization": f"Bearer {r.json()['key']}", "X-Admin-Key": ""}


@pytest.mark.parametrize("role", ["client", "support", "approver", "pricing", "finance"])
def test_roles_without_the_permission_get_403(world, role):
    c, routes = world
    h = _key(c, role)
    granted = ROLE_PERMS[role]
    checked = 0
    for r in routes:
        perms = _perms(r)
        if not perms or "*" in granted or any(p in granted for p in perms):
            continue
        for m in r.methods - {"HEAD", "OPTIONS"}:
            resp = c.request(
                m, _url(r.path), headers=h, json={} if m in {"POST", "PUT", "PATCH"} else None
            )
            assert resp.status_code == 403, (
                f"{role}: {m} {r.path} (needs {perms}) -> {resp.status_code}"
            )
            checked += 1
    assert checked > 20  # sigurohu që testi vërtet ushtron rrugë


def test_client_keys_cannot_reach_staff_only_routes_even_with_owner_param(world):
    c, _ = world
    h = _key(c, "client")
    for path in ["/v1/admin/api-keys", "/v1/admin/accounts", "/v1/admin/audit", "/v1/topups",
                 "/v1/rate-cards", "/v1/admin/switches"]:  # fmt: skip
        assert c.get(path, headers=h, params={"owner_ref": "acme"}).status_code == 403, path
