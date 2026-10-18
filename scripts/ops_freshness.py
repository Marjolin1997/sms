"""Zbulimi i job-eve të planifikuara të dështuara, të munguara ose të vjetruara + freskia e backup-it (M11-I/F-06). VETËM-LEXIM; pa sekrete.

    python -m scripts.ops_freshness --state-dir DIR [--inventory deploy/scheduled-jobs.json] [--environment production]
                                    [--only a,b] [--backup-dir DIR] [--now 2030-01-01T00:00:00Z] [--json] [--strict]

Lexon statuset që shkruan `scripts.job_runner` (`<state-dir>/<job>.json`) dhe i krahason me inventarin `deploy/scheduled-jobs.json`:
  · FAILED  — ekzekutimi i fundit dështoi/timeout dhe s'ka sukses më të ri (rritet te `escalated_severity` pas `escalate_after_failures`);
  · STALE   — suksesi i fundit është më i vjetër se `max_age_seconds`;
  · MISSING — job i detyrueshëm pa asnjë status (s'është ekzekutuar kurrë / runner-i s'është konfiguruar);
  · STUCK   — "running" prej më shumë se 2× timeout-i;
  · backup: artefakti më i ri `sms-<etiketa>-*.dump` + `.meta.json` (me `--backup-dir`): mosha, ekzistenca dhe madhësia e dump-it.
Job-et me `required: false` kontrollohen vetëm nëse kanë status (p.sh. faturimi Central para cutover-it). Job-et `environment` ≠ `--environment` anashkalohen.
Shërbimet (`kind: service`) mbulohen nga healthcheck-u Docker (`scripts.worker_health`), jo nga ky skript.
P1/P2 ⇒ nivel FAIL; P3 ⇒ WARN. Kodi: 0 pa FAIL (me `--strict`: edhe pa WARN) · 1 ka FAIL/WARN · 2 gabim përdorimi/inventari. Nuk printon komanda, argumente apo mjedis."""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INVENTORY = ROOT / "deploy" / "scheduled-jobs.json"
SEVERITIES = ("P1", "P2", "P3")
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
ONESHOT_REQUIRED = (
    "name", "command", "cadence_seconds", "timeout_seconds", "max_age_seconds",
    "missed_severity", "failed_severity", "owner", "environment", "required",
)  # fmt: skip


class ConfigError(Exception):
    pass


