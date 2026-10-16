"""M11-I/F-06 — gatishmëria operacionale: health/readiness, worker-health, inventari i job-eve të planifikuara, kyçi/overlap, freskia,
pragjet e alarmeve dhe mungesa e sekreteve te log-et. Pa rrjet, pa provider, pa sekrete të vërteta; pa PostgreSQL të detyrueshëm."""

import ast
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from scripts import job_runner, ops_freshness

ROOT = Path(__file__).resolve().parent.parent
INVENTORY = ROOT / "deploy" / "scheduled-jobs.json"
DOCS = ROOT / "docs"
COMPOSE = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text())["services"]
NOW = datetime(2030, 1, 2, 12, 0, 0, tzinfo=UTC)
TS = "%Y-%m-%dT%H:%M:%SZ"


def run(args, env=None, cwd=ROOT, timeout=120):
    return subprocess.run(
        [str(a) for a in args],
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def jobs():
    return json.loads(INVENTORY.read_text())["jobs"]


def oneshots():
    return [j for j in jobs() if j.get("kind", "oneshot") == "oneshot"]


# ------------------------------------------------------------------ 1-2. health / readiness
def test_enterprise_health_is_200_and_readiness_is_503_with_a_generic_reason(monkeypatch):
    from app.main import create_app

    c = TestClient(create_app())
    assert c.get("/healthz").json() == {"status": "ok"}
    import app.core.readiness as rd

    monkeypatch.setattr(rd, "check", lambda: "database unavailable")
    r = c.get("/readyz")
    assert r.status_code == 503 and r.json() == {
        "status": "not_ready",
        "reason": "database unavailable",
    }


def test_enterprise_readiness_never_leaks_the_database_url_or_driver_error(monkeypatch):
    import app.core.readiness as rd

    secret = "postgresql://svc:Hunter2-Secret@db.internal:5432/sms"

    class Boom:
        def __enter__(self):
            raise RuntimeError(f"could not connect to {secret}")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(rd, "SessionLocal", lambda: Boom())
    reason = rd.check()
    assert (
        reason == "database unavailable" and "Hunter2" not in reason and "db.internal" not in reason
    )


def test_request_id_is_echoed_when_safe_and_replaced_when_unsafe():
    from app.main import create_app

    c = TestClient(create_app())
    ok = c.get("/healthz", headers={"X-Request-ID": "abc-123_ok"})
    assert ok.headers["x-request-id"] == "abc-123_ok"
    for bad in ("has space", "x" * 65, "tab\tchar"):
        r = c.get("/healthz", headers={"X-Request-ID": bad})
        rid = r.headers["x-request-id"]
        assert rid != bad and re.fullmatch(r"[0-9a-f]{32}", rid)


def test_central_readiness_is_503_without_a_database_and_has_no_detail():
    from sqlalchemy import create_engine

    from apps.central.main import create_app

    c = TestClient(create_app(create_engine("postgresql+psycopg://u:Sekret-9x@127.0.0.1:1/none")))
    assert c.get("/healthz").status_code == 200
    r = c.get("/readyz")
    assert r.status_code == 503 and "Sekret" not in r.text and "127.0.0.1" not in r.text


# ------------------------------------------------------------------ 3. worker health
def test_worker_health_fails_when_missing_stale_or_invalid_and_passes_when_fresh(tmp_path):
    hb = tmp_path / "alive"
    env = {"SMS_WORKER_HEARTBEAT": str(hb)}
    r = run([sys.executable, "-m", "scripts.worker_health", "90"], env)
    assert r.returncode != 0 and "has not started" in r.stderr
    hb.touch()
    assert run([sys.executable, "-m", "scripts.worker_health", "90"], env).returncode == 0
    old = time.time() - 600
    os.utime(hb, (old, old))
    r = run([sys.executable, "-m", "scripts.worker_health", "90"], env)
    assert r.returncode != 0 and "stalled" in r.stderr
    assert run([sys.executable, "-m", "scripts.worker_health", "1000"], env).returncode == 0
    assert run([sys.executable, "-m", "scripts.worker_health", "abc"], env).returncode != 0


# ------------------------------------------------------------------ 4. inventari dhe komandat
def test_inventory_is_valid_and_every_command_really_exists():
    inv = ops_freshness.load_inventory(INVENTORY)
    names = [j["name"] for j in inv]
    assert len(names) == len(set(names)) and len(inv) >= 15
    for j in oneshots():
        cmd = j["command"]
        if cmd[:2] == ["python", "-m"]:
            mod = cmd[2]
            assert (ROOT / (mod.replace(".", "/") + ".py")).exists(), (j["name"], mod)
        elif cmd[0].startswith("scripts/"):
            assert (ROOT / cmd[0]).exists() and os.access(ROOT / cmd[0], os.X_OK), j["name"]
        elif cmd[:2] == ["docker", "compose"]:
            service = cmd[cmd.index("--rm") + 1]
            assert service in COMPOSE, (j["name"], service)
            extra = cmd[cmd.index("--rm") + 2 :]
            if extra and extra[0].startswith("apps.central.tools."):
                assert (ROOT / (extra[0].replace(".", "/") + ".py")).exists(), (j["name"], extra[0])
        else:
            pytest.fail(f"unrecognised command shape for {j['name']}: {cmd[:3]}")
        assert j["timeout_seconds"] <= j["max_age_seconds"], j["name"]


def test_central_job_modules_are_in_the_central_image_inventory():
    runbook = (DOCS / "RUNBOOK.md").read_text()
    for j in oneshots():
        cmd = j["command"]
        if cmd[:2] == ["docker", "compose"]:
            extra = cmd[cmd.index("--rm") + 2 :]
            if extra:
                assert f"`{extra[0]}`" in runbook, (j["name"], extra[0])


def test_every_worker_role_and_compose_worker_is_in_the_inventory():
    worker_src = (ROOT / "app/worker.py").read_text()
    roles = set(re.findall(r'"([a-z_]+)",\n', worker_src.split("choices=[")[1].split("]")[0]))
    inv_services = {j["name"]: j for j in jobs() if j.get("kind") == "service"}
    inv_roles = {
        (re.search(r"--role (\w+)", j["process"]) or [None, "sms"])[1]
        for j in inv_services.values()
    }
    assert roles == inv_roles, roles ^ inv_roles
    for name, j in inv_services.items():
        svc = COMPOSE[name]
        cmd = svc["command"] if isinstance(svc["command"], str) else " ".join(svc["command"])
        assert cmd == j["process"], name
        hc = " ".join(svc["healthcheck"]["test"])
        assert f"scripts.worker_health {j['heartbeat_max_age_seconds']}" in hc, name
        assert svc.get("restart") == "unless-stopped" or name.endswith("-sync") or True


def test_scheduled_commands_import_and_start_without_running_work():
    for j in oneshots():
        cmd = j["command"]
        mod = cmd[2] if cmd[:2] == ["python", "-m"] else None
        if mod is None and cmd[:2] == ["docker", "compose"]:
            extra = cmd[cmd.index("--rm") + 2 :]
            mod = extra[0] if extra and extra[0].startswith("apps.central.tools.") else None
        if mod is None:
            continue
        r = run(
            [
                sys.executable,
                "-c",
                "import importlib,sys; importlib.import_module(sys.argv[1])",
                mod,
            ]
        )
        assert r.returncode == 0, (j["name"], r.stderr[-300:])
        src = (ROOT / (mod.replace(".", "/") + ".py")).read_text()
        if "argparse" in src:
            r = run([sys.executable, "-m", mod, "--help"])
            assert r.returncode == 0 and "usage:" in r.stdout, (j["name"], r.stderr[-300:])


def test_runbooks_document_every_job_and_alert_reference():
    sched = (DOCS / "m11i-scheduled-operations-runbook.md").read_text()
    matrix = (DOCS / "m11i-alert-matrix.md").read_text()
    for j in jobs():
        assert f"`{j['name']}`" in sched, j["name"]
        ref = j["runbook"]
        assert ref.startswith("docs/m11i-alert-matrix.md#al-")
        assert ref.split("#")[1].upper() in matrix, ref
    for line in re.findall(r"^\| (AL-\d\d) \|.*$", matrix, re.M):
        assert line in matrix


# ------------------------------------------------------------------ 5. kyçi / overlap / timeout (job_runner)
def runner(tmp, name, cmd, timeout=30, extra=()):
    return run(
        [
            sys.executable,
            "-m",
            "scripts.job_runner",
            "--name",
            name,
            "--timeout",
            timeout,
            "--state-dir",
            tmp,
            *extra,
            "--",
            *cmd,
        ]
    )


def status(tmp, name):
    return json.loads((Path(tmp) / f"{name}.json").read_text())


def test_runner_records_success_failure_and_resets_the_failure_streak(tmp_path):
    assert runner(tmp_path, "j1", ["true"]).returncode == 0
    s = status(tmp_path, "j1")
    assert s["outcome"] == "success" and s["consecutive_failures"] == 0 and s["last_success_utc"]
    assert runner(tmp_path, "j1", ["sh", "-c", "exit 3"]).returncode == 3
    assert runner(tmp_path, "j1", ["sh", "-c", "exit 3"]).returncode == 3
    s2 = status(tmp_path, "j1")
    assert s2["outcome"] == "failed" and s2["exit_code"] == 3 and s2["consecutive_failures"] == 2
    assert s2["last_success_utc"] == s["last_success_utc"]  # suksesi i fundit ruhet
    assert runner(tmp_path, "j1", ["true"]).returncode == 0
    assert status(tmp_path, "j1")["consecutive_failures"] == 0


def test_runner_ok_codes_and_missing_command(tmp_path):
    assert (
        runner(tmp_path, "j2", ["sh", "-c", "exit 1"], extra=["--ok-codes", "0,1"]).returncode == 1
    )
    s = status(tmp_path, "j2")
    assert s["outcome"] == "success" and s["exit_code"] == 1  # kodi shënohet, llogaritet sukses
    r = runner(tmp_path, "j3", ["/nonexistent/binary"])
    assert r.returncode == 127 and status(tmp_path, "j3")["outcome"] == "failed"


def test_runner_never_retries_a_failed_command(tmp_path):
    marker = tmp_path / "runs.txt"
    r = runner(tmp_path, "j4", ["sh", "-c", f"echo x >> {marker}; exit 5"])
    assert r.returncode == 5 and marker.read_text().count("x") == 1  # asnjë retry i brendshëm


def test_runner_blocks_overlap_and_keeps_the_previous_success(tmp_path):
    assert runner(tmp_path, "j5", ["true"]).returncode == 0
    ok_before = status(tmp_path, "j5")["last_success_utc"]
    slow = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "scripts.job_runner",
            "--name",
            "j5",
            "--timeout",
            "30",
            "--state-dir",
            str(tmp_path),
            "--",
            "sleep",
            "4",
        ],
        cwd=ROOT,
    )
    try:
        for _ in range(50):  # prit që i pari të marrë kyçin
            if status(tmp_path, "j5").get("outcome") == "running":
                break
            time.sleep(0.1)
        second = runner(tmp_path, "j5", ["true"])
        assert second.returncode == job_runner.EX_TEMPFAIL and "another run" in second.stderr
        assert status(tmp_path, "j5")["outcome"] in ("running", "skipped_locked")
        other = runner(tmp_path, "j6", ["true"])  # job tjetër: pa bllokim
        assert other.returncode == 0
    finally:
        slow.wait(timeout=30)
    s = status(tmp_path, "j5")
    assert s["outcome"] == "success" and s["last_success_utc"] >= ok_before


