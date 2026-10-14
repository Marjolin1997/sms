"""M11-I/F-01: paketimi i Central për prodhim (statik, pa Docker): Dockerfile, compose, nginx, shablloni i mjedisit, gate-t e nisjes.

Asnjë shërbim nuk niset këtu dhe asnjë sekret nuk përdoret; validimi i imazhit/nginx-it real bëhet në CI (docker build, nginx -t).
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, text

from apps.central.core.config import Settings
from apps.central.main import create_app

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())
SVC = COMPOSE["services"]
NGINX = (ROOT / "deploy" / "nginx.central.conf").read_text()
WEB_DOCKERFILE = (ROOT / "deploy" / "web-central.Dockerfile").read_text()
ENV_EXAMPLE = (ROOT / ".env.central.example").read_text()
JOBS = ("central-billing-run", "central-send-notifications", "central-tool")
SERVING = ("central-migrate", "central-api", "central-web")
CENTRAL = ("central-migrate", "central-api") + JOBS
ENTERPRISE_DEFAULT = {"db", "migrate", "api", "worker", "webhook-worker", "web"}


# --- Dockerfile ---------------------------------------------------------------------------------------------


def _stages() -> dict[str, str]:
    parts = re.split(r"(?m)^FROM\s+\S+\s+AS\s+(\w+)\s*$", DOCKERFILE)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def test_dockerfile_has_a_central_stage_and_enterprise_stays_the_final_default():
    stages = _stages()
    assert list(stages) == [
        "base",
        "central",
        "enterprise",
    ]  # i fundit = parazgjedhja (build: . i pandryshuar)
    ent = stages["enterprise"]
    for need in (
        "COPY alembic.ini",
        "COPY app ./app",
        "COPY scripts ./scripts",
        "EXPOSE 8000",
        "HEALTHCHECK",
    ):
        assert need in ent
    assert "apps" not in re.sub(r"\bapps\.", "", ent.replace("# ", "")) or "COPY apps" not in ent


def test_central_stage_ships_only_runtime_material_as_non_root_without_secrets():
    c = _stages()["central"]
    assert "COPY apps ./apps" in c and "COPY packages ./packages" in c
    for forbidden in (
        "frontend",
        "tests",
        ".env",
        "COPY scripts",
        "COPY app ",
        "COPY alembic",
        "ENV CENTRAL_",
    ):
        assert forbidden not in c.replace("# ", ""), forbidden
    assert "useradd -r -u 10001 sms" in c and "USER sms" in c
    assert (
        '"apps.central.main:app"' in c and '"--port", "8001"' in c and '"--no-proxy-headers"' in c
    )
    assert "pip install" not in c  # varësitë janë instaluar një herë te `base` (pa dyfishim)
    assert "HEALTHCHECK" not in c  # një mekanizëm i vetëm gatishmërie: compose `central-api`


def test_dockerignore_and_gitignore_keep_central_env_and_secrets_out():
    ignore = (ROOT / ".dockerignore").read_text().split()
    git = (ROOT / ".gitignore").read_text().split()
    assert ".env.*" in ignore and ".env" in ignore and "secrets/" in ignore
    assert ".env.central" in git and ".env" in git and "secrets/" in git


# --- compose ------------------------------------------------------------------------------------------------


def test_central_services_exist_and_only_central_web_publishes_a_port():
    assert {"central-migrate", "central-api", "central-web", *JOBS} <= set(SVC)
    published = {n: s["ports"] for n, s in SVC.items() if s.get("ports")}
    assert published == {"web": ["127.0.0.1:8080:8080"], "central-web": ["127.0.0.1:8081:8081"]}
    assert not SVC["central-api"].get("ports") and not SVC["central-api"].get("expose")


def test_plain_compose_up_is_enterprise_only_and_central_is_profile_gated():
    always_on = {n for n, s in SVC.items() if "profiles" not in s}
    assert (
        always_on == ENTERPRISE_DEFAULT
    )  # `docker compose up -d` i zakonshëm: sjellja e mëparshme, vetëm Enterprise
    assert not any(n.startswith("central-") for n in always_on)
    for n in SERVING:
        assert SVC[n]["profiles"] == ["central"], n
    for n in JOBS:
        assert SVC[n]["profiles"] == ["central-jobs"], n  # kurrë te profili `central`
    # porti i publikuar në nisjen e zakonshme: vetëm konsola Enterprise; Central publikon vetëm central-web (profili `central`)
    assert {n for n in always_on if SVC[n].get("ports")} == {"web"}
    assert {n for n in SERVING if SVC[n].get("ports")} == {"central-web"}
    assert SVC["central-web"]["ports"] == ["127.0.0.1:8081:8081"]
    assert not any(SVC[n].get("ports") for n in JOBS)


def test_central_profile_dependencies_stay_inside_the_profile_and_jobs_do_not_pull_serving():
    dep = lambda n: set(SVC[n].get("depends_on") or {})  # noqa: E731
    assert dep("central-migrate") == {"db"}
    assert dep("central-api") == {"db", "central-migrate"}
    assert dep("central-web") == {"central-api"}
    for n in SERVING:  # asgjë nga serving s'varet nga jobet; varësitë janë `db` (parazgjedhje) ose brenda profilit `central`
        assert dep(n) <= {"db", *SERVING}, n
    for (
        n
    ) in JOBS:  # jobet varen vetëm nga `db`: `--profile central-jobs` s'nis central-api/central-web
        assert dep(n) == {"db"}, n
    for n in ENTERPRISE_DEFAULT:  # Enterprise s'varet nga asnjë shërbim Central
        assert not any(d.startswith("central-") for d in dep(n)), n


# --- rendering i vërtetë i compose-it (kërkon `docker compose`; kalon vetëm pa daemon) ------------------------


def _compose_available() -> bool:
    if not shutil.which("docker"):
        return False
    r = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
    return r.returncode == 0


@pytest.fixture()
def compose_dir(tmp_path):
    shutil.copy(ROOT / "docker-compose.prod.yml", tmp_path / "docker-compose.prod.yml")
    shutil.copy(ROOT / ".env.example", tmp_path / ".env")  # vetëm shabllon: pa sekrete reale
    return tmp_path


def _render(d, *profiles):
    args = ["docker", "compose", "-f", "docker-compose.prod.yml", "--env-file", ".env"]
    for p in profiles:
        args += ["--profile", p]
    r = subprocess.run([*args, "config", "-q"], cwd=d, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def _services(d, *profiles):
    args = ["docker", "compose", "-f", "docker-compose.prod.yml", "--env-file", ".env"]
    for p in profiles:
        args += ["--profile", p]
    r = subprocess.run([*args, "config", "--services"], cwd=d, capture_output=True, text=True)
    return r.returncode, set(r.stdout.split()), r.stdout + r.stderr


@pytest.mark.skipif(not _compose_available(), reason="needs `docker compose` (CLI only; no daemon)")
def test_compose_renders_enterprise_only_without_central_env_and_central_needs_it_when_enabled(
    compose_dir,
):
    rc, services, _ = _services(compose_dir)  # pa `.env.central`: sjellja e mëparshme Enterprise
    assert rc == 0 and services == ENTERPRISE_DEFAULT
    assert (
        _render(compose_dir)[0] == 0
    )  # rendering i plotë pa `.env.central` (nisja e zakonshme s'e kërkon)
    # Central i aktivizuar kërkon mjedisin e vet: kërkesa NUK dobësohet (dështim i qartë pa `.env.central`)
    for prof in ("central", "central-jobs"):
        rc, out = _render(compose_dir, prof)
        assert rc != 0 and ".env.central" in out, prof
    shutil.copy(ROOT / ".env.central.example", compose_dir / ".env.central")
    rc, services, _ = _services(compose_dir)
    assert (
        rc == 0 and services == ENTERPRISE_DEFAULT
    )  # edhe me `.env.central` pranë, nisja e zakonshme = Enterprise
    rc, services, _ = _services(compose_dir, "central")
    assert rc == 0 and services == ENTERPRISE_DEFAULT | set(SERVING)  # pa jobe
    rc, services, _ = _services(compose_dir, "central-jobs")
    assert rc == 0 and services == ENTERPRISE_DEFAULT | set(JOBS)  # pa serving
    rc, services, _ = _services(compose_dir, "central", "central-jobs")
    assert rc == 0 and services == ENTERPRISE_DEFAULT | set(SERVING) | set(JOBS)


def test_central_migration_is_an_explicit_one_shot_on_the_central_ini():
    m = SVC["central-migrate"]
    assert m["command"] == ["alembic", "-c", "apps/central/alembic.ini", "upgrade", "head"]
    assert m["restart"] == "no" and m["build"]["target"] == "central"
    assert SVC["central-api"]["depends_on"]["central-migrate"] == {
        "condition": "service_completed_successfully"
    }
    # asnjë shërbim Enterprise s'varet nga Central, dhe migrimi Enterprise s'ka lidhje me Central
    for n, s in SVC.items():
        if not n.startswith("central-"):
            assert not any(str(d).startswith("central-") for d in (s.get("depends_on") or {})), n


def test_central_services_use_central_env_only_and_the_hardening_conventions():
    for name in CENTRAL:
        s = SVC[name]
        assert s["env_file"] == ".env.central", name  # nuk lexojnë `.env` të Enterprise
        assert s["environment"] == {"CENTRAL_ENV": "production"}, name
        assert s["build"]["target"] == "central", name
        assert s["read_only"] is True and s["cap_drop"] == ["ALL"], name
        assert "no-new-privileges:true" in s["security_opt"], name
        assert (
            not s.get("privileged") and not s.get("network_mode") and not s.get("container_name")
        ), name
        assert "volumes" not in s, name  # pa mount (as çelës CP te Central)
    text = (ROOT / "docker-compose.prod.yml").read_text()
    central_text = (
        text[text.index("x-central:") : text.index("services:")]
        + text[text.index("  central-migrate:") :]
    )
    assert (
        "SMS_" not in central_text.replace("SMS_DATABASE_URL: only", "")
        and "POSTGRES_PASSWORD" not in central_text
    )
    assert (
        "CENTRAL_DATABASE_URL" not in text
    )  # URL vjen vetëm nga `.env.central` (rol/fjalëkalim të veçantë)
    assert "sms_central" in text  # DB logjike e veçantë e dokumentuar


def test_central_api_readiness_is_the_single_health_mechanism_and_web_waits_for_it():
    h = SVC["central-api"]["healthcheck"]["test"]
    assert h[:3] == ["CMD", "python", "-c"] and "127.0.0.1:8001/readyz" in h[3]
    assert SVC["central-api"]["command"][:3] == ["uvicorn", "apps.central.main:app", "--host"]
    assert "--no-proxy-headers" in SVC["central-api"]["command"]
    assert SVC["central-web"]["depends_on"]["central-api"] == {"condition": "service_healthy"}
    assert SVC["central-web"]["build"]["dockerfile"] == "deploy/web-central.Dockerfile"


def test_central_jobs_are_manual_one_shots_behind_the_central_jobs_profile():
    for name in JOBS:
        s = SVC[name]
        assert s["profiles"] == ["central-jobs"] and s["restart"] == "no", name
        assert not s.get("ports") and "healthcheck" not in s, name
    assert SVC["central-billing-run"]["command"][:3] == [
        "python",
        "-m",
        "apps.central.tools.billing_run",
    ]
    assert SVC["central-send-notifications"]["command"] == [
        "python",
        "-m",
        "apps.central.tools.send_notifications",
    ]
    assert SVC["central-tool"]["entrypoint"] == ["python", "-m"]
    assert SVC["central-tool"]["command"] == ["apps.central.tools.sender_central_readiness"]
    # `financial_readiness` varet nga skriptet Enterprise (subproces): s'duhet të jetë parazgjedhje e imazhit Central
    assert "COPY scripts" not in _stages()["central"]
    # ekzistojnë realisht si module dhe `up -d` i zakonshëm s'i nis
    for mod in ("billing_run", "send_notifications", "sender_central_readiness"):
        assert (ROOT / "apps" / "central" / "tools" / f"{mod}.py").exists()
    always_on = {n for n, s in SVC.items() if "profiles" not in s}
    assert not always_on & set(JOBS)


def test_enterprise_services_keep_their_env_file_and_image_target():
    for name in ("api", "worker", "webhook-worker", "migrate"):
        assert SVC[name]["env_file"] == ".env", name
        assert "target" not in SVC[name].get("build", {}), (
            name
        )  # `build: .` = stage-i i fundit (Enterprise)


# --- nginx Central ------------------------------------------------------------------------------------------


def _locations() -> dict[str, str]:
    out = {}
    for m in re.finditer(r"(?m)^\s*location\s+([^{\n]+?)\s*\{", NGINX):
        depth, j = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(NGINX[j], 0)
            j += 1
        out[m.group(1).strip()] = NGINX[m.end() : j - 1]
    return out


def test_central_nginx_routes_only_central_paths_to_the_central_api():
    loc = _locations()
    proxied = {k for k, v in loc.items() if "proxy_pass" in v}
    assert proxied == {"~ ^/auth(/|$)", "~ ^/(admin(/|$)|healthz$|readyz$)"}
    assert re.findall(r"proxy_pass\s+(\S+);", NGINX) == ["http://central_api"] * 2
    assert re.search(r"upstream central_api\s*\{\s*server central-api:8001;", NGINX)
    assert "/v1" not in "".join(v for v in loc.values() if "proxy_pass" in v)


def test_central_nginx_denies_enterprise_and_internal_families_and_falls_back_to_the_spa():
    loc = _locations()
    deny = loc["~ ^/(v1|webhooks|u|internal|registration)(/|$)"]
    assert "return 404" in deny and "proxy_pass" not in deny
    assert "try_files $uri /index.html" in loc["/"]
    for path in ("/v1/x", "/webhooks/dlr/x", "/internal/sync/changes", "/registration"):
        assert re.match(r"^/(v1|webhooks|u|internal|registration)(/|$)", path)
        assert not re.match(r"^/(admin(/|$)|healthz$|readyz$)|^/auth(/|$)", path)


def test_central_nginx_is_plain_http_without_hsts_and_sets_forwarding_headers_deliberately():
    assert (
        "listen 8081;" in NGINX
        and "ssl" not in NGINX.lower().replace("# ", "").split("server {")[1]
    )
    assert not re.search(r"(?m)^\s*add_header\s+Strict-Transport-Security", NGINX)
    for h in (
        "X-Forwarded-For $remote_addr",
        "X-Forwarded-Proto $scheme",
        "X-Request-ID $request_id",
    ):
        assert NGINX.count(f"proxy_set_header {h};") == 2
    assert "$proxy_add_x_forwarded_for" not in NGINX  # SHKRUHET, jo shtohet (hops=1 i saktë)
    assert "Content-Security-Policy" in NGINX and "frame-ancestors 'none'" in NGINX


def test_central_nginx_security_headers_survive_in_every_location_that_sets_its_own_headers():
    # nginx: `add_header` në location zëvendëson atë të serverit; pa përsëritje SPA shërbehej pa CSP/X-Frame (gjetur në validimin real)
    needed = (
        "X-Content-Type-Options",
        "Referrer-Policy",
        "X-Frame-Options",
        "Content-Security-Policy",
    )
    own = {k: v for k, v in _locations().items() if "add_header" in v}
    assert {"/", "/assets/"} <= set(own)
    for name, body in own.items():
        for h in needed:
            assert f"add_header {h} " in body, (name, h)
    assert "limit_req_status 429;" in NGINX


def test_central_web_image_builds_the_central_frontend_only_with_no_build_secrets():
    assert "COPY frontend-central/" in WEB_DOCKERFILE and "npm ci" in WEB_DOCKERFILE
    assert "npm run build" in WEB_DOCKERFILE
    assert "COPY deploy/nginx.central.conf" in WEB_DOCKERFILE and "EXPOSE 8081" in WEB_DOCKERFILE
    assert "COPY frontend/" not in WEB_DOCKERFILE and "ARG " not in WEB_DOCKERFILE
    assert "ENV " not in WEB_DOCKERFILE


# --- inventari i komandave brenda imazhit Central ----------------------------------------------------------


RUNBOOK = (ROOT / "docs" / "RUNBOOK.md").read_text()
TOOLS = ROOT / "apps" / "central" / "tools"


def _inventory() -> list[str]:
    block = RUNBOOK.split("<!-- central-image-commands:start -->")[1].split(
        "<!-- central-image-commands:end -->"
    )[0]
    return re.findall(r"^\|\s*`(apps\.central\.tools\.\w+)`", block, re.M)


def test_documented_central_commands_are_a_non_empty_unique_inventory_of_real_modules():
    inv = _inventory()
    assert len(inv) >= 8 and len(inv) == len(set(inv))
    for mod in inv:
        assert (TOOLS / f"{mod.rsplit('.', 1)[1]}.py").exists(), mod


@pytest.mark.parametrize("mod", _inventory())
def test_every_documented_central_command_imports_and_runs_without_enterprise_code(mod):
    src = (TOOLS / f"{mod.rsplit('.', 1)[1]}.py").read_text()
    # asnjë varësi te kodi Enterprise që mungon brenda imazhit Central (`app/`, `scripts/`) dhe asnjë nënproces
    assert "subprocess" not in src and "import scripts" not in src and "from scripts" not in src, (
        mod
    )
    assert not re.search(r"(?m)^\s*(from|import)\s+app(\.|\s)", src), mod
    assert '"scripts.' not in src and "'scripts." not in src, mod
    r = subprocess.run(
        [sys.executable, "-I", "-m", mod, "--help"], cwd=ROOT, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT)},
    )  # fmt: skip
    if (
        r.returncode != 0 and "No module named" in r.stderr
    ):  # venv jashtë PATH: përdor interpretuesin aktual
        r = subprocess.run(
            [sys.executable, "-m", mod, "--help"], cwd=ROOT, capture_output=True, text=True
        )
    assert r.returncode == 0 and "usage:" in r.stdout, (mod, r.stderr[-300:])


def test_compose_central_commands_are_a_subset_of_the_documented_inventory():
    inv = set(_inventory())
    expected = {
        "central-billing-run": "apps.central.tools.billing_run",
        "central-send-notifications": "apps.central.tools.send_notifications",
        "central-tool": "apps.central.tools.sender_central_readiness",
    }
    for name in JOBS:
        s = SVC[name]
        words = [*s.get("entrypoint", []), *s["command"]]
        mods = {w for w in words if w.startswith("apps.central.tools.")}
        assert mods == {expected[name]}, name
        assert mods <= inv, name
    text = (ROOT / "docker-compose.prod.yml").read_text()
    assert "financial_readiness" not in re.sub(
        r"(?m)^\s*#.*$", "", text
    )  # jo si komandë e ekzekutueshme


def test_financial_readiness_is_documented_as_outside_the_central_image_and_really_needs_enterprise_code():
    assert "apps.central.tools.financial_readiness" not in _inventory()
    src = (TOOLS / "financial_readiness.py").read_text()
    assert (
        "subprocess" in src and "scripts." in src
    )  # arsyeja e përjashtimit; nëse ndryshon, rishiko inventarin
    assert "Jashtë imazhit Central" in RUNBOOK and "COPY scripts" not in _stages()["central"]
    # §8 (hapat e ekzekutueshëm) nuk e paraqet si komandë të imazhit Central
    steps = RUNBOOK.split("## 8. Central")[1].split("### Inventari")[0]
    assert "financial_readiness" not in steps


# --- shablloni i mjedisit -----------------------------------------------------------------------------------


REQUIRED = {
    "CENTRAL_ENV", "CENTRAL_DATABASE_URL", "CENTRAL_AUTH_SECRET", "CENTRAL_AUTH_TTL_SECONDS",
    "CENTRAL_PUBLIC_REGISTRATION_ENABLED", "CENTRAL_ALLOW_UNVERIFIED_AUTO_REGISTRATION",
    "CENTRAL_REGISTRATION_VERIFY_KEY", "CENTRAL_REGISTRATION_VERIFY_URL_BASE", "CENTRAL_MAILER",
    "CENTRAL_SMTP_HOST", "CENTRAL_SMTP_PORT", "CENTRAL_SMTP_USER", "CENTRAL_SMTP_PASSWORD",
    "CENTRAL_SMTP_STARTTLS", "CENTRAL_SMTP_FROM", "CENTRAL_BOT_CHALLENGE",
    "CENTRAL_PUBLIC_REGISTRATION_REQUIRE_CHALLENGE", "CENTRAL_TRUSTED_PROXY_HOPS",
    "CENTRAL_BILLING_WORKER_CONFIGURED", "CENTRAL_ISSUER_NAME", "CENTRAL_ISSUER_ADDRESS",
    "CENTRAL_ISSUER_TAX_ID", "CENTRAL_INVOICE_DUE_DAYS",
}  # fmt: skip


def _template():
    active, all_names = {}, set()
    for line in ENV_EXAMPLE.splitlines():
        m = re.match(r"^(#\s*)?(CENTRAL_[A-Z0-9_]+)=(.*?)\s*(?:#.*)?$", line)
        if m:
            all_names.add(m.group(2))
            if not m.group(1):
                active[m.group(2)] = m.group(3)
    return active, all_names


def test_env_template_covers_every_central_setting_and_has_no_unknown_names():
    _, names = _template()
    fields = {"CENTRAL_" + f.upper() for f in Settings.model_fields}
    assert names == fields
    assert REQUIRED <= names


def test_env_template_has_only_placeholders_and_safe_defaults():
    active, _ = _template()
    assert active["CENTRAL_ENV"] == "production"
    assert active["CENTRAL_AUTH_SECRET"] == "" and active["CENTRAL_REGISTRATION_VERIFY_KEY"] == ""
    assert active["CENTRAL_SMTP_PASSWORD"] == "" and active["CENTRAL_SMTP_USER"] == ""
    assert active["CENTRAL_ALLOW_UNVERIFIED_AUTO_REGISTRATION"] == "false"
    assert active["CENTRAL_PUBLIC_REGISTRATION_ENABLED"] == "false"
    assert active["CENTRAL_BILLING_WORKER_CONFIGURED"] == "false"
    assert active["CENTRAL_MAILER"] == "disabled" and active["CENTRAL_BOT_CHALLENGE"] == "disabled"
    assert active["CENTRAL_TRUSTED_PROXY_HOPS"] == "1"
    url = active["CENTRAL_DATABASE_URL"]
    assert url.startswith("postgresql+psycopg://") and url.endswith("/sms_central")
    assert "CHANGE_ME" in url
    for name, value in active.items():  # asnjë sekret/çelës real (vlera të gjata të rastësishme)
        if name != "CENTRAL_DATABASE_URL":
            assert not re.search(r"[A-Za-z0-9+/=_-]{32,}", value), name
    # shablloni, kur ngarkohet siç është, NUK nis prodhimin (sekret bosh): gate-t e sigurisë mbeten të vlefshme
    settings = Settings(
        _env_file=None, **{k[len("CENTRAL_") :].lower(): v for k, v in active.items() if v != ""}
    )
    assert settings.env == "production" and settings.auth_secret == ""


# --- gate-t e nisjes ----------------------------------------------------------------------------------------


PG = "postgresql+psycopg://u:p@localhost/db"  # create_engine nuk lidhet; vetëm dialekti


def test_production_rejects_sqlite_and_any_non_postgresql_database(monkeypatch):
    from apps.central.core import tokens
    from apps.central.main import settings as central_settings

    monkeypatch.setattr(central_settings, "env", "production")
    monkeypatch.setattr(tokens, "configured", lambda: True)
    for url in ("sqlite://", "sqlite:///./central_dev.db"):
        with pytest.raises(RuntimeError, match="must be PostgreSQL in production"):
            create_app(create_engine(url))


def test_production_with_postgresql_dialect_passes_this_gate_and_development_keeps_sqlite(
    monkeypatch,
):
    from apps.central.core import tokens
    from apps.central.main import settings as central_settings

    monkeypatch.setattr(central_settings, "env", "production")
    monkeypatch.setattr(tokens, "configured", lambda: True)
    assert create_app(create_engine(PG)).title == "SMS Central"  # pa lidhje reale
    monkeypatch.setattr(central_settings, "env", "development")
    assert create_app(create_engine("sqlite://")).title == "SMS Central"


def test_existing_production_gates_still_fire_before_the_database_gate(monkeypatch):
    from apps.central.core import tokens
    from apps.central.main import settings as central_settings

    monkeypatch.setattr(central_settings, "env", "production")
    monkeypatch.setattr(tokens, "configured", lambda: False)
    with pytest.raises(RuntimeError, match="CENTRAL_AUTH_SECRET"):
        create_app(create_engine("sqlite://"))
    monkeypatch.setattr(tokens, "configured", lambda: True)
    monkeypatch.setattr(central_settings, "allow_unverified_auto_registration", True)
    with pytest.raises(RuntimeError, match="UNVERIFIED_AUTO_REGISTRATION"):
        create_app(create_engine(PG))
    monkeypatch.setattr(central_settings, "allow_unverified_auto_registration", False)
    monkeypatch.setattr(central_settings, "mailer", "fake")
    with pytest.raises(RuntimeError, match="CENTRAL_MAILER"):
        create_app(create_engine(PG))
    monkeypatch.setattr(central_settings, "mailer", "disabled")
    monkeypatch.setattr(central_settings, "bot_challenge", "fake")
    with pytest.raises(RuntimeError, match="CENTRAL_BOT_CHALLENGE"):
        create_app(create_engine(PG))


# --- migrimi: koka dhe mbrojtja kundër DB-së Enterprise -------------------------------------------------------


CENTRAL_INI = str(ROOT / "apps" / "central" / "alembic.ini")


def _alembic(url, *args, env_extra=None):
    import os

    env = {**os.environ, "CENTRAL_DATABASE_URL": url, **(env_extra or {})}
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", CENTRAL_INI, *args],
        env=env, cwd=ROOT, capture_output=True, text=True,
    )  # fmt: skip


def test_central_migration_head_is_0031_and_unique(tmp_path):
    r = _alembic(f"sqlite:///{tmp_path / 'c.db'}", "heads")
    assert (
        r.returncode == 0
        and r.stdout.split()[0] == "0031"
        and len(r.stdout.strip().splitlines()) == 1
    )


def test_central_migrate_refuses_an_enterprise_database(tmp_path):
    url = f"sqlite:///{tmp_path / 'ent.db'}"
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("create table sms_alembic_version (version_num varchar(32) not null)"))
    r = _alembic(url, "upgrade", "head")
    assert r.returncode != 0 and "looks like an Enterprise database" in r.stderr
    with eng.connect() as c:  # asgjë s'u krijua në DB-në Enterprise
        tables = {
            t for (t,) in c.execute(text("select name from sqlite_master where type='table'"))
        }
    assert tables == {"sms_alembic_version"}


def test_central_migrate_upgrades_a_clean_database_and_production_requires_postgresql(tmp_path):
    url = f"sqlite:///{tmp_path / 'ok.db'}"
    assert _alembic(url, "upgrade", "head").returncode == 0
    r = _alembic(url, "upgrade", "head", env_extra={"CENTRAL_ENV": "production"})
    assert r.returncode != 0 and "must be PostgreSQL in production" in r.stderr
