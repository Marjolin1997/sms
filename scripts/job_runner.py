"""Mbështjellës për job-et e planifikuara (M11-I/F-06): një ekzekutim i vetëm në çdo kohë, timeout i qartë, status i lexueshëm nga makinat.

    python -m scripts.job_runner --name <emri> --timeout <sekonda> [--state-dir DIR] [--ok-codes 0,1] -- <komanda …>

Çfarë garanton (vendor-neutral, pa varësi të jashtme; vetëm biblioteka standarde):
  · LOCK: `flock` ekskluziv jo-bllokues te `<state-dir>/<emri>.lock` ⇒ dy ekzekutime të të njëjtit job NUK mbivendosen në këtë host;
    nëse kyçi mbahet: dalje 75 (EX_TEMPFAIL), statusi regjistron `skipped_locked` (s'prek `last_success`). Ky NUK zëvendëson mbrojtjen
    e vetë job-it në DB (kyç advisory / FOR UPDATE): ajo mbron edhe nga dy host-e.
  · TIMEOUT: pas `--timeout` s e gjithë grupi i proceseve merr SIGTERM, pas 10 s SIGKILL; dalje 124, statusi `timeout`.
  · STATUS: `<state-dir>/<emri>.json` (shkrim atomik, pa komandë/argumente/mjedis — vetëm emri i modulit): `outcome`
    (success|failed|timeout|skipped_locked), `last_start_utc`, `last_end_utc`, `last_success_utc`, `exit_code`, `duration_s`, `consecutive_failures`.
    `scripts.ops_freshness` e lexon për të zbuluar job-e të dështuar, të munguar ose të vjetruar.
  · PA RETRY: runner-i NUK e rinis kurrë një komandë të dështuar (retry i ekzekutimit të parave do të rrezikonte efekte të dyfishta);
    përpjekja tjetër është ekzekutimi i radhës i planifikuar, dhe job-et financiare janë vetë idempotente (shih docs).
Kodi i daljes: kodi i komandës fëmijë · 124 timeout · 75 job tjetër në vazhdim · 2 gabim përdorimi. `--ok-codes` (parazgjedhje 0) liston kodet që
llogariten sukses (p.sh. 0,1 për një readiness ku 1 = FAIL i raportuar, jo dështim i job-it — por atëherë statusi e shënon `exit_code`)."""

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
EX_TEMPFAIL = 75
EX_TIMEOUT = 124
GRACE_SECONDS = 10.0


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def command_label(argv: list[str]) -> str:
    """Etiketë pa argumente (mund të përmbajnë sekrete): `-m modul` ose emri bazë i ekzekutuesit."""
    if "-m" in argv[:3]:
        i = argv.index("-m")
        if i + 1 < len(argv) and re.fullmatch(r"[A-Za-z_][\w.]*", argv[i + 1]):
            return f"python -m {argv[i + 1]}"
    return os.path.basename(argv[0])


def read_status(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def write_status(path: Path, doc: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, sort_keys=True, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o640)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def parse_codes(text: str) -> set[int]:
    try:
        codes = {int(x) for x in text.split(",") if x.strip() != ""}
    except ValueError:
        raise argparse.ArgumentTypeError(
            "--ok-codes must be a comma-separated list of integers"
        ) from None
    if not codes or any(c < 0 or c > 255 for c in codes):
        raise argparse.ArgumentTypeError("--ok-codes needs at least one value in 0..255")
    return codes


def run_command(argv: list[str], timeout: float) -> tuple[str, int]:
    """→ ('ok'|'timeout', kodi). Grup i ri procesesh: timeout vret edhe fëmijët."""
    proc = subprocess.Popen(argv, start_new_session=True)  # noqa: S603  # nosec B603 - argv i dhënë nga operatori, pa shell
    try:
        return "ok", proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        for sig, wait in ((signal.SIGTERM, GRACE_SECONDS), (signal.SIGKILL, GRACE_SECONDS)):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                proc.wait(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
        return "timeout", EX_TIMEOUT


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run a scheduled job once: lock, timeout, status file; never retries."
    )
    ap.add_argument("--name", required=True)
    ap.add_argument("--timeout", type=float, required=True, help="sekonda; 0 < timeout <= 86400")
    ap.add_argument("--state-dir", default=os.environ.get("SMS_JOB_STATE_DIR"))
    ap.add_argument("--ok-codes", type=parse_codes, default={0})
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args(argv)
    cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not NAME_RE.fullmatch(args.name):
        print("ERROR: --name must match ^[a-z][a-z0-9_-]{0,63}$", file=sys.stderr)
        return 2
    if not (0 < args.timeout <= 86400):
        print("ERROR: --timeout must be in (0, 86400] seconds", file=sys.stderr)
        return 2
    if not cmd:
        print("ERROR: no command given (use: -- <command …>)", file=sys.stderr)
        return 2
    if not args.state_dir:
        print("ERROR: set --state-dir or SMS_JOB_STATE_DIR (no implicit location)", file=sys.stderr)
        return 2
    state = Path(args.state_dir)
    try:
        state.mkdir(parents=True, exist_ok=True, mode=0o750)
        lock_fd = os.open(state / f"{args.name}.lock", os.O_CREAT | os.O_RDWR, 0o640)
    except OSError as exc:
        print(f"ERROR: cannot use the state directory ({type(exc).__name__})", file=sys.stderr)
        return 2
    status_path = state / f"{args.name}.json"
    prev = read_status(status_path)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        write_status(
            status_path,
            {**prev, "job": args.name, "outcome": "skipped_locked", "last_skip_utc": utc_now()},
        )
        print(f"job {args.name}: another run is in progress, skipped", file=sys.stderr)
        os.close(lock_fd)
        return EX_TEMPFAIL
    start_utc, t0 = utc_now(), time.monotonic()
    write_status(
        status_path, {**prev, "job": args.name, "outcome": "running", "last_start_utc": start_utc}
    )
    try:
        try:
            kind, code = run_command(cmd, args.timeout)
        except OSError as exc:  # komanda nuk ekziston / nuk ekzekutohet
            print(f"ERROR: cannot start the command ({type(exc).__name__})", file=sys.stderr)
            kind, code = "ok", 127
    finally:
        duration = round(time.monotonic() - t0, 3)
    end_utc = utc_now()
    success = kind == "ok" and code in args.ok_codes
    outcome = "success" if success else ("timeout" if kind == "timeout" else "failed")
    doc = {
        "job": args.name,
        "command": command_label(cmd),
        "outcome": outcome,
        "exit_code": code,
        "ok_codes": sorted(args.ok_codes),
        "timeout_s": args.timeout,
        "last_start_utc": start_utc,
        "last_end_utc": end_utc,
        "duration_s": duration,
        "last_success_utc": end_utc if success else prev.get("last_success_utc"),
        "consecutive_failures": 0 if success else int(prev.get("consecutive_failures", 0)) + 1,
    }
    try:
        write_status(status_path, doc)
    except OSError as exc:
        print(f"WARNING: could not write the job status ({type(exc).__name__})", file=sys.stderr)
    finally:
        os.close(lock_fd)
    return code


if __name__ == "__main__":
    sys.exit(main())
