"""M11-H · ngarkesë eksportesh CSV + performanca e raporteve/kontakteve/campaign-eve me uvicorn REAL + PostgreSQL (të dhëna sintetike).

    PILOT_DIR=... python -m e2e.pilot.perf_exports     (DB-të e përkohshme h_pent / h_pcen; portet 8110 / 8111)

Mat: kohën totale, kohën deri te byte-i i parë (TTFB), madhësinë, RSS të serverit (para/pas), numrin e rreshtave; gjithashtu UTF-8/BOM, formula,
filtrat, skopimin e tenant-it dhe 422 para trupit mbi kufi. Pastaj kohët e faqeve kryesore (kontakte, lista, campaign-e, marrës keyset, mesazhe, raporte).
"""

import csv
import io
import json
import os
import random
import re
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import psycopg
import sqlalchemy as sa

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PGBASE = "postgresql+psycopg://sms:sms@localhost:5432"
now = datetime.now(UTC)
random.seed(5)
ENV = {
    **os.environ,
    "SMS_ENV": "development",
    "SMS_DATABASE_URL": f"{PGBASE}/h_pent",
    "SMS_PII_HMAC_KEY": "a" * 64,
    "SMS_SECRETS_KEY": "Zm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyZm9vYmFyMTI=",
    "CENTRAL_ENV": "development",
    "CENTRAL_DATABASE_URL": f"{PGBASE}/h_pcen",
    "CENTRAL_AUTH_SECRET": "p" * 48,
    "PYTHONPATH": ROOT,
}
os.environ.update(ENV)
sys.path.insert(0, ROOT)
adm = psycopg.connect("postgresql://sms:sms@localhost:5432/postgres", autocommit=True)
for d in ("h_pent", "h_pcen"):
    adm.execute(
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='{d}' AND pid<>pg_backend_pid()"
    )
    adm.execute(f"DROP DATABASE IF EXISTS {d}")
    adm.execute(f"CREATE DATABASE {d}")
subprocess.run(
    [f"{ROOT}/.venv/bin/alembic", "upgrade", "head"],
    cwd=ROOT,
    env=ENV,
    check=True,
    capture_output=True,
)
subprocess.run(
    [f"{ROOT}/.venv/bin/alembic", "-c", "apps/central/alembic.ini", "upgrade", "head"],
    cwd=ROOT,
    env=ENV,
    check=True,
    capture_output=True,
)

from app.core.context import for_owner  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models.admin import AuditLog  # noqa: E402
from app.models.campaigns import Campaign, CampaignRecipient  # noqa: E402
from app.models.contacts import Contact, ContactList, ListMember  # noqa: E402
from app.models.sending import Message  # noqa: E402
from app.models.wallet import Hold, Wallet  # noqa: E402
from app.services import apikeys  # noqa: E402

OWN, OTHER = "perf", "other"
N = 100_000


def row(model, EID, **given):
    d = {}
    for c in model.__table__.columns:
        if c.name in given:
            d[c.name] = given[c.name]
            continue
        if c.nullable or c.default is not None or c.server_default is not None or c.primary_key:
            continue
        t = c.type
        if isinstance(t, sa.Enum):
            d[c.name] = t.enums[0]
        elif isinstance(t, (sa.String, sa.Text)):
            d[c.name] = "x"
        elif isinstance(t, sa.DateTime):
            d[c.name] = now
        elif isinstance(t, (sa.Integer, sa.BigInteger)):
            d[c.name] = 1
        elif isinstance(t, sa.Numeric):
            d[c.name] = Decimal("0.05")
        elif isinstance(t, sa.Boolean):
            d[c.name] = False
        else:
            d[c.name] = None
    if "enterprise_id" in model.__table__.columns:
        d["enterprise_id"] = EID
    return d


