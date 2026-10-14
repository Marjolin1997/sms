"""Prova e skajit me nginx REAL në Docker (M11-I/F-02): real_ip, XFF/Proto, header-at, 429, webhooks, health, /internal.

    EDGE_WEB_IMAGE=sms-web EDGE_CENTRAL_WEB_IMAGE=sms-web-central EDGE_PY_IMAGE=python:3.12-slim python e2e/edge/check_edge.py

Kërkon daemon Docker; përdor rrjet të përkohshëm (10.77.0.0/24), upstream të rremë (stub_upstream.py), vetëm loopback.
Asnjë sekret, asnjë provider, asnjë SMS/email, asnjë DNS/TLS real. Del me kod 1 në dështim të çdo kontrolli.
"""

import http.client
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WEB = os.environ.get("EDGE_WEB_IMAGE", "sms-web")
CEN = os.environ.get("EDGE_CENTRAL_WEB_IMAGE", "sms-web-central")
PY = os.environ.get("EDGE_PY_IMAGE", "python:3.12-slim")
NET, SUBNET = "edgetest", "10.77.0.0/24"
HERE = Path(__file__).resolve().parent
passed = failed = 0


def sh(*a, check=True):
    r = subprocess.run(a, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(a)}\n{r.stdout}\n{r.stderr}")
    return r


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok  {msg}")
    else:
        failed += 1
        print(f"  FAIL {msg}")


def cleanup():
    names = sh("docker", "ps", "-aq", "--filter", "label=edgetest=1", check=False).stdout.split()
    if names:
        sh("docker", "rm", "-f", *names, check=False)
    sh("docker", "network", "rm", NET, check=False)


def req(port, path, method="GET", headers=None, body=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    hdrs = [(k.lower(), v) for k, v in r.getheaders()]
    c.close()
    return r.status, hdrs, data


def burst(port, path, n, method="GET", body=None, workers=24):
    """n kërkesa në paralel (kufiri është 30 r/s: një klient serial s'e arrin kurrë)."""
    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(lambda _: req(port, path, method=method, body=body)[0], range(n)))


def hv(hdrs, name):
    return [v for k, v in hdrs if k == name.lower()]


def jbody(data):
    try:
        return json.loads(data)
    except ValueError:
        return {}


def start_web(image, name, ports, env):
    args = ["docker", "run", "-d", "--name", name, "--network", NET, "--label", "edgetest=1"]
    for p in ports:
        args += ["-p", f"127.0.0.1:{p}:{p}"]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    sh(*args, image)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            if req(ports[0], "/nginx-health")[0] == 200:
                return
        except OSError:
            pass
        time.sleep(0.3)
    raise SystemExit(f"{name} did not start:\n" + sh("docker", "logs", name, check=False).stdout)


