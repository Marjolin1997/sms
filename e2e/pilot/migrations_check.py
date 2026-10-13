"""M11-H · migrime: koka, drift, rruga e përmirësimit me të dhëna (downgrade → upgrade) dhe instalimi i pastër, mbi PostgreSQL.

. $PILOT_DIR/pilot.env && python -m e2e.pilot.migrations_check
"""

import os
import subprocess
import time

import psycopg
import sqlalchemy as sa

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PG = "postgresql://sms:sms@localhost:5432"
SA = "postgresql+psycopg://sms:sms@localhost:5432"
passed = 0


def check(c, m):
    global passed
    if not c:
        raise AssertionError(m)
    passed += 1
    print("  ok ", m)


def alembic(kind, url, *args):
    env = {**os.environ, "SMS_DATABASE_URL": url, "CENTRAL_DATABASE_URL": url}
    ini = ["-c", "apps/central/alembic.ini"] if kind == "central" else []
    t = time.time()
    r = subprocess.run(
        [f"{ROOT}/.venv/bin/alembic", *ini, *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    return r.returncode, (r.stdout + r.stderr).strip(), time.time() - t


def drift(kind, url):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    if kind == "central":
        import apps.central.models  # noqa: F401
        from apps.central.core.db import Base
    else:
        import app.models  # noqa: F401
        from app.core.db import Base
    with sa.create_engine(url).connect() as c:
        ctx = MigrationContext.configure(c, opts={"compare_type": True})
        return [
            r
            for r in (
                repr(i)
                for d in compare_metadata(ctx, Base.metadata)
                for i in (d if isinstance(d, list) else [d])
            )
            if "alembic_version" not in r
        ]


def counts(url, skip=()):
    with sa.create_engine(url).connect() as c:
        tabs = [
            r[0]
            for r in c.execute(
                sa.text(
                    "select tablename from pg_tables where schemaname='public' and tablename not like '%alembic_version' order by 1"
                )
            )
        ]
        return {
            t: c.execute(sa.text(f'select count(*) from "{t}"')).scalar()
            for t in tabs
            if t not in skip
        }


def clone(src, dst):
    a = psycopg.connect(f"{PG}/postgres", autocommit=True)
    a.execute(
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{dst}' AND pid<>pg_backend_pid()"
    )
    a.execute(f"DROP DATABASE IF EXISTS {dst}")
    a.execute(f"CREATE DATABASE {dst}")
    dump = subprocess.run(["pg_dump", f"{PG}/{src}"], capture_output=True, check=True).stdout
    subprocess.run(["psql", "-q", f"{PG}/{dst}"], input=dump, capture_output=True, check=True)


for kind, src, down_to, label in (
    ("enterprise", "h_ent", "0027", "Enterprise"),
    ("central", "h_cen", "0028", "Central"),
):
    print(f"{label}: head, drift, upgrade path")
    dst = f"h_up_{kind[:3]}"
    url = f"{SA}/{dst}"
    clone(src, dst)
    rc, out, _ = alembic(kind, url, "heads")
    head = out.split()[0]
    rc, out, _ = alembic(kind, url, "current")
    check(out.split()[0] == head, f"{label}: DB is at the single head {head}")
    d = drift(kind, url)
    check(d == [], f"{label}: compare_metadata on PostgreSQL = 0 differences {d[:2]}")
    before = counts(url)
    rows_before = sum(before.values())
    check(
        rows_before > 20,
        f"{label}: the cloned pilot DB holds representative data ({rows_before} rows in {len([k for k, v in before.items() if v])} tables)",
    )
    rc, out, t = alembic(kind, url, "downgrade", down_to)
    check(
        rc == 0,
        f"{label}: downgrade {head} → {down_to} with data present ({t:.1f}s) {out[-120:] if rc else ''}",
    )
    mid = counts(url)
    dropped = sorted(set(before) - set(mid))
    rc, out, t = alembic(kind, url, "upgrade", "head")
    check(
        rc == 0,
        f"{label}: upgrade {down_to} → head with data present ({t:.1f}s) {out[-160:] if rc else ''}",
    )
    after = counts(url)
    lost = {k: (before[k], after.get(k)) for k in before if k in mid and before[k] != after.get(k)}
    check(not lost, f"{label}: no row lost in tables that exist at both ends: {lost}")
    check(
        drift(kind, url) == [],
        f"{label}: schema after down/up matches the models (compare_metadata = 0)",
    )
    print(f"     tables dropped by the downgrade (re-created empty by the upgrade): {dropped}")
    cleanup = psycopg.connect(f"{PG}/postgres", autocommit=True)
    cleanup.execute(
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{dst}' AND pid<>pg_backend_pid()"
    )
    cleanup.execute(f"DROP DATABASE IF EXISTS {dst}")

print("clean install (empty PostgreSQL → head)")
for kind, head in (("enterprise", "0034"), ("central", "0031")):
    db = f"h_clean_{kind[:3]}"
    a = psycopg.connect(f"{PG}/postgres", autocommit=True)
    a.execute(f"DROP DATABASE IF EXISTS {db}")
    a.execute(f"CREATE DATABASE {db}")
    rc, out, t = alembic(kind, f"{SA}/{db}", "upgrade", "head")
    check(rc == 0, f"{kind}: base → head on an empty database in {t:.1f}s")
    rc, out, _ = alembic(kind, f"{SA}/{db}", "current")
    check(out.split()[0] == head, f"{kind}: at {head}")
    check(drift(kind, f"{SA}/{db}") == [], f"{kind}: compare_metadata = 0")
    rc, out, _ = alembic(kind, f"{SA}/{db}", "downgrade", "base")
    check(rc == 0, f"{kind}: full downgrade head → base works ({out[-100:] if rc else 'no error'})")
    rc, out, t = alembic(kind, f"{SA}/{db}", "upgrade", "head")
    check(rc == 0, f"{kind}: base → head again after a full downgrade ({t:.1f}s)")
    a.execute(
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{db}' AND pid<>pg_backend_pid()"
    )
    a.execute(f"DROP DATABASE IF EXISTS {db}")
print(f"{passed} checks")
