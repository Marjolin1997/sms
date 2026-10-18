"""M11-H · RBAC + auth/sesion kundër uvicorn REAL + PostgreSQL (zbatim në backend, jo vetëm fshehje në UI).

    . $PILOT_DIR/pilot.env && python -m e2e.pilot.rbac_auth

Enterprise: çdo rrugë me leje (nxjerrë nga aplikacioni) × çdo rol → rol pa lejen merr 403; roli me lejen nuk merr 401/403; pa çelës → 401.
Central: operator → çdo metodë shkrimi nën /admin = 403 dhe çdo lexim i lejuar; admin lexon; pa token → 401.
Auth: çelës i pavlefshëm/i revokuar 401; JWT i skaduar / përdorues i çaktivizuar / token i falsifikuar 401; 429 pas shumë dështimeve.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import psycopg
from fastapi.routing import APIRoute

from app.core.security import ROLE_PERMS
from app.main import create_app as create_ent
from apps.central.main import create_app as create_cen
from e2e.pilot import lib
from e2e.pilot.lib import CEN, ENT, IDS, Api, check

ROLES = ["client", "support", "approver", "finance", "pricing", "superadmin"]
SKIP = re.compile(r"^/(healthz|readyz|u/|webhooks/)")


def deps(d):
    for x in d.dependencies:
        yield x.call
        yield from deps(x)


def flatten(routes):
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, "original_router"):
            yield from flatten(r.original_router.routes)


def keyfor(role):
    return IDS["tenants"]["a"]["key"] if role == "client" else IDS["keys"][role]


def has(role, perm):
    perms = ROLE_PERMS[role]
    return "*" in perms or perm in perms


def url(path):
    return re.sub(r"\{[^}]+\}", "1", path)


print("Enterprise RBAC (real HTTP)")
routes = [r for r in flatten(create_ent().routes) if not SKIP.match(r.path)]
apis = {r: Api(ENT, keyfor(r)) for r in ROLES}
anon = Api(ENT)
n401 = n403 = n_ok = 0
bad = []
for route in routes:
    perms = next((c.perms for c in deps(route.dependant) if hasattr(c, "perms")), None)
    if perms is None:
        continue
    for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
        st, _ = anon.req(method, url(route.path), json={} if method != "GET" else None)
        if st != 401:
            bad.append(f"anon {method} {route.path} → {st}")
        n401 += 1
        for role in ROLES:
            allowed = any(has(role, p) for p in perms)
            if allowed and method != "GET":
                continue  # nuk ekzekutojmë ndryshime të lejuara (vetëm refuzimet dhe leximet)
            st, _ = apis[role].req(method, url(route.path), json={} if method != "GET" else None)
            if not allowed:
                n403 += 1
                if st != 403:
                    bad.append(f"{role} {method} {route.path} needs {perms} → {st}")
            else:
                n_ok += 1
                if st in (401, 403) and not (
                    role != "client" and route.path.startswith("/v1/portal/")
                ):  # `_client_only`: portali është vetëm për klientin (qëllimisht)
                    bad.append(f"{role} {method} {route.path} should pass authz → {st}")
check(
    not bad,
    f"Enterprise: {n401} anonymous probes → 401, {n403} forbidden role probes → 403, {n_ok} allowed GETs pass authz. Violations: {bad[:5]}",
)
check(n401 > 100 and n403 > 400, f"matrix is not vacuous ({n401} routes × methods, {n403} denials)")

print("Central RBAC (real HTTP)")
croutes = [r for r in flatten(create_cen().routes) if r.path.startswith("/admin")]
adm, opr, cano = (
    lib.central("PILOT_ADMIN_PASSWORD"),
    lib.central("PILOT_OPERATOR_PASSWORD"),
    Api(CEN),
)
vio = []
n_wr = n_rd = 0
for route in croutes:
    for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
        body = {"json": {}} if method != "GET" else {}
        if cano.req(method, url(route.path).replace("1", str(uuid.uuid4())), **body)[0] != 401:
            vio.append(f"anon {method} {route.path}")
        path = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), route.path)
        if method == "GET":
            n_rd += 1
            if route.path == "/admin/ping":  # probë vetëm-admin (qëllimisht)
                if adm.req(method, path)[0] != 200 or opr.req(method, path)[0] != 403:
                    vio.append("ping must be admin-only")
            elif opr.req(method, path)[0] in (401, 403) or adm.req(method, path)[0] in (401, 403):
                vio.append(f"read {route.path} denied to staff")
        else:
            n_wr += 1
            st = opr.req(method, path, json={})[0]
            if st != 403:
                vio.append(f"operator {method} {route.path} → {st}")
check(
    not vio and n_wr > 20 and n_rd > 30,
    f"Central: operator {n_wr} write routes → 403, operator+admin {n_rd} read routes allowed, anonymous → 401 ({vio[:5]})",
)
# që operatori të mos ketë shkruar asgjë (pas rrugëve të refuzuara)
con = psycopg.connect("postgresql://sms:sms@localhost:5432/h_cen")
op_id = con.execute("select id from users where email='operator@pilot.test'").fetchone()[0]
check(
    con.execute("select count(*) from audit_log where actor_id=%s", (op_id,)).fetchone()[0] == 0,
    "operator left no audit/write footprint in Central",
)

print("Enterprise auth / session")
check(Api(ENT, "sms_deadbeef_not-a-real-key").get("/v1/me")[0] == 401, "invalid key → 401")
check(Api(ENT).get("/v1/me")[0] == 401, "missing key → 401")
A, ta = lib.tenant("a")
st, k = A.post("/v1/portal/api-keys", {"name": "rbac-temp"})
temp = Api(ENT, k["key"])
check(temp.get("/v1/me")[0] == 200, "a fresh key works")
check(
    A.post(f"/v1/portal/api-keys/{k['id']}/revoke", {})[0] in (200, 204), "key revoked by its owner"
)
check(temp.get("/v1/me")[0] == 401, "revoked key → 401 immediately")
check(
    lib.staff("support").get("/v1/rate-cards")[0] == 403,
    "valid key without the permission → 403 (session stays valid)",
)
check(
    lib.staff("support").get("/v1/me")[0] == 200, "…and the same key still works for allowed calls"
)

print("Central auth / session")
secret = __import__("os").environ["CENTRAL_AUTH_SECRET"]
now = datetime.now(UTC)


def jwt_for(uid, exp, sec=secret, aud="sms-central-admin"):
    return jwt.encode(
        {
            "iss": "sms-central",
            "aud": aud,
            "sub": str(uid),
            "jti": uuid.uuid4().hex,
            "iat": int((now - timedelta(hours=1)).timestamp()),
            "exp": int(exp.timestamp()),
        },
        sec,
        algorithm="HS256",
    )


admin_id = IDS["admin_id"]
check(
    Api(CEN, token=jwt_for(admin_id, now + timedelta(minutes=5))).get("/auth/me")[0] == 200,
    "valid admin JWT → 200",
)
check(
    Api(CEN, token=jwt_for(admin_id, now - timedelta(minutes=1))).get("/auth/me")[0] == 401,
    "expired JWT → 401",
)
check(
    Api(CEN, token=jwt_for(admin_id, now + timedelta(minutes=5), sec="x" * 40)).get("/auth/me")[0]
    == 401,
    "forged JWT (wrong secret) → 401",
)
check(
    Api(CEN, token=jwt_for(admin_id, now + timedelta(minutes=5), aud="other")).get("/auth/me")[0]
    == 401,
    "wrong audience → 401",
)
check(Api(CEN, token="not.a.jwt").get("/auth/me")[0] == 401, "garbage token → 401")
st, _ = Api(CEN).post("/auth/token", {"email": "admin@pilot.test", "password": "wrong-password"})
check(st == 401, "bad password → 401")
con.execute("update users set status='disabled' where email='operator@pilot.test'")
con.commit()
check(
    opr.get("/admin/senders")[0] == 401,
    "disabled user's still-unexpired JWT → 401 (status read from DB on every request)",
)
con.execute("update users set status='active' where email='operator@pilot.test'")
con.commit()
check(opr.get("/admin/senders")[0] == 200, "re-enabled user works again")

print("Enterprise 429 (auth failure lockout)")
bad = Api(ENT, "sms_deadbeef_wrong")
codes = [bad.get("/v1/me")[0] for _ in range(24)]
check(401 in codes and codes[-1] == 429, f"repeated bad keys end in 429 ({codes[0]}…{codes[-1]})")
st, body = A.get("/v1/me")
check(
    st == 429 and "detail" in body,
    f"the lockout is per client IP and answers with a sanitized 429 ({st})",
)
ent = psycopg.connect("postgresql://sms:sms@localhost:5432/h_ent")
ent.execute("delete from sms_auth_failures")
ent.commit()
check(A.get("/v1/me")[0] == 200, "after the window/cleanup the valid key works again")
print(f"{lib.passed} checks")
