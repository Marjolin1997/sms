"""M11-H · audit në shkallë (100k / 500k / 1M) mbi PostgreSQL me uvicorn REAL: lista, filtra, offset i thellë, EXPLAIN, dhe efekti i një indeksi kandidat.

    PILOT_DIR=... python -m e2e.pilot.perf_audit central|enterprise   (DB-të e përkohshme h_pcen / h_pent; portet 8111 / 8110)
Nuk prek stack-un e pilotit. Të dhëna sintetike. Raporton timing (median i 5 matjeve), pyetje (jo këtu), plan.
"""

import json
import os
import random
import statistics
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import psycopg
import sqlalchemy as sa

KIND = sys.argv[1]
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PY = os.path.join(ROOT, ".venv/bin/python")
PGBASE = "postgresql+psycopg://sms:sms@localhost:5432"
DB = "h_pcen" if KIND == "central" else "h_pent"
PORT = 8111 if KIND == "central" else 8110
now = datetime.now(UTC)
random.seed(11)
ENV = {**os.environ}
if KIND == "central":
    ENV.update(
        CENTRAL_ENV="development",
        CENTRAL_DATABASE_URL=f"{PGBASE}/{DB}",
        CENTRAL_AUTH_SECRET="p" * 48,
    )
else:
    ENV.update(
        SMS_ENV="development",
        SMS_DATABASE_URL=f"{PGBASE}/{DB}",
        SMS_PII_HMAC_KEY="a" * 64,
        SMS_SECRETS_KEY="Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyMTI=",
    )
ENV["PYTHONPATH"] = ROOT


def sh(*a, **k):
    return subprocess.run(a, cwd=ROOT, env=ENV, check=True, capture_output=True, text=True, **k)


adm = psycopg.connect("postgresql://sms:sms@localhost:5432/postgres", autocommit=True)
adm.execute(
    f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{DB}' AND pid<>pg_backend_pid()"
)
adm.execute(f"DROP DATABASE IF EXISTS {DB}")
adm.execute(f"CREATE DATABASE {DB}")
if KIND == "central":
    sh(f"{ROOT}/.venv/bin/alembic", "-c", "apps/central/alembic.ini", "upgrade", "head")
else:
    sh(f"{ROOT}/.venv/bin/alembic", "upgrade", "head")
eng = sa.create_engine(f"{PGBASE}/{DB}")
ACTS = [
    "payment.approve",
    "invoice.issued",
    "sender.request",
    "sender.approve",
    "plan_version.activate",
    "price_rule.set",
    "user.create",
    "product.update",
    "billing.run",
    "campaign.create",
    "webhook.update",
]


def seed_central_users():
    sys.path.insert(0, ROOT)
    from sqlalchemy.orm import Session

    from apps.central.services import users

    with Session(eng) as s:
        u = users.create_user(s, "perf@example.com", "correct horse battery", "admin")
        s.commit()
        return u.id


def grow(target, have, uid=None):
    from app.models.admin import AuditLog as EAudit
    from apps.central.models import AuditLog as CAudit

    while have < target:
        n = min(50_000, target - have)
        if KIND == "central":
            rows = [
                dict(
                    id=uuid.uuid4(),
                    actor_kind="user",
                    actor_id=uid,
                    actor_label=None,
                    action=random.choice(ACTS),
                    resource_type="x",
                    resource_id=str(i),
                    detail={"before": {"a": 1}, "after": {"a": 2}},
                    created_at=now - timedelta(seconds=random.random() * 365 * 86400),
                )
                for i in range(n)
            ]
            with eng.begin() as c:
                c.execute(CAudit.__table__.insert(), rows)
        else:
            rows = [
                dict(
                    actor="client:k",
                    role="client",
                    action=random.choice(ACTS),
                    target_type="campaign",
                    target_id=str(i % 1000),
                    owner_ref="perf" if random.random() < 0.5 else f"o{random.randint(1, 200)}",
                    detail=json.dumps({"at": "x"}),
                    created_at=now - timedelta(seconds=random.random() * 365 * 86400),
                )
                for i in range(n)
            ]
            with eng.begin() as c:
                c.execute(EAudit.__table__.insert(), rows)
        have += n
    with eng.begin() as c:
        c.execute(sa.text("ANALYZE"))
    return have


