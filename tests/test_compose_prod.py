"""Kontrata statike e `docker-compose.prod.yml` për punonjësit M10 (pa Docker): rolet ekzistojnë në aplikacion, profili `sender`, pa porte/privilegje/sekrete, healthcheck i punonjësve."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())
SENDER = {"sender-sync": "sender_control_plane", "sender-report": "sender_request_reporter"}


def _roles() -> set[str]:
    src = (ROOT / "app" / "worker.py").read_text()
    block = src[src.index('"--role"') : src.index("default=")]
    return set(re.findall(r'"([a-z_]+)"', block)) - {"role"}


def test_each_sender_service_runs_the_matching_real_worker_role():
    roles = _roles()
    assert set(SENDER.values()) <= roles
    for name, role in SENDER.items():
        cmd = COMPOSE["services"][name]["command"]
        assert cmd == f"python -m app.worker --role {role}", name


def test_sender_workers_are_opt_in_via_the_sender_profile_only():
    svc = COMPOSE["services"]
    assert all(svc[n]["profiles"] == ["sender"] for n in SENDER)
    always_on = {n for n, s in svc.items() if "profiles" not in s}
    assert not always_on & set(SENDER)  # `up -d` i zakonshëm s'i nis


def test_sender_workers_follow_the_worker_security_and_health_conventions():
    svc = COMPOSE["services"]
    for name in SENDER:
        s = svc[name]
        assert (
            not s.get("ports")
            and not s.get("privileged")
            and not s.get("network_mode")
            and not s.get("container_name")
        )
        assert (
            s["read_only"] is True
            and s["cap_drop"] == ["ALL"]
            and "no-new-privileges:true" in s["security_opt"]
        )
        assert s["restart"] == "unless-stopped"
        assert s["volumes"] == [
            "./secrets/cp_private_key.pem:/run/secrets/cp_private_key:ro"
        ]  # vetëm çelësi, read-only
        assert s["environment"]["SMS_CP_PRIVATE_KEY_PATH"] == "/run/secrets/cp_private_key"
        assert s["healthcheck"]["test"] == ["CMD", "python", "-m", "scripts.worker_health", "180"]
        # nisja s'varet nga Central (fail-static/retry): vetëm db + migrate
        assert set(s["depends_on"]) == {"db", "migrate"}
        assert s["env_file"] == ".env"


def test_compose_has_no_embedded_credentials_and_only_the_web_port_is_published():
    text = (ROOT / "docker-compose.prod.yml").read_text()
    for name in SENDER:
        block = yaml.dump(COMPOSE["services"][name])
        assert not re.search(
            r"(?i)(password|token|secret_key|api_key)\s*:\s*['\"]?[A-Za-z0-9+/=]{12,}",
            block.replace("${POSTGRES_PASSWORD}", ""),
        )
    assert "BEGIN" not in text and not re.search(
        r"https?://[a-z0-9]", text.replace("https://", "") or ""
    )
    published = {n for n, s in COMPOSE["services"].items() if s.get("ports")}
    assert published == {"web"} and COMPOSE["services"]["web"]["ports"] == ["127.0.0.1:8080:8080"]


def test_env_example_documents_the_m10_variables_with_safe_defaults():
    from app.core.config import Settings

    env = dict(
        re.findall(
            r"^(SMS_[A-Z_]+)=([^#\n]*?)\s*(?:#.*)?$", (ROOT / ".env.example").read_text(), re.M
        )
    )
    wanted = {
        "SMS_SENDER_AUTHORITY": "local", "SMS_SENDER_AUTHORITY_ACK": "", "SMS_SENDER_DISPATCH_RECHECK": "true",
        "SMS_SENDER_SYNC_ENABLED": "false", "SMS_SENDER_REQUEST_REPORTING": "false", "SMS_SENDER_SHADOW_SAMPLE_PCT": "10",
        "SMS_SENDER_PROJECTION_FRESH_SECONDS": "3600", "SMS_SENDER_EVIDENCE_MIN_SAMPLES": "100",
        "SMS_SENDER_REQUEST_INTERVAL_SECONDS": "30", "SMS_SENDER_REQUEST_ALERT_AGE_SECONDS": "900",
        "SMS_SENDER_POLL_INTERVAL_SECONDS": "30", "SMS_SENDER_SNAPSHOT_INTERVAL_SECONDS": "3600",
        "SMS_CP_BASE_URL": "", "SMS_CP_CLIENT_ID": "", "SMS_CP_KEY_ID": "",
    }  # fmt: skip
    for k, v in wanted.items():
        assert env.get(k) == v, k
        assert ("SMS_" + k[4:]).lower().removeprefix("sms_") in Settings.model_fields, (
            k
        )  # emër real në kod
    assert Settings.model_fields["sender_authority"].default == "local"
    assert env["SMS_SENDER_AUTHORITY"] not in ("shadow", "central")
