"""M11-I/F-02: kontratat statike të nginx Enterprise dhe Central (parsues i bllokut; pa nginx).

Sjellja reale me nginx në Docker provohet nga `e2e/edge/check_edge.py` (real_ip, XFF/Proto, header-at, 429, webhooks, health, /internal).
Këtu mbrohen: HSTS jo mbi HTTP, trashëgimia e `add_header`, real_ip vetëm nga CIDR të konfiguruara, zonat e kufizimit,
listener-i i brendshëm `/internal` dhe skripti që gjeneron CIDR-t.
"""

import re
import subprocess
from pathlib import Path

import pytest

from tests.nginx_conf import parse, strip_comments

ROOT = Path(__file__).resolve().parents[1]
ENT_RAW = (ROOT / "deploy" / "nginx.conf").read_text()
CEN_RAW = (ROOT / "deploy" / "nginx.central.conf").read_text()
ENT_HTTP, (ENT,) = parse(ENT_RAW)
CEN_HTTP, (PUB, INT) = parse(CEN_RAW)
SCRIPT = ROOT / "deploy" / "edge-cidrs.sh"
SPA = {"X-Content-Type-Options", "Referrer-Policy", "X-Frame-Options", "Content-Security-Policy"}
HSTS = "Strict-Transport-Security"
ERROR_CODES = (403, 404, 413, 429, 502, 503, 504)


def _code(raw: str) -> str:
    return strip_comments(raw)


# --- të përbashkëta: Enterprise dhe Central -------------------------------------------------------------------


@pytest.mark.parametrize(("name", "raw"), [("enterprise", ENT_RAW), ("central", CEN_RAW)])
def test_origin_is_http_only_and_never_sends_hsts_nor_trusts_everyone(name, raw):
    code = _code(raw)
    assert "ssl" not in code.lower() and "listen 443" not in code
    assert not re.search(rf"add_header\s+{HSTS}", code, re.I), (
        name
    )  # HSTS e zotëron terminatori i TLS
    assert "set_real_ip_from" not in code  # CIDR-t vijnë nga edge-cidrs.sh, kurrë të ngulitura
    assert "0.0.0.0/0" not in code and "::/0" not in code
    assert "$proxy_add_x_forwarded_for" not in code and "$http_x_forwarded_for" not in code
    assert (
        "X-Forwarded-Proto $scheme" not in code
    )  # nginx merr HTTP nga terminatori: $scheme do ishte gjithmonë `http`


@pytest.mark.parametrize(
    ("name", "raw", "servers"), [("enterprise", ENT_RAW, [ENT]), ("central", CEN_RAW, [PUB, INT])]
)
def test_real_ip_is_driven_only_by_the_generated_trusted_proxy_list(name, raw, servers):
    code = _code(raw)
    assert "geo $realip_remote_addr $from_trusted_proxy" in code
    assert "include /etc/nginx/trusted_proxies_geo.conf;" in code
    assert re.search(
        r'map "\$from_trusted_proxy:\$http_x_forwarded_proto" \$edge_proto \{\s*default http;\s*"~\^1:https\(,\|\$\)" https;',
        code,
    )
    for srv in servers:
        lvl = srv.server_level()
        assert "include /etc/nginx/trusted_proxies.conf;" in lvl, (name, srv.port)
        assert "real_ip_header X-Forwarded-For;" in lvl and "real_ip_recursive on;" in lvl
        assert "limit_req_status 429;" in lvl


@pytest.mark.parametrize(("name", "servers"), [("enterprise", [ENT]), ("central", [PUB, INT])])
def test_every_proxied_location_forwards_one_normalized_client_ip_and_a_trusted_proto(
    name, servers
):
    for srv in servers:
        proxied = [x for x in srv.locations if x.proxied]
        assert proxied, (name, srv.port)
        for x in proxied:
            for d in (
                "X-Forwarded-For $remote_addr",
                "X-Real-IP $remote_addr",
                "X-Forwarded-Proto $edge_proto",
                "X-Request-ID $request_id",
            ):
                assert f"proxy_set_header {d};" in x.body, (srv.port, x.spec, d)
            assert re.search(r"proxy_pass http://[a-z_]+;", x.body), (
                x.spec,
                "proxy_pass pa URI: path/query ruhen",
            )
            assert "proxy_connect_timeout 5s;" in x.body and f"proxy_hide_header {HSTS};" in x.body


def test_nginx_generated_errors_carry_the_full_header_set_on_every_listener():
    for srv, prefix, codes in (
        (ENT, "e", ERROR_CODES),
        (PUB, "e", ERROR_CODES),
        (INT, "i", (403, 429, 502, 504)),
    ):
        mapped = {int(c): n for c, n in re.findall(r"error_page (\d+) @(\w+);", srv.body)}
        assert tuple(sorted(mapped)) == tuple(sorted(codes)), srv.port
        for code in codes:
            loc = srv.loc(f"@{prefix}{code}")
            assert mapped[code] == f"{prefix}{code}" and f"return {code} " in loc.body
            if srv is not INT:
                assert SPA <= set(loc.headers()) and "Cache-Control" in loc.headers(), (
                    srv.port,
                    code,
                )