def parse_ts(value) -> datetime | None:
    try:
        return datetime.strptime(str(value), TS_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def level_of(severity: str) -> str:
    return "WARN" if severity == "P3" else "FAIL"


def load_inventory(path: Path) -> list[dict]:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read the job inventory ({type(exc).__name__})") from None
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if doc.get("schema_version") != 1 or not isinstance(jobs, list):
        raise ConfigError(
            "unsupported job inventory (schema_version 1 with a 'jobs' list required)"
        )
    seen = set()
    for j in jobs:
        n = j.get("name")
        if not isinstance(n, str) or n in seen:
            raise ConfigError(f"duplicate or missing job name: {n!r}")
        seen.add(n)
        if j.get("kind", "oneshot") != "oneshot":
            continue
        miss = [k for k in ONESHOT_REQUIRED if k not in j]
        if miss:
            raise ConfigError(f"job {n}: missing fields {miss}")
        for k in ("cadence_seconds", "timeout_seconds", "max_age_seconds"):
            if not isinstance(j[k], int | float) or isinstance(j[k], bool) or j[k] <= 0:
                raise ConfigError(f"job {n}: {k} must be a positive number")
        if j["max_age_seconds"] < j["cadence_seconds"]:
            raise ConfigError(
                f"job {n}: max_age_seconds is below the cadence (would alert on every run)"
            )
        for k in ("missed_severity", "failed_severity", "escalated_severity"):
            if k in j and j[k] not in SEVERITIES:
                raise ConfigError(f"job {n}: {k} must be one of {SEVERITIES}")
    return jobs


def _row(name, status, severity, detail):
    level = "OK" if status in ("OK", "SKIP") else level_of(severity)
    return {
        "check": name,
        "status": status,
        "level": level,
        "severity": None if level == "OK" else severity,
        "detail": detail,
    }


def check_job(job: dict, state_dir: Path, now: datetime) -> dict:
    name = job["name"]
    path = state_dir / f"{name}.json"
    try:
        st = json.loads(path.read_text())
        if not isinstance(st, dict):
            raise ValueError
    except FileNotFoundError:
        if job["required"]:
            return _row(
                name,
                "MISSING",
                job["missed_severity"],
                "no status file: the job has never run through job_runner",
            )
        return _row(name, "SKIP", "P3", "optional job without status (not enabled here)")
    except (OSError, ValueError):
        return _row(name, "FAILED", job["failed_severity"], "status file is unreadable or corrupt")

    outcome = st.get("outcome")
    last_success = parse_ts(st.get("last_success_utc"))
    last_end = parse_ts(st.get("last_end_utc"))
    fails = int(st.get("consecutive_failures", 0) or 0)

    if outcome in ("failed", "timeout") and (
        last_success is None or (last_end and last_end > last_success)
    ):
        sev = job["failed_severity"]
        if "escalate_after_failures" in job and fails >= job["escalate_after_failures"]:
            sev = job.get("escalated_severity", sev)
        return _row(
            name,
            "FAILED",
            sev,
            f"last run {outcome} (exit {st.get('exit_code')}), {fails} consecutive failure(s)",
        )
    if outcome == "running":
        started = parse_ts(st.get("last_start_utc"))
        if started and (now - started).total_seconds() > 2 * job["timeout_seconds"]:
            return _row(
                name,
                "STUCK",
                job["missed_severity"],
                "marked running for more than 2x the timeout (runner died?)",
            )
    if last_success is None:
        return _row(name, "MISSING", job["missed_severity"], "no successful run recorded")
    age = (now - last_success).total_seconds()
    if age > job["max_age_seconds"]:
        return _row(
            name,
            "STALE",
            job["missed_severity"],
            f"last success {int(age)}s ago (max {int(job['max_age_seconds'])}s)",
        )
    return _row(name, "OK", "P3", f"last success {int(age)}s ago")


def check_backup(job: dict, backup_dir: Path, now: datetime) -> dict:
    label, name = job["backup_label"], f"{job['name']}-artifact"
    metas = sorted(backup_dir.glob(f"sms-{label}-*.dump.meta.json"))
    if not metas:
        return _row(
            name,
            "MISSING",
            job["missed_severity"],
            f"no {label} backup metadata in the backup directory",
        )
    try:
        meta = json.loads(metas[-1].read_text())
        created = datetime.strptime(meta["created_utc"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        dump = backup_dir / meta["file"]
        size_ok = dump.is_file() and dump.stat().st_size == int(meta["bytes"])
        has_sum = (backup_dir / f"{meta['file']}.sha256").is_file()
    except (OSError, ValueError, KeyError, TypeError):
        return _row(name, "FAILED", job["failed_severity"], "newest backup metadata is unreadable")
    if not (size_ok and has_sum):
        return _row(
            name,
            "FAILED",
            job["failed_severity"],
            "newest backup is incomplete (dump missing/size mismatch or no .sha256)",
        )
    age = (now - created).total_seconds()
    if age > job["max_age_seconds"]:
        return _row(
            name,
            "STALE",
            job["missed_severity"],
            f"newest backup is {int(age)}s old (max {int(job['max_age_seconds'])}s)",
        )
    return _row(name, "OK", "P3", f"newest backup is {int(age)}s old")


def run(inventory, state_dir, environment, only, backup_dir, now) -> list[dict]:
    rows = []
    for job in inventory:
        if job.get("kind", "oneshot") != "oneshot" or job["environment"] != environment:
            continue
        if only and job["name"] not in only:
            continue
        rows.append(check_job(job, state_dir, now))
        if backup_dir is not None and job.get("backup_label"):
            rows.append(check_backup(job, backup_dir, now))
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Detect failed, missing or stale scheduled jobs and stale backups (read-only)."
    )
    ap.add_argument("--state-dir", default=os.environ.get("SMS_JOB_STATE_DIR"))
    ap.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    ap.add_argument("--environment", default="production")
    ap.add_argument("--only", help="lista e job-eve (emra, me presje)")
    ap.add_argument("--backup-dir", type=Path)
    ap.add_argument("--now", help="UTC ISO (testim): YYYY-MM-DDTHH:MM:SSZ")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args(argv)
    try:
        if not args.state_dir:
            raise ConfigError("set --state-dir or SMS_JOB_STATE_DIR")
        inventory = load_inventory(args.inventory)
        known = {j["name"] for j in inventory}
        only = {x.strip() for x in args.only.split(",")} if args.only else None
        if only and not only <= known:
            raise ConfigError(f"unknown job(s): {sorted(only - known)}")
        now = parse_ts(args.now) if args.now else datetime.now(UTC)
        if now is None:
            raise ConfigError("--now must look like 2030-01-01T00:00:00Z")
        state_dir = Path(args.state_dir)
        if not state_dir.is_dir():
            raise ConfigError("the state directory does not exist")
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    rows = run(inventory, state_dir, args.environment, only, args.backup_dir, now)
    bad = [r for r in rows if r["level"] == "FAIL" or (args.strict and r["level"] == "WARN")]
    if args.json:
        print(
            json.dumps(
                {"generated_at": now.strftime(TS_FORMAT), "ok": not bad, "checks": rows},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        for r in rows:
            sev = f" [{r['severity']}]" if r["severity"] else ""
            print(f"{r['level']} {r['check']}: {r['status']}{sev} {r['detail']}")
        print(f"checked {len(rows)} item(s): {'OK' if not bad else 'PROBLEMS'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