def main():
    cleanup()
    sh("docker", "network", "create", "--subnet", SUBNET, NET)
    gw = sh(
        "docker", "network", "inspect", NET, "-f", "{{(index .IPAM.Config 0).Gateway}}"
    ).stdout.strip()
    stub = sh(
        "docker", "run", "-d", "--name", "stub", "--network", NET, "--network-alias", "api",
        "--network-alias", "central-api", "--label", "edgetest=1",
        "-v", f"{HERE / 'stub_upstream.py'}:/stub.py:ro", PY, "python", "/stub.py",
    ).stdout.strip()  # fmt: skip
    time.sleep(1.5)
    pub = {"c": "203.0.113.9"}  # klient publik i dokumentimit (TEST-NET-3)

    # --------------------------------------------------------- CIDR të refuzuar
    print("== CIDR i refuzuar (besim për gjithë rrjetin)")
    for bad in ("0.0.0.0/0", "::/0", "10.0.0.0/8 0.0.0.0/0", "not-a-cidr"):
        r = sh(
            "docker",
            "run",
            "--rm",
            "-e",
            f"EDGE_TRUSTED_PROXY_CIDRS={bad}",
            WEB,
            "nginx",
            "-t",
            check=False,
        )
        check(
            r.returncode != 0 and "edge-cidrs" in (r.stderr + r.stdout),
            f"nginx refuzon të nisë me EDGE_TRUSTED_PROXY_CIDRS='{bad}'",
        )

    # --------------------------------------------------------- Enterprise
    print("== Enterprise: nginx -t")
    r = sh("docker", "run", "--rm", "--add-host", "api:127.0.0.1", WEB, "nginx", "-t", check=False)
    check(r.returncode == 0, "nginx -t Enterprise (parazgjedhje: asnjë proxy i besuar)")
    r = sh("docker", "run", "--rm", "--add-host", "api:127.0.0.1", "-e", f"EDGE_TRUSTED_PROXY_CIDRS={gw}/32,{SUBNET}",
           "-e", "EDGE_INTERNAL_ALLOW_CIDRS=192.0.2.0/24", WEB, "nginx", "-t", check=False)  # fmt: skip
    check(r.returncode == 0, "nginx -t Enterprise me CIDR të dhëna")

    # W1: peer-i (gateway) i besuar; W2: asnjë proxy i besuar; W3: proxy dhe rrjet i brendshëm i besuar
    for name, host_port, env in (
        ("w1", 18081, {"EDGE_TRUSTED_PROXY_CIDRS": f"{gw}/32", "EDGE_INTERNAL_ALLOW_CIDRS": ""}),
        ("w2", 18082, {"EDGE_TRUSTED_PROXY_CIDRS": "", "EDGE_INTERNAL_ALLOW_CIDRS": ""}),
        (
            "w3",
            18083,
            {"EDGE_TRUSTED_PROXY_CIDRS": f"{SUBNET}", "EDGE_INTERNAL_ALLOW_CIDRS": SUBNET},
        ),
    ):
        args = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            NET,
            "--label",
            "edgetest=1",
            "-p",
            f"127.0.0.1:{host_port}:8080",
        ]
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        sh(*args, WEB)
    for p in (18081, 18082, 18083):
        for _ in range(60):
            try:
                if req(p, "/nginx-health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.3)

    print("== Enterprise: real_ip, XFF dhe X-Forwarded-Proto")
    s, h, d = req(
        18081,
        "/v1/x",
        headers={
            "X-Forwarded-For": f"6.6.6.6, {pub['c']}",
            "X-Forwarded-Proto": "https",
            "X-Real-IP": "6.6.6.6",
        },
    )
    got = jbody(d).get("headers", {})
    check(
        s == 200 and got.get("x-forwarded-for") == pub["c"],
        f"proxy i besuar + XFF i falsifikuar majtas: aplikacioni merr vetëm klientin real ({got.get('x-forwarded-for')})",
    )
    check(got.get("x-real-ip") == pub["c"], "X-Real-IP i klientit zëvendësohet me IP-në e zgjidhur")
    check(
        got.get("x-forwarded-proto") == "https",
        "X-Forwarded-Proto=https kalon vetëm nga proxy i besuar",
    )
    s, h, d = req(18081, "/v1/x", headers={"X-Forwarded-For": f"{pub['c']}, {gw}"})
    check(
        jbody(d)["headers"].get("x-forwarded-for") == pub["c"],
        "disa proxy upstream (real_ip_recursive): klienti real, jo proxy-t",
    )
    s, h, d = req(18081, "/v1/x")
    check(
        jbody(d)["headers"].get("x-forwarded-for") == gw
        and jbody(d)["headers"].get("x-forwarded-proto") == "http",
        "pa XFF nga proxy i besuar: IP e lidhjes, Proto=http",
    )
    s, h, d = req(
        18082,
        "/v1/x",
        headers={
            "X-Forwarded-For": "6.6.6.6",
            "X-Forwarded-Proto": "https",
            "X-Real-IP": "6.6.6.6",
        },
    )
    got = jbody(d)["headers"]
    check(
        got.get("x-forwarded-for") == gw,
        f"pa proxy të besuar: XFF i klientit injorohet, aplikacioni merr IP-në e lidhjes ({got.get('x-forwarded-for')})",
    )
    check(
        got.get("x-forwarded-proto") == "http",
        "klient publik nuk mund të falsifikojë X-Forwarded-Proto",
    )
    check(got.get("x-real-ip") == gw, "X-Real-IP i falsifikuar injorohet")

    print("== Enterprise: header-at e sigurisë")
    need = (
        "x-content-type-options",
        "referrer-policy",
        "x-frame-options",
        "content-security-policy",
    )
    for label, path, _kw in (
        ("/", "/", {}),
        ("SPA fallback", "/inbox/123", {}),
        ("/assets/* (404)", "/assets/missing.js", {}),
    ):
        s, h, d = req(18082, path)
        check(
            all(len(hv(h, n)) == 1 for n in need) and not hv(h, "strict-transport-security"),
            f"{label}: {s}, header-at e sigurisë një herë, pa HSTS",
        )
    s, h, d = req(18082, "/")
    check(
        "script-src 'self'" in hv(h, "content-security-policy")[0]
        and hv(h, "cache-control") == ["no-store"],
        "/: CSP e SPA + no-store",
    )
    s, h, d = req(18082, "/v1/x")
    check(
        s == 200
        and len(hv(h, "content-security-policy")) == 1
        and len(hv(h, "x-content-type-options")) == 1
        and len(hv(h, "referrer-policy")) == 1,
        "API: një CSP/nosniff/referrer (të aplikacionit, pa dyfishim nga nginx)",
    )
    check(not hv(h, "strict-transport-security"), "API: HSTS i aplikacionit hiqet te origjina HTTP")
    check(
        hv(h, "x-frame-options") == ["DENY"] and hv(h, "permissions-policy"),
        "API: nginx shton X-Frame-Options dhe Permissions-Policy",
    )
    s, h, d = req(
        18082,
        "/v1/x",
        method="POST",
        body=b"x" * (600 * 1024),
        headers={"Content-Type": "application/json"},
    )
    check(
        s == 413 and all(len(hv(h, n)) == 1 for n in need),
        f"gabim nginx 413: header-a të plotë ({s})",
    )
    s, h, d = req(18082, "/healthz")
    check(
        s == 403 and all(len(hv(h, n)) == 1 for n in need),
        f"gabim nginx 403 (health publik): header-a të plotë ({s})",
    )

    print("== Enterprise: kufizimi i shpejtësisë")
    codes = burst(18082, "/v1/x", 400)
    check(
        429 in codes and 503 not in codes,
        f"kufizimi i klientëve kthen 429, jo 503 ({codes.count(429)}x429, {codes.count(503)}x503)",
    )
    with ThreadPoolExecutor(24) as ex:
        full = list(ex.map(lambda _: req(18082, "/v1/x"), range(400)))
    limited = next((r for r in full if r[0] == 429), None)
    check(
        limited is not None
        and all(len(hv(limited[1], n)) == 1 for n in need)
        and hv(limited[1], "retry-after") == ["1"],
        "429 i nginx ka header-a të plotë dhe Retry-After",
    )
    time.sleep(1.5)
    codes = burst(18082, "/webhooks/dlr/fake", 300, method="POST", body=b"{}")
    check(
        429 not in codes and 503 not in codes,
        f"callback-et e provider-it nuk goditen nga kufiri i klientëve (300 kërkesa: {sorted(set(codes))})",
    )

    print("== Enterprise: webhooks/DLR (path, query, trup, nënshkrim të paprekur)")
    body = b'{"provider_message_id":"abc","status":"delivered"}'
    s, h, d = req(
        18082,
        "/webhooks/dlr/fake?x=1&y=%20z",
        method="POST",
        body=body,
        headers={"X-Signature": "sha256=deadbeef", "Content-Type": "application/json"},
    )
    j = jbody(d)
    import hashlib

    check(
        j.get("path") == "/webhooks/dlr/fake" and j.get("query") == "x=1&y=%20z",
        "path dhe query ruhen saktësisht",
    )
    check(
        j.get("body_sha256") == hashlib.sha256(body).hexdigest()
        and j["headers"].get("x-signature") == "sha256=deadbeef",
        "trupi dhe X-Signature kalojnë të pandryshuar",
    )
    s, h, d = req(18082, "/webhooks/twilio/status?a=b", method="POST", body=b"MessageSid=SM1&MessageStatus=delivered",
                  headers={"X-Twilio-Signature": "sig=", "Content-Type": "application/x-www-form-urlencoded"})  # fmt: skip
    j = jbody(d)
    check(
        j.get("path") == "/webhooks/twilio/status"
        and j["headers"].get("x-twilio-signature") == "sig="
        and j["headers"].get("host"),
        "Twilio: path dhe X-Twilio-Signature të paprekur; Host përcillet",
    )

    print("== Enterprise: health/readiness")
    s, h, d = req(18082, "/readyz")
    check(s == 403, f"/readyz publik refuzohet ({s})")
    s, h, d = req(18081, "/readyz", headers={"X-Forwarded-For": pub["c"]})
    check(s == 403, f"/readyz nga klient publik përmes proxy të besuar refuzohet ({s})")
    s, h, d = req(18083, "/readyz")
    check(
        s == 200 and not hv(h, "strict-transport-security"),
        f"/readyz nga rrjeti i brendshëm i lejuar ({s})",
    )
    s, h, d = req(18082, "/nginx-health")
    check(s == 200 and d == b"ok", "/nginx-health publik (vetëm gjallëria e nginx)")

    # --------------------------------------------------------- Central
    print("== Central: nginx -t dhe nisja")
    r = sh(
        "docker",
        "run",
        "--rm",
        "--add-host",
        "central-api:127.0.0.1",
        CEN,
        "nginx",
        "-t",
        check=False,
    )
    check(r.returncode == 0, "nginx -t Central (parazgjedhje)")
    for name, ports, env in (
        (
            "c1",
            (18091, 18092),
            {
                "EDGE_TRUSTED_PROXY_CIDRS": f"{gw}/32",
                "EDGE_S2S_ALLOW_CIDRS": f"{pub['c']}/32,{gw}/32",
            },
        ),
        ("c2", (18093, 18094), {"EDGE_TRUSTED_PROXY_CIDRS": "", "EDGE_S2S_ALLOW_CIDRS": ""}),
    ):
        args = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--network",
            NET,
            "--label",
            "edgetest=1",
            "-p",
            f"127.0.0.1:{ports[0]}:8081",
            "-p",
            f"127.0.0.1:{ports[1]}:8082",
        ]
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        sh(*args, CEN)
    for p in (18091, 18092, 18093, 18094):
        for _ in range(60):
            try:
                if req(p, "/nginx-health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.3)

    print("== Central: sipërfaqja publike")
    for path in (
        "/internal/sync/changes",
        "/internal/money/state",
        "/v1/contacts",
        "/webhooks/dlr/x",
        "/u/x",
        "/registration",
    ):
        s, h, d = req(18091, path)
        check(
            s == 404 and len(hv(h, "content-security-policy")) == 1,
            f"{path} -> 404 në sipërfaqen publike (edhe nga proxy i besuar/i lejuar)",
        )
    s, h, d = req(18091, "/auth/token", method="POST", body=b'{"email":"a","password":"b"}',
                  headers={"Content-Type": "application/json", "X-Forwarded-For": f"6.6.6.6, {pub['c']}", "X-Forwarded-Proto": "https"})  # fmt: skip
    j = jbody(d)
    check(
        s == 200
        and j["headers"].get("x-forwarded-for") == pub["c"]
        and j["headers"].get("x-forwarded-proto") == "https",
        "Central /auth: XFF i normalizuar, Proto i besuar",
    )
    check(
        hv(h, "cache-control") == ["no-store"]
        and len(hv(h, "x-content-type-options")) == 1
        and len(hv(h, "content-security-policy")) == 1,
        "Central /auth: Cache-Control no-store dhe header-a një herë",
    )
    check(not hv(h, "strict-transport-security"), "Central: pa HSTS")
    s, h, d = req(18091, "/admin/ping")
    check(s == 200 and hv(h, "cache-control") == ["no-store"], "Central /admin: no-store")
    s, h, d = req(
        18093,
        "/auth/token",
        method="POST",
        body=b"{}",
        headers={"X-Forwarded-For": "6.6.6.6", "X-Forwarded-Proto": "https"},
    )
    got = jbody(d)["headers"]
    check(
        got.get("x-forwarded-for") == gw and got.get("x-forwarded-proto") == "http",
        "Central pa proxy të besuar: XFF/Proto të falsifikuar injorohen",
    )
    for path, name in (
        ("/", "/"),
        ("/some/spa/route", "SPA fallback"),
        ("/assets/missing.js", "/assets/* 404"),
    ):
        s, h, d = req(18093, path)
        check(
            all(len(hv(h, n)) == 1 for n in need) and not hv(h, "strict-transport-security"),
            f"Central {name}: {s}, header-at një herë, pa HSTS",
        )
    s, h, d = req(18093, "/readyz")
    check(s == 403, f"Central /readyz publik refuzohet ({s})")
    codes = burst(18093, "/auth/token", 60, method="POST", body=b"{}")
    check(429 in codes and 503 not in codes, f"Central /auth kufizon me 429 ({codes.count(429)}x)")

    print("== Central: listener i brendshëm /internal (8082)")
    auth = {"Authorization": "Bearer eyJ.fake.token"}
    s, h, d = req(
        18092,
        "/internal/sync/changes?after=5",
        headers={**auth, "X-Forwarded-For": f"6.6.6.6, {pub['c']}"},
    )
    j = jbody(d)
    check(
        s == 200
        and j.get("path") == "/internal/sync/changes"
        and j.get("query") == "after=5"
        and j["headers"].get("authorization") == auth["Authorization"],
        "rrugë /internal e lejuar: path/query/Authorization të paprekur",
    )
    check(
        j["headers"].get("x-forwarded-for") == pub["c"]
        and len(hv(h, "content-security-policy")) == 1
        and not hv(h, "strict-transport-security"),
        "8082: XFF i normalizuar, header-a një herë, pa HSTS",
    )
    for path in (
        "/auth/token",
        "/admin/ping",
        "/",
        "/healthz",
        "/readyz",
        "/registration",
        "/v1/x",
        "/webhooks/dlr/x",
    ):
        s, h, d = req(18092, path)
        check(s == 404, f"8082 {path} -> 404 (vetëm /internal)")
    s, h, d = req(18094, "/internal/sync/changes", headers=auth)
    check(s == 403, f"8082 pa allowlist (parazgjedhje: askush) -> 403 ({s})")
    s, h, d = req(18091, "/internal/sync/changes", headers=auth)
    check(s == 404, "/internal s'është i arritshëm në sipërfaqen publike as për IP të lejuar")

    # --------------------------------------------------------- upstream i rënë: gabime nginx
    sh("docker", "stop", stub)
    s, h, d = req(18082, "/v1/x")
    check(
        s in (502, 504)
        and all(len(hv(h, n)) == 1 for n in need)
        and not hv(h, "strict-transport-security"),
        f"upstream i rënë: gabim nginx {s} me header-a të plotë",
    )
    s, h, d = req(18091, "/admin/ping")
    check(
        s in (502, 504) and all(len(hv(h, n)) == 1 for n in need),
        f"Central upstream i rënë: gabim nginx {s} me header-a të plotë",
    )

    cleanup()
    print(f"\n{passed} checks passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        cleanup()
