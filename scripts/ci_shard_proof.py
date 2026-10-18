"""Provë se shard-et e CI ndajnë suitën pa dublikim dhe pa mungesa (vetëm collection, pa teste).

    python -m scripts.ci_shard_proof --mode pg --shards 4
    python -m scripts.ci_shard_proof --mode sqlite --shards 2

Mbledh suitën e plotë dhe çdo shard (me të njëjtin mjedis si CI), pastaj kontrollon: bashkimi i
shard-eve = suita e plotë; prerja e çdo çifti = bosh; çdo test në një shard. Dalja 0 = OK."""

import argparse
import os
import subprocess
import sys

# vetëm për collection (s'lidhet kurrë)
PG_DUMMY = "postgresql+psycopg://sms:sms@localhost:5432/sms_collect_only"


def collect(mode: str, extra: list[str]) -> list[str]:
    env = {k: v for k, v in os.environ.items() if k != "SMS_TEST_DATABASE_URL"}
    if mode == "pg":
        env["SMS_TEST_DATABASE_URL"] = os.environ.get("SMS_TEST_DATABASE_URL") or PG_DUMMY
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        *extra,
        "tests",
    ]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode not in (0, 5):
        sys.stderr.write(r.stdout[-2000:] + r.stderr[-2000:])
        raise SystemExit(f"collection failed (exit {r.returncode})")
    return [ln for ln in r.stdout.splitlines() if "::" in ln and not ln.startswith(("=", " "))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("pg", "sqlite"), required=True)
    ap.add_argument("--shards", type=int, required=True)
    a = ap.parse_args()
    full = collect(a.mode, [])
    assert len(full) == len(set(full)), "duplicate node ids in the full collection"
    shards = [
        collect(
            a.mode,
            ["-p", "tests.ci_shard", "--shard-index", str(i), "--shard-count", str(a.shards)],
        )
        for i in range(a.shards)
    ]
    ok = True
    union: set[str] = set()
    for i, s in enumerate(shards):
        dup = len(s) - len(set(s))
        print(f"shard {i}: {len(s)} tests" + (f" ({dup} DUPLICATES inside)" if dup else ""))
        ok &= dup == 0
        union |= set(s)
    overlap = sum(
        len(set(shards[i]) & set(shards[j]))
        for i in range(a.shards)
        for j in range(i + 1, a.shards)
    )
    missing, extra = set(full) - union, union - set(full)
    print(f"mode={a.mode} full={len(full)} union={len(union)} sum(shards)={sum(map(len, shards))} "
          f"overlap={overlap} missing={len(missing)} extra={len(extra)}")  # fmt: skip
    ok &= overlap == 0 and not missing and not extra and sum(map(len, shards)) == len(full)
    print("PARTITION OK" if ok else "PARTITION BROKEN")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
