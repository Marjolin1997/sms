"""Krahasim A/B i dual-write (SMS_ENTERPRISE_DUAL_WRITE=true|false) me kushte identike.

Çdo ekzekutim: bazë e re e kopjuar nga i njëjti TEMPLATE (dataset identik), i njëjti host/DB/konfigurim,
2 procese uvicorn, 2 workers, i njëjti numër mesazhesh dhe konkurrencë; asnjë ndryshim kodi mes tyre.
Renditja është e kundërbalancuar (ON,OFF,OFF,ON,ON,OFF) kundër drift-it të hostit.
  accept: POST /v1/messages (A1 një wallet, A2 20 llogari): req/s, p50/p95/p99
  drain:  workers shkarkojnë të njëjtën radhë (template me 6000 mesazhe në radhë): mesazhe/s
Matet edhe CPU e sistemit, CPU e proceseve të aplikacionit (rusage) dhe ngarkesa e DB (pg_stat_database).

    python -m scripts.bench_ab --runs 3 --messages 3000 --out /tmp/ab.json
Kërkon PostgreSQL lokal (përdoruesi sms/sms), bazat `*_bench` krijohen/fshihen vetë."""

import argparse
import json
import os
import resource
import statistics
import subprocess
import sys
import time
import urllib.request

PGURL = os.environ.get("BENCH_PG_URL", "postgresql://sms:sms@127.0.0.1:5432")
ENV = os.environ | {
    "PGPASSWORD": "sms",
    "SMS_PII_HMAC_KEY": "bench-pii-key-0123456789abcdef012345",
    "SMS_SECRETS_KEY": "wV0dVQ1nH7xk2m3bYw0m8y7QbKpZ0o1o9mGQ0mF0dJQ=",
    "SMS_ADMIN_API_KEY": "bench-admin",
    "PYTHONPATH": ".",
}
KEYS = "/tmp/bench_ab_keys.json"
ORDER = ["ON", "OFF", "OFF", "ON", "ON", "OFF"]


def psql(sql, db="postgres"):
    r = subprocess.run(
        ["psql", f"{PGURL}/{db}", "-Atqc", sql], env=ENV, capture_output=True, text=True
    )
    if r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout.strip()


def dburl(db):
    return PGURL.replace("postgresql://", "postgresql+psycopg://") + f"/{db}"