# --- Enterprise ------------------------------------------------------------------------------------------------


def test_enterprise_routes_health_webhooks_and_api_with_separate_limits():
    specs = {x.spec: x for x in ENT.locations if x.proxied}
    assert set(specs) == {"~ ^/(healthz|readyz)$", "~ ^/webhooks(/|$)", "~ ^/(v1|u)(/|$)"}
    api, hooks = specs["~ ^/(v1|u)(/|$)"], specs["~ ^/webhooks(/|$)"]
    assert (
        "limit_req zone=perip burst=60 nodelay;" in api.body
        and "limit_req zone=hooks" in hooks.body
    )
    assert (
        "zone=hooks" not in api.body and "zone=perip" not in hooks.body
    )  # callback-et s'ndajnë kufirin e klientëve
    rate = {
        z: int(r)
        for z, r in re.findall(
            r"limit_req_zone \$binary_remote_addr zone=(\w+):\w+ rate=(\d+)r/s;", _code(ENT_RAW)
        )
    }
    burst = int(re.search(r"zone=hooks burst=(\d+)", hooks.body).group(1))
    assert (
        rate["hooks"] > rate["perip"] and burst > 60
    )  # provider i vetëm mund të dërgojë batch-e legjitime
    assert "/webhooks" not in api.spec and "/v1" not in hooks.spec


def test_enterprise_health_is_internal_only_and_nginx_health_is_the_edge_probe():
    health = ENT.loc("(healthz|readyz)")
    assert health.body.index("include /etc/nginx/internal_allow.conf;") < health.body.index(
        "proxy_pass"
    )
    assert "limit_req" not in health.body
    probe = ENT.loc("= /nginx-health")
    assert 'return 200 "ok";' in probe.body and not probe.proxied
    assert not any(re.search(r"readyz", x.spec) for x in ENT.locations if x is not health)


def test_enterprise_header_inheritance_is_explicit_in_every_location_with_add_header():
    assert SPA <= set(re.findall(r"add_header\s+([A-Za-z-]+)\s", ENT.server_level()))
    for x in ENT.locations:
        if not x.headers():
            continue
        if x.proxied:  # përgjigjet e aplikacionit: CSP/nosniff/referrer janë të aplikacionit; nginx shton vetëm këto dy
            assert set(x.headers()) == {"X-Frame-Options", "Permissions-Policy"}, x.spec
        else:
            assert SPA <= set(x.headers()), x.spec
    for spec, extra in (("/assets/", "immutable"), ("/", "no-store")):
        loc = next(x for x in ENT.locations if x.spec == spec)
        assert "script-src 'self'" in loc.body and extra in loc.body
    assert "try_files $uri /index.html;" in next(x for x in ENT.locations if x.spec == "/").body


def test_enterprise_webhook_location_keeps_path_query_and_public_url_semantics():
    hooks = ENT.loc("^/webhooks")
    assert (
        "proxy_pass http://api;" in hooks.body
    )  # pa URI: path-i dhe query kalojnë siç janë (nënshkrimi Twilio mbi URL-në publike)
    assert "rewrite" not in hooks.body and "proxy_set_header Host $host;" in hooks.body
    assert "client_max_body_size 512k;" in ENT.server_level()


# --- Central ---------------------------------------------------------------------------------------------------


def test_central_public_surface_proxies_only_staff_paths_and_never_internal_or_enterprise():
    assert PUB.port == 8081
    specs = {x.spec for x in PUB.locations if x.proxied}
    assert specs == {"~ ^/auth(/|$)", "~ ^/admin(/|$)", "~ ^/(healthz|readyz)$"}
    deny = PUB.loc("^/(v1|webhooks|u|internal|registration)")
    assert "return 404;" in deny.body and not deny.proxied
    assert "/internal" not in " ".join(x.spec for x in PUB.locations if x.proxied)
    assert "try_files $uri /index.html;" in next(x for x in PUB.locations if x.spec == "/").body


def test_central_api_responses_are_never_cacheable_and_nginx_owns_the_security_headers():
    for x in (PUB.loc("^/auth"), PUB.loc("^/admin")):
        assert SPA <= set(x.headers()) and 'add_header Cache-Control "no-store" always;' in x.body
        for h in (
            "X-Content-Type-Options",
            "Referrer-Policy",
            "Content-Security-Policy",
            "X-Frame-Options",
            "Cache-Control",
        ):
            assert f"proxy_hide_header {h};" in x.body  # pa dyfishim me ç'dërgon upstream-i
    assert "limit_req zone=central_auth burst=10 nodelay;" in PUB.loc("^/auth").body


def test_central_health_is_internal_only_on_the_public_surface():
    h = PUB.loc("(healthz|readyz)")
    assert h.body.index("include /etc/nginx/internal_allow.conf;") < h.body.index("proxy_pass")