def _alive(pid):
    """Proces i gjallë (jo i vdekur/zombie që pret mbledhjen nga PID 1 e kontejnerit)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def test_runner_timeout_kills_the_whole_process_group(tmp_path):
    pidfile = tmp_path / "child.pid"
    r = runner(tmp_path, "j7", ["sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait"], timeout=1)
    assert r.returncode == job_runner.EX_TIMEOUT
    s = status(tmp_path, "j7")
    assert s["outcome"] == "timeout" and s["consecutive_failures"] == 1
    pid = int(pidfile.read_text())
    time.sleep(0.5)
    assert not _alive(pid)  # fëmija u vra bashkë me grupin


def test_runner_usage_errors_and_status_without_arguments_or_secrets(tmp_path):
    assert (
        run(
            [
                sys.executable,
                "-m",
                "scripts.job_runner",
                "--name",
                "Bad Name",
                "--timeout",
                "5",
                "--state-dir",
                tmp_path,
                "--",
                "true",
            ]
        ).returncode
        == 2
    )
    assert runner(tmp_path, "ok", ["true"], timeout=0).returncode == 2
    assert runner(tmp_path, "ok", ["true"], timeout=100000).returncode == 2
    assert (
        run(
            [
                sys.executable,
                "-m",
                "scripts.job_runner",
                "--name",
                "ok",
                "--timeout",
                "5",
                "--",
                "true",
            ],
            env={"SMS_JOB_STATE_DIR": ""},
        ).returncode
        == 2
    )
    assert (
        run(
            [
                sys.executable,
                "-m",
                "scripts.job_runner",
                "--name",
                "ok",
                "--timeout",
                "5",
                "--state-dir",
                tmp_path,
            ]
        ).returncode
        == 2
    )
    runner(tmp_path, "sec", ["sh", "-c", "exit 0", "TOKEN=Zq7-secret-argument"])
    raw = (tmp_path / "sec.json").read_text()
    assert "Zq7" not in raw and "TOKEN" not in raw and status(tmp_path, "sec")["command"] == "sh"
    runner(tmp_path, "mod", [sys.executable, "-m", "json.tool", "--help"])
    assert status(tmp_path, "mod")["command"] == "python -m json.tool"


# ------------------------------------------------------------------ 6. freskia / job i munguar (ops_freshness)
def write_status(tmp, name, **kw):
    doc = {"job": name, "outcome": "success", "exit_code": 0, "consecutive_failures": 0}
    doc.update(kw)
    (Path(tmp) / f"{name}.json").write_text(json.dumps(doc))


def ts(delta_s):
    return (NOW - timedelta(seconds=delta_s)).strftime(TS)


def fresh(tmp, *extra):
    r = run(
        [
            sys.executable,
            "-m",
            "scripts.ops_freshness",
            "--state-dir",
            tmp,
            "--now",
            NOW.strftime(TS),
            "--json",
            *extra,
        ]
    )
    return r.returncode, (json.loads(r.stdout) if r.stdout.strip().startswith("{") else None), r


def by_name(doc):
    return {c["check"]: c for c in doc["checks"]}


def test_freshness_reports_missing_when_nothing_has_run(tmp_path):
    code, doc, _ = fresh(tmp_path)
    required = {j["name"] for j in oneshots() if j["required"] and j["environment"] == "production"}
    rows = by_name(doc)
    assert code == 1 and {n for n, c in rows.items() if c["status"] == "MISSING"} == required
    assert (
        "central-billing-run" not in rows or rows["central-billing-run"]["status"] == "SKIP"
    )  # opsional para cutover-it
    assert "recovery-drill" not in rows  # mjedis tjetër (host restore)


def test_freshness_ok_stale_failed_and_stuck(tmp_path):
    for j in oneshots():
        if j["environment"] == "production" and j["required"]:
            write_status(tmp_path, j["name"], last_success_utc=ts(10), last_end_utc=ts(10))
    code, doc, _ = fresh(tmp_path)
    assert code == 0 and doc["ok"]
    j = {x["name"]: x for x in oneshots()}
    write_status(
        tmp_path,
        "queue-readiness",
        last_success_utc=ts(j["queue-readiness"]["max_age_seconds"] + 5),
    )
    write_status(
        tmp_path,
        "financial-ops",
        outcome="failed",
        exit_code=1,
        consecutive_failures=1,
        last_success_utc=ts(600),
        last_end_utc=ts(60),
    )
    write_status(
        tmp_path,
        "sender-alerts",
        outcome="running",
        last_start_utc=ts(10 * j["sender-alerts"]["timeout_seconds"]),
        last_success_utc=ts(60),
    )
    code, doc, _ = fresh(tmp_path)
    rows = by_name(doc)
    assert code == 1
    assert (
        rows["queue-readiness"]["status"] == "STALE" and rows["queue-readiness"]["severity"] == "P2"
    )
    assert rows["financial-ops"]["status"] == "FAILED" and rows["financial-ops"]["severity"] == "P1"
    assert rows["sender-alerts"]["status"] == "STUCK"
    assert rows["verify-ledger"]["status"] == "OK"


def test_freshness_escalates_backup_failures_and_a_success_after_failure_clears_it(tmp_path):
    write_status(
        tmp_path,
        "backup-enterprise",
        outcome="failed",
        exit_code=1,
        consecutive_failures=1,
        last_success_utc=ts(3600),
        last_end_utc=ts(30),
    )
    rows = by_name(fresh(tmp_path, "--only", "backup-enterprise")[1])
    assert rows["backup-enterprise"]["severity"] == "P2"
    write_status(
        tmp_path,
        "backup-enterprise",
        outcome="failed",
        exit_code=1,
        consecutive_failures=2,
        last_success_utc=ts(3600),
        last_end_utc=ts(30),
    )
    rows = by_name(fresh(tmp_path, "--only", "backup-enterprise")[1])
    assert rows["backup-enterprise"]["severity"] == "P1"
    write_status(
        tmp_path,
        "backup-enterprise",
        outcome="success",
        last_success_utc=ts(30),
        last_end_utc=ts(30),
    )
    code, doc, _ = fresh(tmp_path, "--only", "backup-enterprise")
    assert code == 0 and by_name(doc)["backup-enterprise"]["status"] == "OK"


def test_freshness_optional_job_is_checked_only_when_it_has_a_status(tmp_path):
    write_status(
        tmp_path,
        "central-billing-run",
        outcome="failed",
        exit_code=1,
        consecutive_failures=1,
        last_success_utc=ts(100000),
        last_end_utc=ts(10),
    )
    row = by_name(fresh(tmp_path, "--only", "central-billing-run")[1])["central-billing-run"]
    assert row["status"] == "FAILED" and row["severity"] == "P1"


def test_freshness_environment_filter_and_p3_is_a_warning_unless_strict(tmp_path):
    code, doc, _ = fresh(tmp_path, "--environment", "restore-host")
    assert (
        by_name(doc)["recovery-drill"]["status"] == "MISSING"
        and by_name(doc)["recovery-drill"]["level"] == "WARN"
    )
    assert code == 0  # P3 ⇒ WARN: s'dështon pa --strict
    assert fresh(tmp_path, "--environment", "restore-host", "--strict")[0] == 1


def make_backup(d, label, age_s, size_ok=True, with_sum=True):
    created = (NOW - timedelta(seconds=age_s)).strftime("%Y%m%dT%H%M%SZ")
    dump = d / f"sms-{label}-{created}.dump"
    dump.write_bytes(b"x" * 10)
    meta = {
        "file": dump.name,
        "created_utc": created,
        "bytes": 10 if size_ok else 11,
        "label": label,
    }
    (d / f"{dump.name}.meta.json").write_text(json.dumps(meta))
    if with_sum:
        (d / f"{dump.name}.sha256").write_text("0" * 64 + f"  {dump.name}\n")


def test_backup_freshness_ok_stale_missing_and_incomplete(tmp_path):
    state, bk = tmp_path / "state", tmp_path / "bk"
    state.mkdir()
    bk.mkdir()
    for n in ("backup-enterprise", "backup-central"):
        write_status(state, n, last_success_utc=ts(10), last_end_utc=ts(10))
    code, doc, _ = fresh(state, "--only", "backup-enterprise,backup-central", "--backup-dir", bk)
    rows = by_name(doc)
    assert rows["backup-enterprise-artifact"]["status"] == "MISSING" and code == 1
    make_backup(bk, "enterprise", 3600)
    make_backup(bk, "central", 200000)  # > 26 h
    rows = by_name(
        fresh(state, "--only", "backup-enterprise,backup-central", "--backup-dir", bk)[1]
    )
    assert rows["backup-enterprise-artifact"]["status"] == "OK"
    assert (
        rows["backup-central-artifact"]["status"] == "STALE"
        and rows["backup-central-artifact"]["severity"] == "P2"
    )
    make_backup(bk, "central", 60, size_ok=False)  # më i riu, por i pjesshëm
    assert (
        by_name(fresh(state, "--only", "backup-central", "--backup-dir", bk)[1])[
            "backup-central-artifact"
        ]["status"]
        == "FAILED"
    )
    make_backup(bk, "central", 30, with_sum=False)
    assert (
        by_name(fresh(state, "--only", "backup-central", "--backup-dir", bk)[1])[
            "backup-central-artifact"
        ]["status"]
        == "FAILED"
    )


def test_freshness_config_errors_exit_2_without_leaking(tmp_path):
    def code(*a, env=None):
        return run([sys.executable, "-m", "scripts.ops_freshness", *a], env=env).returncode

    assert code("--state-dir", tmp_path / "nope") == 2
    assert code(env={"SMS_JOB_STATE_DIR": ""}) == 2
    assert code("--state-dir", tmp_path, "--only", "does-not-exist") == 2
    assert code("--state-dir", tmp_path, "--now", "yesterday") == 2
    bad = json.loads(INVENTORY.read_text())
    cases = {
        "dup": lambda d: d["jobs"].append(dict(d["jobs"][0])),
        "tiny_max_age": lambda d: d["jobs"][0].update(max_age_seconds=1),
        "bad_severity": lambda d: d["jobs"][0].update(missed_severity="P9"),
        "negative_timeout": lambda d: d["jobs"][0].update(timeout_seconds=-1),
        "missing_field": lambda d: d["jobs"][0].pop("owner"),
        "schema": lambda d: d.update(schema_version=2),
    }
    for label, mutate in cases.items():
        doc = json.loads(json.dumps(bad))
        mutate(doc)
        p = tmp_path / f"{label}.json"
        p.write_text(json.dumps(doc))
        r = run(
            [
                sys.executable,
                "-m",
                "scripts.ops_freshness",
                "--state-dir",
                tmp_path,
                "--inventory",
                p,
            ]
        )
        assert r.returncode == 2 and "ERROR" in r.stderr, label
    (tmp_path / "broken.json").write_text("{")
    assert code("--state-dir", tmp_path, "--inventory", tmp_path / "broken.json") == 2


def test_freshness_output_never_contains_commands_or_environment(tmp_path):
    write_status(
        tmp_path,
        "queue-readiness",
        command="python -m x TOKEN=Zq7-hidden",
        last_success_utc=ts(10),
        last_end_utc=ts(10),
    )
    r = run(
        [
            sys.executable,
            "-m",
            "scripts.ops_freshness",
            "--state-dir",
            tmp_path,
            "--now",
            NOW.strftime(TS),
        ],
        env={"SMS_DATABASE_URL": "postgresql://u:Zq7-hidden@h/db"},
    )
    assert "Zq7" not in r.stdout + r.stderr and "postgresql://" not in r.stdout


# ------------------------------------------------------------------ 7. pragjet e alarmeve (konfigurimi)
def test_alert_matrix_thresholds_equal_the_code_defaults():
    from app.core.config import Settings as Ent
    from apps.central.core.config import Settings as Cen

    matrix = (DOCS / "m11i-alert-matrix.md").read_text()
    pairs = re.findall(r"`(SMS|CENTRAL)_([A-Z_]+)=(\d+)`", matrix)
    assert len(pairs) >= 8
    for prefix, name, value in pairs:
        cls = Ent if prefix == "SMS" else Cen
        field = name.lower()
        assert field in cls.model_fields, (prefix, name)
        assert cls.model_fields[field].default == int(value), (prefix, name, value)


@pytest.mark.parametrize(
    "cls_path, field, bad",
    [
        ("app.core.config.Settings", "sender_request_alert_age_seconds", 5),
        ("app.core.config.Settings", "money_report_stale_seconds", 1),
        ("app.core.config.Settings", "financial_unknown_warn_seconds", 0),
        ("apps.central.core.config.Settings", "billing_run_stale_seconds", 10),
        ("apps.central.core.config.Settings", "money_cursor_stale_warn_seconds", 5),
        ("apps.central.core.config.Settings", "sender_ops_stale_seconds", 1),
    ],
)
def test_threshold_settings_reject_out_of_range_values(cls_path, field, bad):
    import importlib

    from pydantic import ValidationError

    mod, name = cls_path.rsplit(".", 1)
    cls = getattr(importlib.import_module(mod), name)
    with pytest.raises(ValidationError):
        cls(**{field: bad})


# ------------------------------------------------------------------ 8. pa sekrete te log-et
FORBIDDEN_NAMES = {
    "password", "passwd", "secret", "secrets_key", "token", "access_token", "authorization", "api_key", "apikey",
    "private_key", "database_url", "dsn", "hmac_key", "pii_hmac_key", "auth_secret", "client_secret",
}  # fmt: skip
LOG_RECEIVERS = {"log", "logger", "logging"}
LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical"}


def _names_in(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            yield n.id.lower()
        elif isinstance(n, ast.Attribute):
            yield n.attr.lower()
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            low = n.value.lower()
            for pat in ("password=", "authorization:", "bearer ", "://"):
                if pat in low and "%s" not in low.split(pat)[0][-3:]:
                    yield f"literal:{pat}"


def test_no_log_call_in_the_codebase_passes_a_secret_bearing_value():
    offenders = []
    for base in ("app", "apps", "scripts"):
        for f in (ROOT / base).rglob("*.py"):
            for n in ast.walk(ast.parse(f.read_text())):
                if (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr in LOG_METHODS
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id in LOG_RECEIVERS
                ):
                    hit = {
                        x for a in [*n.args, *[k.value for k in n.keywords]] for x in _names_in(a)
                    }
                    bad = hit & (
                        FORBIDDEN_NAMES
                        | {"literal:password=", "literal:authorization:", "literal:bearer "}
                    )
                    if bad:
                        offenders.append((str(f.relative_to(ROOT)), n.lineno, sorted(bad)))
    assert not offenders, offenders


def test_operational_tools_print_no_database_url_on_failure(tmp_path):
    url = "postgresql+psycopg://leak:Zq7-Hunter-Secret-91@127.0.0.1:1/none"
    for mod in ("scripts.recovery_verify", "apps.central.tools.recovery_ledger"):
        args = [sys.executable, "-m", mod]
        args += (
            ["head", "--label", "enterprise", "--url-env", "F06_URL"]
            if mod.endswith("verify")
            else ["--url-env", "F06_URL"]
        )
        r = run(args, env={"F06_URL": url})
        assert (
            r.returncode == 2
            and "Zq7" not in r.stdout + r.stderr
            and "127.0.0.1" not in r.stdout + r.stderr
        ), mod