def recreate(db, template=None):
    psql(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
    psql(f'CREATE DATABASE "{db}"' + (f' TEMPLATE "{template}"' if template else ""))


def env_for(db, mode):
    return ENV | {
        "SMS_DATABASE_URL": dburl(db),
        "SMS_ENTERPRISE_DUAL_WRITE": "true" if mode == "ON" else "false",
        "SMS_TENANT_SCOPING": "enterprise"
        if mode == "ON"
        else "owner_ref",  # M1c (ignorohet para tij)
    }


def cpu_stat():
    f = open("/proc/stat").readline().split()[1:]
    vals = list(map(int, f))
    idle = vals[3] + vals[4]
    return sum(vals) - idle, sum(vals)


def db_stats(db):
    row = psql(
        f"select xact_commit, tup_inserted, tup_updated, tup_fetched, blks_hit+blks_read from pg_stat_database where datname='{db}'",
        db,
    )
    return list(map(int, row.split("|")))


def wait_ready(url="http://127.0.0.1:8000/readyz", timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if urllib.request.urlopen(url, timeout=2).status == 200:
                return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("API nuk u ngrit")


def measure(fn):
    """Ekzekuto fn(); → (rezultati, {cpu_sys_pct, cpu_app_s, db deltas, wall})."""
    busy0, tot0 = cpu_stat()
    ru0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.perf_counter()
    res = fn()
    wall = time.perf_counter() - t0
    busy1, tot1 = cpu_stat()
    ru1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    return res, {
        "wall_s": wall,
        "cpu_system_pct": 100 * (busy1 - busy0) / max(tot1 - tot0, 1),
        "cpu_app_s": (ru1.ru_utime + ru1.ru_stime) - (ru0.ru_utime + ru0.ru_stime),
    }


def run_bench(db, mode, *args):
    out = f"/tmp/bench_ab_out_{os.getpid()}.json"
    subprocess.run(
        [
            sys.executable,
            "-u",
            "-m",
            "scripts.bench",
            "--keys-file",
            KEYS,
            "--json-out",
            out,
            *args,
        ],
        env=env_for(db, mode),
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return json.load(open(out))


def accept_run(mode, messages, conc):
    recreate("run_bench", "tpl_bench")
    api = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--port",
            "8000",
            "--workers",
            "2",
            "--no-proxy-headers",
            "--log-level",
            "warning",
        ],
        env=env_for("run_bench", mode),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_ready()
        s0 = db_stats("run_bench")

        def go():
            return run_bench(
                "run_bench",
                mode,
                "--phase",
                "accept",
                "--messages",
                str(messages),
                "--concurrency",
                str(conc),
            )

        res, m = measure(go)
        s1 = db_stats("run_bench")
    finally:
        api.terminate()
        api.wait(timeout=20)
    m["db_xact"] = s1[0] - s0[0]
    m["db_tup_ins"] = s1[1] - s0[1]
    m["db_tup_upd"] = s1[2] - s0[2]
    return res, m


def drain_run(mode, workers):
    recreate("run_bench", "tplq_bench")
    s0 = db_stats("run_bench")
    res, m = measure(
        lambda: run_bench("run_bench", mode, "--phase", "drain", "--workers", str(workers))
    )
    s1 = db_stats("run_bench")
    m["db_xact"] = s1[0] - s0[0]
    m["db_tup_ins"] = s1[1] - s0[1]
    m["db_tup_upd"] = s1[2] - s0[2]
    return res, m


def stats(vals):
    return {"runs": vals, "mean": statistics.mean(vals), "median": statistics.median(vals),
            "min": min(vals), "max": max(vals), "stdev": statistics.pstdev(vals)}  # fmt: skip


def report(name, on, off, unit, higher_better=True):
    a, b = stats(on), stats(off)
    d_mean = 100 * (a["mean"] - b["mean"]) / b["mean"]
    d_med = 100 * (a["median"] - b["median"]) / b["median"]
    print(f"\n{name} [{unit}]")
    for label, s in (("ON ", a), ("OFF", b)):
        print(f"  {label} runs: {', '.join(f'{v:.1f}' for v in s['runs'])}")
        print(
            f"      mean {s['mean']:.1f}  median {s['median']:.1f}  min {s['min']:.1f}  max {s['max']:.1f}  stdev {s['stdev']:.1f}"
        )
    verdict = "më keq" if (d_mean < 0) == higher_better else "më mirë"
    print(
        f"  ndryshimi ON vs OFF: mesatare {d_mean:+.1f}%  mediane {d_med:+.1f}%  ({verdict} me ON)"
    )
    return {"on": a, "off": b, "diff_mean_pct": d_mean, "diff_median_pct": d_med}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--messages", type=int, default=3000)
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--out", default="/tmp/bench_ab.json")
    ap.add_argument("--skip-accept", action="store_true")
    a = ap.parse_args()
    order = (ORDER * 3)[: a.runs * 2]
    if os.path.exists(KEYS):
        os.remove(KEYS)
    recreate("tpl_bench")
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env_for("tpl_bench", "OFF"),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.bench",
            "--phase",
            "setup",
            "--accounts",
            "20",
            "--keys-file",
            KEYS,
        ],
        env=env_for("tpl_bench", "OFF"),
        check=True,
        capture_output=True,
    )
    for mod in (["scripts.enterprises_audit", "--backfill"], ["scripts.backfill_enterprise_id"]):
        # M1c: skopimi me enterprise_id është fail-closed → dataseti i përbashkët duhet të ketë backfill
        subprocess.run(
            [sys.executable, "-m", *mod],
            env=env_for("tpl_bench", "ON"),
            check=True,
            capture_output=True,
        )
    results: dict = {"accept": {"ON": [], "OFF": []}, "drain": {"ON": [], "OFF": []}}
    if not a.skip_accept:
        print(f"== ACCEPT: {order}", flush=True)
        for i, mode in enumerate(order, 1):
            res, m = accept_run(mode, a.messages, a.concurrency)
            print(
                f"  run {i} {mode}: A1 {res['A1']['rps']:.0f} req/s p95 {res['A1']['p95']:.0f}ms | A2 {res['A2']['rps']:.0f} req/s p95 {res['A2']['p95']:.0f}ms | cpu sys {m['cpu_system_pct']:.0f}% app {m['cpu_app_s']:.0f}s xact {m['db_xact']}  {res['integrity']}",
                flush=True,
            )
            results["accept"][mode].append({**res, "m": m})
    # dataset identik për drain: 6000 mesazhe në radhë, krijuar një herë (me dual-write ON)
    recreate("tplq_bench", "tpl_bench")
    api = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--port",
            "8000",
            "--workers",
            "2",
            "--no-proxy-headers",
            "--log-level",
            "warning",
        ],
        env=env_for("tplq_bench", "ON"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_ready()
        run_bench(
            "tplq_bench",
            "ON",
            "--phase",
            "accept",
            "--messages",
            str(a.messages),
            "--concurrency",
            str(a.concurrency),
        )
    finally:
        api.terminate()
        api.wait(timeout=20)
    print(f"== DRAIN: {order} (template me {2 * a.messages} mesazhe në radhë)", flush=True)
    for i, mode in enumerate(order, 1):
        res, m = drain_run(mode, a.workers)
        print(
            f"  run {i} {mode}: {res['B']['per_second']:.0f} mesazhe/s ({res['B']['messages']} në {res['B']['seconds']:.1f}s) | cpu sys {m['cpu_system_pct']:.0f}% app {m['cpu_app_s']:.0f}s xact {m['db_xact']} tup_upd {m['db_tup_upd']}  {res['integrity']}",
            flush=True,
        )
        results["drain"][mode].append({**res, "m": m})
    summary = {}
    if not a.skip_accept:
        for key, label in (("A1", "A1 pranim, një wallet"), ("A2", "A2 pranim, 20 llogari")):
            on = [r[key]["rps"] for r in results["accept"]["ON"]]
            off = [r[key]["rps"] for r in results["accept"]["OFF"]]
            summary[key] = report(label, on, off, "req/s")
            for pct in ("p95", "p99"):
                report(
                    f"{label}: latenca {pct}",
                    [r[key][pct] for r in results["accept"]["ON"]],
                    [r[key][pct] for r in results["accept"]["OFF"]],
                    "ms",
                    higher_better=False,
                )
        for f, label, hb in (
            ("cpu_system_pct", "CPU e sistemit gjatë accept", False),
            ("cpu_app_s", "CPU e aplikacionit gjatë accept", False),
            ("db_xact", "transaksione DB gjatë accept", False),
        ):
            report(
                label,
                [r["m"][f] for r in results["accept"]["ON"]],
                [r["m"][f] for r in results["accept"]["OFF"]],
                f,
                higher_better=hb,
            )
    summary["B"] = report(
        "B dërgim nga radha (2 workers)",
        [r["B"]["per_second"] for r in results["drain"]["ON"]],
        [r["B"]["per_second"] for r in results["drain"]["OFF"]],
        "mesazhe/s",
    )
    for f, label in (
        ("cpu_system_pct", "CPU e sistemit gjatë drain"),
        ("cpu_app_s", "CPU e workers gjatë drain"),
        ("db_xact", "transaksione DB gjatë drain"),
        ("db_tup_upd", "tuple të përditësuara gjatë drain"),
    ):
        report(
            label,
            [r["m"][f] for r in results["drain"]["ON"]],
            [r["m"][f] for r in results["drain"]["OFF"]],
            f,
            higher_better=False,
        )
    json.dump({"order": order, "results": results, "summary": summary}, open(a.out, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)
    sys.exit(main())
