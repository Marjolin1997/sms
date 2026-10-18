"""Verifikim pas restore për Enterprise dhe Central (M11-I/F-05A). VETËM-LEXIM; asnjë URL/kredencial nuk printohet.

    python -m scripts.recovery_verify head --label enterprise|central [--url-env VAR]
        revizioni i regjistruar në DB = koka(t) e migrimeve të kodit (jo një numër i ngulitur)
    python -m scripts.recovery_verify fingerprint --label L [--url-env VAR] [--out file.json]
        gishti i DB-së: numri i rreshtave për tabelë, trigger-at, indekset, kufizimet, sekuencat, revizioni
    python -m scripts.recovery_verify compare a.json b.json
        dy gishta duhet të jenë identikë (burimi para backup-it vs. baza e rikthyer)
    (ledger-i i Enterprise: `python -m scripts.verify_ledger`; ledger-i i Central: `python -m apps.central.tools.recovery_ledger` —
     mjet i Central, pasi `scripts/` nuk importon kurrë `apps.*`)

URL-ja lexohet nga ndryshorja e mjedisit (parazgjedhje: SMS_DATABASE_URL për enterprise, CENTRAL_DATABASE_URL për central).
Kodi: 0 në rregull · 1 gjetje/mospërputhje · 2 gabim përdorimi/lidhjeje."""

import argparse
import json
import os
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parent.parent
LABELS = {
    "enterprise": {"env": "SMS_DATABASE_URL", "table": "sms_alembic_version"},
    "central": {"env": "CENTRAL_DATABASE_URL", "table": "central_alembic_version"},
}


class Usage(Exception):
    pass


def _engine(label: str, url_env: str | None) -> Engine:
    var = url_env or LABELS[label]["env"]
    url = os.environ.get(var)
    if not url:
        raise Usage(f"environment variable {var} is not set")
    if not url.startswith("postgresql"):
        raise Usage("recovery verification is PostgreSQL-only")
    try:
        return create_engine(url)
    except Exception as exc:  # mesazhi i SQLAlchemy mund të përfshijë URL-në: s'e printojmë
        raise Usage(f"cannot create an engine from {var} ({type(exc).__name__})") from None


def code_heads(label: str) -> list[str]:
    if label == "central":
        cfg = Config(str(ROOT / "apps/central/alembic.ini"))
    else:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return sorted(ScriptDirectory.from_config(cfg).get_heads())


def db_revisions(eng: Engine, label: str) -> list[str]:
    with eng.connect() as c:
        return sorted(
            r[0] for r in c.execute(text(f"select version_num from {LABELS[label]['table']}"))
        )


def cmd_head(label: str, url_env: str | None) -> int:
    eng = _engine(label, url_env)
    try:
        have, want = db_revisions(eng, label), code_heads(label)
    except Exception as exc:
        raise Usage(f"cannot read the {label} version table ({type(exc).__name__})") from None
    ok = have == want
    print(f"{label}: db revision(s)={have} code head(s)={want}: {'OK' if ok else 'MISMATCH'}")
    return 0 if ok else 1


def fingerprint(label: str, eng: Engine) -> dict:
    with eng.connect() as c:
        tables = [
            r[0]
            for r in c.execute(
                text(
                    "select table_name from information_schema.tables "
                    "where table_schema='public' and table_type='BASE TABLE' order by 1"
                )
            )
        ]
        counts = {
            t: int(c.execute(text(f'select count(*) from "{t}"')).scalar_one()) for t in tables
        }
        triggers = sorted(
            f"{r[1]}.{r[0]}"
            for r in c.execute(
                text(
                    "select tgname, tgrelid::regclass::text from pg_trigger where not tgisinternal"
                )
            )
        )
        indexes = [
            r[0]
            for r in c.execute(
                text("select indexname from pg_indexes where schemaname='public' order by 1")
            )
        ]
        constraints = sorted(
            f"{r[0]}.{r[1]}:{r[2]}"
            for r in c.execute(
                text(
                    "select conrelid::regclass::text, conname, contype from pg_constraint "
                    "where connamespace='public'::regnamespace and conrelid <> 0"
                )
            )
        )
        sequences = {
            r[0]: (None if r[1] is None else int(r[1]))
            for r in c.execute(
                text(
                    "select sequencename, last_value from pg_sequences where schemaname='public' order by 1"
                )
            )
        }
        revision = sorted(
            r[0] for r in c.execute(text(f"select version_num from {LABELS[label]['table']}"))
        )
    return {
        "label": label,
        "revision": revision,
        "row_counts": counts,
        "triggers": triggers,
        "indexes": indexes,
        "constraints": constraints,
        "sequences": sequences,
    }


def cmd_fingerprint(label: str, url_env: str | None, out: str | None) -> int:
    eng = _engine(label, url_env)
    try:
        fp = fingerprint(label, eng)
    except Exception as exc:
        raise Usage(f"cannot fingerprint the {label} database ({type(exc).__name__})") from None
    doc = json.dumps(fp, indent=2, sort_keys=True)
    if out:
        Path(out).write_text(doc + "\n")
    print(
        f"fingerprint {label}: {len(fp['row_counts'])} tables, {sum(fp['row_counts'].values())} rows, {len(fp['triggers'])} triggers"
    )
    return 0


def cmd_compare(a: str, b: str) -> int:
    fa, fb = json.loads(Path(a).read_text()), json.loads(Path(b).read_text())
    diffs = [k for k in sorted(set(fa) | set(fb)) if fa.get(k) != fb.get(k)]
    for k in diffs:
        if isinstance(fa.get(k), dict) and isinstance(fb.get(k), dict):
            for name in sorted(set(fa[k]) | set(fb[k])):
                if fa[k].get(name) != fb[k].get(name):
                    print(f"DIFF {k}.{name}: {fa[k].get(name)!r} != {fb[k].get(name)!r}")
        else:
            print(f"DIFF {k}")
    print(f"compare: {'IDENTICAL' if not diffs else 'DIFFERENT'}")
    return 0 if not diffs else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("head", "fingerprint"):
        p = sub.add_parser(name)
        p.add_argument("--label", required=True, choices=sorted(LABELS))
        p.add_argument("--url-env")
        if name == "fingerprint":
            p.add_argument("--out")
    pc = sub.add_parser("compare")
    pc.add_argument("a")
    pc.add_argument("b")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "head":
            return cmd_head(args.label, args.url_env)
        if args.cmd == "fingerprint":
            return cmd_fingerprint(args.label, args.url_env, args.out)
        return cmd_compare(args.a, args.b)
    except Usage as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