def test_central_static_locations_repeat_the_full_header_set():
    for spec in ("/assets/", "/"):
        loc = next(x for x in PUB.locations if x.spec == spec)
        assert SPA <= set(loc.headers()) and "script-src 'self'" in loc.body, spec


def test_central_internal_listener_serves_only_internal_behind_an_allowlist():
    assert INT.port == 8082
    proxied = [x for x in INT.locations if x.proxied]
    assert [x.spec for x in proxied] == ["~ ^/internal(/|$)"]
    x = proxied[0]
    assert x.body.index("include /etc/nginx/s2s_allow.conf;") < x.body.index("proxy_pass")
    assert "limit_req zone=central_internal" in x.body and "http://central_api;" in x.body
    root = next(loc for loc in INT.locations if loc.spec == "/")
    assert "return 404;" in root.body and not root.proxied  # /auth, /admin, SPA, health: jo këtu
    assert not re.search(
        r"\^/(auth|admin|registration)", " ".join(loc.spec for loc in INT.locations)
    )


# --- imazhet dhe skripti i CIDR-ve --------------------------------------------------------------------------------


@pytest.mark.parametrize("df", ["web.Dockerfile", "web-central.Dockerfile"])
def test_web_images_ship_the_cidr_generator_with_safe_empty_defaults(df):
    text = (ROOT / "deploy" / df).read_text()
    assert (
        "COPY deploy/edge-cidrs.sh /docker-entrypoint.d/40-edge-cidrs.sh" in text
        and "chmod 0755" in text
    )
    assert (
        ": > /etc/nginx/trusted_proxies.conf" in text and "deny all" in text
    )  # pa entrypoint: mos beso asnjë proxy


def _run(tmp_path, **env):
    full = {"PATH": "/usr/bin:/bin", "EDGE_NGINX_DIR": str(tmp_path), **env}
    return subprocess.run(["sh", str(SCRIPT)], env=full, capture_output=True, text=True)


def test_cidr_generator_defaults_trust_nobody_and_allow_only_loopback(tmp_path):
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert "set_real_ip_from" not in (tmp_path / "trusted_proxies.conf").read_text()
    assert (tmp_path / "trusted_proxies_geo.conf").read_text().strip().splitlines() == [
        "# gjeneruar nga edge-cidrs.sh"
    ]
    internal = (tmp_path / "internal_allow.conf").read_text().split()
    assert "127.0.0.1;" in internal and internal[-2:] == ["deny", "all;"]
    assert (tmp_path / "s2s_allow.conf").read_text().split()[-2:] == [
        "deny",
        "all;",
    ] and "allow" not in (tmp_path / "s2s_allow.conf").read_text()


def test_cidr_generator_writes_exactly_the_operator_supplied_networks(tmp_path):
    r = _run(
        tmp_path,
        EDGE_TRUSTED_PROXY_CIDRS="10.0.0.5/32, 192.0.2.0/24 2001:db8::/32",
        EDGE_INTERNAL_ALLOW_CIDRS="172.16.0.0/12",
        EDGE_S2S_ALLOW_CIDRS="203.0.113.9",
    )
    assert r.returncode == 0, r.stderr
    trusted = (tmp_path / "trusted_proxies.conf").read_text()
    assert [ln for ln in trusted.splitlines() if ln.startswith("set_real")] == [
        "set_real_ip_from 10.0.0.5/32;",
        "set_real_ip_from 192.0.2.0/24;",
        "set_real_ip_from 2001:db8::/32;",
    ]
    assert "10.0.0.5/32 1;" in (tmp_path / "trusted_proxies_geo.conf").read_text()
    assert "allow 172.16.0.0/12;" in (tmp_path / "internal_allow.conf").read_text()
    assert "allow 203.0.113.9;" in (tmp_path / "s2s_allow.conf").read_text()


@pytest.mark.parametrize(
    "bad", ["0.0.0.0/0", "::/0", "10.0.0.0/8,0.0.0.0/0", "not-a-cidr", "10.0.0.1/8;deny"]
)
def test_cidr_generator_refuses_trust_everyone_and_malformed_values(tmp_path, bad):
    for var in ("EDGE_TRUSTED_PROXY_CIDRS", "EDGE_INTERNAL_ALLOW_CIDRS", "EDGE_S2S_ALLOW_CIDRS"):
        r = _run(tmp_path, **{var: bad})
        assert r.returncode != 0 and "edge-cidrs" in r.stderr, (var, bad)


def test_the_real_nginx_edge_harness_exists_and_is_wired_into_ci():
    harness = ROOT / "e2e" / "edge" / "check_edge.py"
    assert harness.exists() and (ROOT / "e2e" / "edge" / "stub_upstream.py").exists()
    assert "e2e/edge/check_edge.py" in (ROOT / ".github" / "workflows" / "ci.yml").read_text()