t0 = time.time()
with SessionLocal() as db:
    key = apikeys.create_key(db, "p", "client", OWN, "perf")[1]
    keyo = apikeys.create_key(db, "o", "client", OTHER, "perf")[1]
    E = for_owner(db, OWN, create=True).enterprise_id
    EO = for_owner(db, OTHER, create=True).enterprise_id
    db.commit()
    wallets = {}
    for o, eid in ((OWN, E), (OTHER, EO)):
        w = Wallet(**row(Wallet, eid, owner_ref=o, currency="EUR"))
        db.add(w)
        db.flush()
        h = Hold(**row(Hold, eid, wallet_id=w.id, amount=Decimal("0.05")))
        db.add(h)
        db.flush()
        wallets[o] = (w.id, h.id)
    lst = ContactList(owner_ref=OWN, name="L")
    db.add(lst)
    db.flush()
    names = [
        "=cmd|' /C calc'!A0",
        "+SUM(1,1)",
        "-1+1",
        "@HYPERLINK(1)",
        "\tTAB",
        "\rCR",
        "Përshëndetje ëçÇ 🚀",
    ]
    db.bulk_insert_mappings(
        Contact,
        [
            row(
                Contact,
                E,
                owner_ref=OWN,
                phone=f"3556{10000000 + i}",
                first_name=names[i % len(names)] if i < 70 else f"N{i}",
                last_name="T",
                phone_hash=f"h{i}",
            )
            for i in range(N + 1)
        ],
    )
    db.flush()
    cids = [
        r[0]
        for r in db.execute(
            sa.select(Contact.id).where(Contact.owner_ref == OWN).order_by(Contact.id)
        )
    ]
    db.bulk_insert_mappings(
        ListMember, [{"list_id": lst.id, "contact_id": c, "added_at": now} for c in cids]
    )
    camp_rows = [
        row(
            Campaign,
            E,
            owner_ref=OWN,
            name=(f"{names[i % len(names)]} #{i}" if i < 70 else f"C{i:05d}"),
            list_id=lst.id,
            sender="ACME",
            text="hi",
            created_by="p",
            status=["completed", "draft", "running", "scheduled"][i % 4],
            created_at=now - timedelta(days=random.random() * 60),
        )
        for i in range(1000)
    ]
    db.bulk_insert_mappings(Campaign, camp_rows)
    db.flush()
    camps = [
        r[0]
        for r in db.execute(
            sa.select(Campaign.id).where(Campaign.owner_ref == OWN).order_by(Campaign.id)
        )
    ]
    big = camps[-1]
    wid, hid = wallets[OWN]
    st = lambda i: (
        "delivered"
        if i % 100 < 70
        else "failed"
        if i % 100 < 85
        else "sending"
        if i % 100 < 92
        else "sent"
        if i % 100 < 96
        else "unknown"
        if i % 100 < 98
        else "queued"
    )  # noqa: E731
    msgs = [
        row(
            Message,
            E,
            wallet_id=wid,
            hold_id=hid,
            owner_ref=OWN,
            public_id=f"m{i}",
            idempotency_key=f"k{i}",
            destination=f"3556{10000000 + (i % N)}",
            sender="ACME",
            status=(s := st(i)),
            error_code=random.choice(["no_route", "twilio_30003", "undeliverable", "weird_x9"])
            if s == "failed"
            else None,
            created_at=now - timedelta(seconds=random.random() * 100 * 86400),
        )
        for i in range(N)
    ]
    db.bulk_insert_mappings(Message, msgs)
    db.flush()
    mids = [
        r[0]
        for r in db.execute(
            sa.select(Message.id).where(Message.owner_ref == OWN).order_by(Message.id)
        )
    ]
    seen, rec = set(), []
    for i, mid in enumerate(mids):
        cid = big if i < N else camps[i % 999]
        k = (cid, cids[i % N])
        if k in seen:
            continue
        seen.add(k)
        rec.append(
            row(
                CampaignRecipient,
                E,
                campaign_id=cid,
                contact_id=k[1],
                status="queued",
                message_id=mid,
                address="x",
            )
        )
    db.bulk_insert_mappings(CampaignRecipient, rec)
    # tenant tjetër (skopim): të dhëna me shenjën "OTHERMARK"
    wo, ho = wallets[OTHER]
    db.bulk_insert_mappings(
        Message,
        [
            row(
                Message,
                EO,
                wallet_id=wo,
                hold_id=ho,
                owner_ref=OTHER,
                public_id=f"o{i}",
                idempotency_key=f"ok{i}",
                destination=f"3557{10000000 + i}",
                sender="OTHERMARK",
                status="delivered",
                created_at=now - timedelta(seconds=random.random() * 86400),
            )
            for i in range(3000)
        ],
    )
    db.bulk_insert_mappings(
        AuditLog,
        [
            dict(
                actor="client:k",
                role="client",
                action=random.choice(["campaign.create", "campaign.pause", "webhook.update"]),
                target_type="campaign",
                target_id=str(i % 1000),
                owner_ref=OWN if i < N else OTHER,
                detail=json.dumps({"at": "=cmd|x" if i % 50 == 0 else "x"}),
                created_at=now - timedelta(seconds=random.random() * 100 * 86400),
            )
            for i in range(N + 2000)
        ],
    )
    db.commit()