def start():
    p = subprocess.Popen(
        [
            f"{ROOT}/.venv/bin/uvicorn",
            "apps.central.main:app" if KIND == "central" else "app.main:app",
            "--port",
            str(PORT),
        ],
        cwd=ROOT,
        env=ENV,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(60):
        try:
            if httpx.get(f"http://127.0.0.1:{PORT}/healthz", timeout=2).status_code == 200:
                return p
        except httpx.HTTPError:
            time.sleep(0.5)
    raise SystemExit("uvicorn did not start")


def med(c, path, params, n=5):
    ts = []
    for _ in range(n + 1):
        t = time.perf_counter()
        r = c.get(path, params=params)
        ts.append((time.perf_counter() - t) * 1000)
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
    return statistics.median(ts[1:])


def plan(sql, **kw):
    with eng.connect() as c:
        rows = [r[0] for r in c.execute(sa.text("EXPLAIN (ANALYZE, FORMAT TEXT) " + sql), kw).all()]
    scans = [l.strip()[:110] for l in rows if "Scan" in l][:2]
    return rows[-1].strip(), scans


def measure(c, size, label):
    d0, d1 = (now - timedelta(days=29)).date().isoformat(), now.date().isoformat()
    out = {}
    if KIND == "central":
        paths = [
            ("list", "/admin/audit", {"limit": 50}),
            ("scope commercial", "/admin/audit", {"scope": "commercial", "limit": 50}),
            ("action filter", "/admin/audit", {"action": "payment.approve", "limit": 50}),
            ("date 30d", "/admin/audit", {"limit": 50, "from": d0, "to": d1}),
            ("facets", "/admin/audit/facets", {}),
        ]
        paths += [
            (f"offset {o}", "/admin/audit", {"limit": 50, "offset": o})
            for o in (1000, 10000, 20000, 100000)
        ]
        sql = "SELECT id FROM audit_log ORDER BY created_at DESC, id DESC LIMIT 51"
    else:
        paths = [
            ("list", "/v1/reports/audit", {"limit": 51}),
            ("action filter", "/v1/reports/audit", {"limit": 51, "action": "campaign.create"}),
            ("date 30d", "/v1/reports/audit", {"limit": 51, "from": d0, "to": d1}),
            ("keyset deep", "/v1/reports/audit", {"limit": 51, "before_id": max(1, size // 2)}),
            ("facets", "/v1/reports/audit/facets", {}),
        ]
        sql = "SELECT id FROM sms_audit_log WHERE owner_ref='perf' ORDER BY id DESC LIMIT 51"
    for name, path, params in paths:
        out[name] = med(c, path, params)
    ex, scans = plan(sql)
    print(
        f"[{label}] rows={size:>9,}  "
        + "  ".join(
            f"{k}={v:.0f}ms" if isinstance(v, float) else f"{k}={v}" for k, v in out.items()
        )
    )
    print(f"           plan({sql[:60]}…): {ex} | {scans}")


have = 0
uid = None
if KIND == "central":
    uid = seed_central_users()
proc = start()
try:
    if KIND == "central":
        tok = httpx.post(
            f"http://127.0.0.1:{PORT}/auth/token",
            json={"email": "perf@example.com", "password": "correct horse battery"},
        ).json()["access_token"]
        c = httpx.Client(headers={"Authorization": f"Bearer {tok}"}, timeout=120)
    else:
        sys.path.insert(0, ROOT)
        os.environ.update({k: v for k, v in ENV.items() if k.startswith("SMS_")})
        from app.core.db import SessionLocal
        from app.services import apikeys

        with SessionLocal() as db:
            key = apikeys.create_key(db, "p", "client", "perf", "perf")[1]
            db.commit()
        c = httpx.Client(
            headers={"Authorization": f"Bearer {key}"},
            timeout=120,
            base_url=f"http://127.0.0.1:{PORT}",
        )
    c.base_url = f"http://127.0.0.1:{PORT}"
    for size in (100_000, 500_000, 1_000_000):
        have = grow(size, have, uid)
        measure(c, size, "no extra index")
    # indeksi kandidat vetëm në DB-në e provës
    idx = (
        "CREATE INDEX ix_cand ON audit_log (created_at DESC, id DESC)"
        if KIND == "central"
        else "CREATE INDEX ix_cand ON sms_audit_log (owner_ref, created_at DESC)"
    )
    t = time.time()
    with eng.begin() as conn:
        conn.execute(sa.text(idx))
        conn.execute(sa.text("ANALYZE"))
    print(f"candidate index built in {time.time() - t:.1f}s: {idx}")
    measure(c, have, "with candidate index")
    with eng.connect() as conn:
        print(
            "table+index size:",
            conn.execute(
                sa.text("select pg_size_pretty(pg_total_relation_size(:t))"),
                {"t": "audit_log" if KIND == "central" else "sms_audit_log"},
            ).scalar(),
        )
finally:
    proc.terminate()