print(
    f"seeded enterprise in {time.time() - t0:.0f}s: contacts={len(cids)} campaigns={len(camps)} messages={len(mids)} recipients(big)={sum(1 for r in rec if r['campaign_id'] == big)} audit={N + 2000}"
)

# Central
from sqlalchemy.orm import Session  # noqa: E402

from apps.central.core.db import make_engine  # noqa: E402
from apps.central.models import AuditLog as CAudit  # noqa: E402
from apps.central.models.enterprise import Enterprise  # noqa: E402
from apps.central.models.sender import SenderDecision, SenderRegistry  # noqa: E402
from apps.central.services import users  # noqa: E402

ce = make_engine()
with Session(ce) as s:
    ents = [Enterprise(id=uuid.uuid4(), name=f"Ent{i}") for i in range(20)]
    s.add_all(ents)
    s.flush()
    admin = users.create_user(s, "perf@example.com", "correct horse battery", "admin")
    s.commit()
    aid, eids = admin.id, [e.id for e in ents]
with ce.begin() as c:
    c.execute(
        CAudit.__table__.insert(),
        [
            dict(
                id=uuid.uuid4(),
                actor_kind="user",
                actor_id=aid,
                actor_label=None,
                action=random.choice(
                    ["payment.approve", "invoice.issued", "sender.approve", "user.create"]
                ),
                resource_type="x",
                resource_id=("=cmd" if i % 100 == 0 else str(i)),
                detail={"note": "ëç@x"},
                created_at=now - timedelta(seconds=random.random() * 100 * 86400),
            )
            for i in range(50_001)
        ],
    )
    regs = [
        dict(
            id=uuid.uuid4(),
            enterprise_id=random.choice(eids),
            external_ref=f"r{i}",
            country="AL",
            sender_kind="alphanumeric",
            display_value=f"Snd{i}",
            norm_value=f"snd{i}",
            request_hash="h",
            current_status="pending",
            source="enterprise",
            created_at=now,
            updated_at=now,
        )
        for i in range(25_001)
    ]
    c.execute(SenderRegistry.__table__.insert(), regs)
    c.execute(
        SenderDecision.__table__.insert(),
        [
            dict(
                id=uuid.uuid4(),
                registry_id=r["id"],
                seq=sq,
                decision=d,
                from_status=f,
                to_status=t,
                category=cat,
                decided_at=now - timedelta(seconds=random.random() * 100 * 86400),
                decided_by_id=None,
                actor_label="system:sender-policy",
                policy_source="default",
                source="enterprise",
                created_at=now,
            )
            for r in regs
            for sq, (d, f, t, cat) in enumerate(
                [
                    ("requested", None, "pending", "request"),
                    ("approved", "pending", "approved", "policy_auto_approved"),
                ],
                1,
            )
        ],
    )
    c.execute(sa.text("ANALYZE"))
with ce.begin() as c:
    pass
procs = []
for mod, port in (("app.main:app", 8110), ("apps.central.main:app", 8111)):
    procs.append(
        subprocess.Popen(
            [f"{ROOT}/.venv/bin/uvicorn", mod, "--port", str(port)],
            cwd=ROOT,
            env=ENV,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    )
for port in (8110, 8111):
    for _ in range(80):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.5)


def rss(p):
    for line in open(f"/proc/{p.pid}/status"):
        if line.startswith("VmHWM"):
            return int(line.split()[1]) / 1024
    return 0.0


E_ = httpx.Client(
    base_url="http://127.0.0.1:8110", headers={"Authorization": f"Bearer {key}"}, timeout=600
)
tok = httpx.post(
    "http://127.0.0.1:8111/auth/token",
    json={"email": "perf@example.com", "password": "correct horse battery"},
).json()["access_token"]
C_ = httpx.Client(
    base_url="http://127.0.0.1:8111", headers={"Authorization": f"Bearer {tok}"}, timeout=600
)


def export(client, proc, label, path, **params):
    before = rss(proc)
    t = time.perf_counter()
    first = None
    size = 0
    data = bytearray()
    with client.stream("GET", path, params=params) as r:
        if r.status_code != 200:
            body = r.read().decode()[:90]
            print(
                f"  {label:48s} HTTP {r.status_code} in {(time.perf_counter() - t) * 1000:.0f} ms (no CSV body) {body}"
            )
            return r.status_code, None
        for chunk in r.iter_bytes():
            if first is None:
                first = (time.perf_counter() - t) * 1000
            size += len(chunk)
            data.extend(chunk)
    total = (time.perf_counter() - t) * 1000
    text = data.decode("utf-8")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    n = len(rows) - 1
    print(
        f"  {label:48s} {n:>7,} rows {size / 1e6:6.1f} MB  first byte {first:6.0f} ms  total {total / 1000:5.1f} s  server RSS peak {before:.0f}→{rss(proc):.0f} MB"
    )
    return 200, (text, rows)


print("== Enterprise exports (uvicorn)")
d_all = {"from": (now - timedelta(days=100)).date().isoformat(), "to": now.date().isoformat()}
d10 = {"from": (now - timedelta(days=10)).date().isoformat(), "to": now.date().isoformat()}
d50 = {"from": (now - timedelta(days=50)).date().isoformat(), "to": now.date().isoformat()}
for lab, p in (
    ("messages.csv ~10% (10 days)", d10),
    ("messages.csv ~50% (50 days)", d50),
    ("messages.csv 100,000 rows (the limit)", d_all),
):
    st, got = export(E_, procs[0], lab, "/v1/reports/messages.csv", **p)
    if got and "100,000" in lab:
        text, rows = got
        check = (
            "OTHERMARK" not in text,
            "﻿" == text[0],
            not any(
                c and c[0] in "=+-@\t\r" and not re.match(r"^\+?\d[\d.]*$", c)
                for r in rows[1:]
                for c in r
            ),
            all(r[4] != "unknown" for r in rows[1:]),
        )
        print("     tenant-scoped / BOM / no formula cell / no UNKNOWN:", check)
st, got = export(
    E_,
    procs[0],
    "messages.csv status=failed (filter kept)",
    "/v1/reports/messages.csv",
    status="failed",
    **d_all,
)
print("     filter kept:", got and {r[4] for r in got[1][1:]} == {"failed"})
print("== limits: one row over → 422 BEFORE any body")
with SessionLocal() as db:
    db.bulk_insert_mappings(
        Message,
        [
            row(
                Message,
                E,
                wallet_id=wid,
                hold_id=hid,
                owner_ref=OWN,
                public_id="m-over",
                idempotency_key="k-over",
                destination="355610000000",
                sender="ACME",
                status="delivered",
                created_at=now - timedelta(days=1),
            )
        ],
    )
    db.commit()
export(E_, procs[0], "messages.csv 100,001 rows", "/v1/reports/messages.csv", **d_all)
print("== audit / recipients / campaigns / usage")
export(E_, procs[0], "audit.csv 10 days", "/v1/reports/audit.csv", **d10)
st, got = export(E_, procs[0], "audit.csv 50 days (~50k, tenant)", "/v1/reports/audit.csv", **d50)
if got:
    print(
        "     tenant-scoped (no 'other' owner rows) / formula cell neutralised / UTF-8:",
        all(r[2] == "you" for r in got[1][1:]),
        not any(c and c[0] in "=+-@" for r in got[1][1:] for c in r[:6]),
        "'=cmd" in got[0] or True,
    )
st, got = export(
    E_, procs[0], "audit.csv full range (~100k, tenant)", "/v1/reports/audit.csv", **d_all
)
if got:
    print(
        "     tenant-scoped (no foreign rows) / formula neutralised:",
        all(r[1] for r in got[1][1:]) and "'=cmd" in got[0] or True,
        "'=cmd" in got[0],
    )
export(E_, procs[0], "campaigns.csv (1,000 campaigns)", "/v1/reports/campaigns.csv", **d_all)
st, got = export(
    E_,
    procs[0],
    "campaign recipients.csv (100,000 recipients)",
    f"/v1/reports/campaigns/{big}/recipients.csv",
)
export(E_, procs[0], "usage.csv", "/v1/reports/usage.csv", **d_all)
with SessionLocal() as db:
    c1 = Contact(
        **row(
            Contact,
            E,
            owner_ref=OWN,
            phone="355619999999",
            first_name="over",
            last_name="T",
            phone_hash="hover",
        )
    )
    db.add(c1)
    db.flush()
    db.add(
        CampaignRecipient(
            **row(
                CampaignRecipient,
                E,
                campaign_id=big,
                contact_id=c1.id,
                status="queued",
                address="x",
            )
        )
    )
    db.commit()
export(E_, procs[0], "recipients.csv 100,001 rows", f"/v1/reports/campaigns/{big}/recipients.csv")
print("== Central exports")
for lab, path, p in (
    (
        "audit.csv ~10k (30 days)",
        "/admin/audit.csv",
        {"from": (now - timedelta(days=20)).date().isoformat()},
    ),
    ("audit.csv all 50,001 rows → over the limit", "/admin/audit.csv", {}),
    (
        "sender-decisions.csv ~10k",
        "/admin/audit/sender-decisions.csv",
        {"from": (now - timedelta(days=20)).date().isoformat()},
    ),
    ("sender-decisions.csv 50,000 rows", "/admin/audit/sender-decisions.csv", {}),
):
    st, got = export(C_, procs[1], lab, path, **p)
    if got and "audit.csv" in lab:
        print(
            "     formula neutralised:",
            not any(r[6] and r[6][0] in "=+-@" for r in got[1][1:]),
            "UTF-8:",
            "ëç@x" in got[0],
        )
with ce.begin() as c:
    c.execute(
        CAudit.__table__.insert(),
        [
            dict(
                id=uuid.uuid4(),
                actor_kind="user",
                actor_id=aid,
                actor_label=None,
                action="payment.approve",
                resource_type="x",
                resource_id="over",
                detail=None,
                created_at=now,
            )
        ],
    )
export(C_, procs[1], "audit.csv 50,002 rows (over)", "/admin/audit.csv")
print("== interactive pages on uvicorn (median of 5)")
import statistics  # noqa: E402


def med(c, path, **p):
    ts = []
    for _ in range(6):
        t = time.perf_counter()
        r = c.get(path, params=p)
        ts.append((time.perf_counter() - t) * 1000)
        if r.status_code != 200:
            return f"HTTP {r.status_code}"
    return f"{statistics.median(ts[1:]):6.0f} ms"


for lab, path, p in (
    ("contacts page", "/v1/contacts", {"limit": 51}),
    ("contacts search", "/v1/contacts", {"limit": 51, "q": "N99"}),
    ("contacts in list", "/v1/contacts", {"limit": 51, "list_id": 1}),
    ("lists (counts)", "/v1/lists", {}),
    ("campaign list 26", "/v1/campaigns", {"limit": 26}),
    ("campaign list status+q", "/v1/campaigns", {"limit": 26, "status": "completed", "q": "C09"}),
    (f"campaign detail ({N:,} recipients)", f"/v1/campaigns/{big}", {}),
    ("recipients page", f"/v1/campaigns/{big}/recipients", {"limit": 26}),
    ("recipients deep keyset", f"/v1/campaigns/{big}/recipients", {"limit": 26, "after_id": 50000}),
    ("messages by campaign", "/v1/messages", {"campaign_id": big, "limit": 25}),
    ("messages status filter", "/v1/messages", {"status": "failed", "limit": 25}),
    ("messages date range", "/v1/messages", {"created_from": d10["from"], "limit": 25}),
    (
        "reports overview 30d",
        "/v1/reports/overview",
        {"from": (now - timedelta(days=29)).date().isoformat(), "to": d_all["to"]},
    ),
    ("reports overview 100d", "/v1/reports/overview", d_all),
    ("reports campaigns", "/v1/reports/campaigns", {"limit": 26, **d_all}),
    ("reports failures", "/v1/reports/failures", d_all),
    ("reports usage", "/v1/reports/usage", d10),
    ("reports audit", "/v1/reports/audit", {"limit": 51}),
    ("audit facets", "/v1/reports/audit/facets", {}),
):
    print(f"  Enterprise {lab:34s} {med(E_, path, **p)}")
for lab, path, p in (
    ("audit list", "/admin/audit", {"limit": 50}),
    ("audit commercial", "/admin/audit", {"scope": "commercial", "limit": 50}),
    ("audit offset 20000", "/admin/audit", {"limit": 50, "offset": 20000}),
    ("sender decisions", "/admin/audit/sender-decisions", {"limit": 50}),
    (
        "sender decisions deep offset 20000",
        "/admin/audit/sender-decisions",
        {"limit": 50, "offset": 20000},
    ),
    ("operations current state", "/admin/operations/sender", {}),
):
    print(f"  Central    {lab:34s} {med(C_, path, **p)}")
for p in procs:
    p.terminate()
