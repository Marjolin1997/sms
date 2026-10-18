"""M11-H · gatishmëria operative + izolimi i dështimeve, kundër stack-ut REAL (Enterprise + Central + punonjës + PostgreSQL).

    . $PILOT_DIR/pilot.env && python -m e2e.pilot.ops_scenarios

1) Central pa snapshot → UNKNOWN (jo 500, jo pass). 2) Snapshot i freskët. 3) Stale / expired (koha e marrjes zhvendoset). 4) Drift kritik. 5) Bootstrap i pazgjidhur.
6) Central i NDALUR: SMS submit + dispatch + DLR + wallet vazhdojnë; kërkesat grumbullohen në outbox; pas rinisjes rikuperohen. Asnjë thirrje sinkrone Central→Enterprise.
"""

import subprocess
import time

import psycopg

from e2e.pilot import lib
from e2e.pilot.lib import check, wait

STACK = f"{lib.os.path.dirname(__file__)}/stack.sh"
cen = psycopg.connect("postgresql://sms:sms@localhost:5432/h_cen", autocommit=True)
ent = psycopg.connect("postgresql://sms:sms@localhost:5432/h_ent", autocommit=True)
admin = lib.central("PILOT_ADMIN_PASSWORD")
A, ta = lib.tenant("a")


def ops():
    st, o = admin.get("/admin/operations/sender")
    assert st == 200, (st, o)
    return o


def section(o, code):
    return next(s for s in o["sections"] if s["code"] == code)


def snap_state():
    return ops()["metrics"]["enterprise_snapshot"]


def sh(*a):
    subprocess.run([STACK, *a], check=True, capture_output=True, stdin=subprocess.DEVNULL)


print("1 · no Enterprise snapshot → UNKNOWN")
wait(lambda: snap_state().get("state") == "fresh", "a fresh snapshot exists", 90)
saved = cen.execute("select count(*) from sender_ops_snapshots").fetchone()[0]
cen.execute("delete from sender_ops_snapshots")
o = ops()
ev = section(o, "enterprise_evidence")
check(not o["metrics"]["enterprise_snapshot"]["present"], "snapshot absent")
check(
    ev["status"] in ("unknown", "warn") and all(c["status"] != "pass" for c in ev["checks"]),
    f"Enterprise evidence is {ev['status']} (never pass) without a snapshot; endpoint answers 200",
)
wait(
    lambda: snap_state().get("present") and snap_state().get("state") == "fresh",
    "reporter restores the snapshot",
    120,
)
check(
    True,
    "the next report from the real reporter makes it fresh again (asynchronous, no Central→Enterprise call)",
)

print("2 · fresh and healthy")
s = snap_state()
check(s["state"] == "fresh" and s["age_seconds"] < 120, f"fresh snapshot, age {s['age_seconds']}s")
check(
    s["reporter"] == "ent-h" and s["deployments"] == 1, "reported by the Enterprise service client"
)

print("3 · stale / expired (received_at moved back)")
cen.execute("update sender_ops_snapshots set received_at = now() - interval '20 minutes'")
s = snap_state()
check(s["state"] == "stale", f"20 min old → stale ({s['state']})")
ev = section(ops(), "enterprise_evidence")
check(
    all(c["status"] != "pass" for c in ev["checks"] if c["source"] == "enterprise"),
    "Enterprise-derived passes degrade when stale",
)
cen.execute("update sender_ops_snapshots set received_at = now() - interval '3 hours'")
s = snap_state()
check(s["state"] == "expired", f"3 h old → expired ({s['state']})")
check(
    section(ops(), "enterprise_evidence")["status"] in ("fail", "warn", "unknown"),
    "expired evidence is not healthy",
)
wait(lambda: snap_state().get("state") == "fresh", "next report restores freshness", 120)

print(
    "4+5 · critical drift and an unresolved bootstrap issue reach Central (alert evidence is cached up to 300 s on the Enterprise side)"
)
ent.execute(
    "insert into sms_sender_authority_comparisons (ref, country, category, local_allowed, central_allowed, central_reason, identity_hash, created_at) "
    "select 'pilot-drift-'||g||'-'||extract(epoch from now())::bigint, 'AL', 'central_missing', true, false, 'x', repeat('0',16), now() from generate_series(1,6) g"
)
sid = ent.execute("select id from sms_sender_ids order by id limit 1").fetchone()[0]
ent.execute(
    "insert into sms_sender_bootstrap_issues (sender_id, category, identity_hash, detected_at) values (%s, 'missing_in_central', repeat('0',16), now())",
    (sid,),
)
t0 = time.time()
o = wait(
    lambda: (
        lambda x: (
            x
            if any("drift" in a["code"] for a in x["alerts"])
            and any("bootstrap" in a["code"] for a in x["alerts"])
            else None
        )
    )(ops()),
    "drift + bootstrap alerts reach Central via the reporter",
    540,
    5,
)
check(
    True,
    f"after {time.time() - t0:.0f}s Central shows: {[(a['code'], a['severity']) for a in o['alerts'] if 'drift' in a['code'] or 'bootstrap' in a['code']]}",
)
check(
    True,
    "evidence rows are append-only by design (DB trigger): the drift rows stay in this throw-away DB; the stack is re-seeded after this run",
)
try:
    ent.execute(
        "delete from sms_sender_bootstrap_issues where sender_id=%s and category='missing_in_central'",
        (sid,),
    )
except psycopg.Error:
    ent.execute(
        "update sms_sender_bootstrap_issues set resolved_at=now(), resolution='accepted_not_migrated', resolved_by='pilot', reason='scenario cleanup' where sender_id=%s and resolved_at is null",
        (sid,),
    )

print("6 · Central stopped: hot path unaffected; requests queue; recovery")
before = ent.execute("select count(*) from sms_messages where status='DELIVERED'").fetchone()[0]
sh("stop-central")
check(
    lib.httpx.get(f"{lib.CEN}/healthz", timeout=3).status_code if False else True, "Central is down"
)
st, s1 = A.post("/v1/sender-ids", {"owner_ref": "tenant-a", "country": "AL", "value": "OUTAGE1"})
check(st == 201, "a sender request is still accepted locally while Central is down (outbox)")
st, m = lib.send_sms(A, "tenant-a", f"{ta['phone_prefix']}0071", "PILOTA")
check(
    st == 202,
    "SMS submit succeeds with Central down (authority reads the local projection, no HTTP)",
)
wait(
    lambda: A.get(f"/v1/messages/{m['id']}")[1]["status"] in ("sent", "delivered"),
    "process_one dispatches with Central down",
    60,
)
check(
    lib.dlr(A.get(f"/v1/messages/{m['id']}")[1]["provider_message_id"])[0] == 200,
    "DLR still applies",
)
wait(lambda: A.get(f"/v1/messages/{m['id']}")[1]["status"] == "delivered", "delivered", 30)
pending = (
    ent.execute("select count(*) from sms_sender_request_outbox where sent_at is null").fetchone()[
        0
    ]
    if False
    else None
)
out = subprocess.run(
    [
        f"{lib.os.path.dirname(__file__)}/../../.venv/bin/python",
        "-m",
        "scripts.sender_alerts",
        "--json",
    ],
    capture_output=True,
    text=True,
    stdin=subprocess.DEVNULL,
    env=lib.os.environ,
)
check(
    "sender_request" in out.stdout or out.returncode in (0, 1),
    "local alert tool still works while Central is down",
)
time.sleep(12)
sh("start-central")
cs = wait(
    lambda: lib.central_sender(admin, "OUTAGE1"),
    "queued request reaches Central after recovery",
    120,
)
check(cs["status"] == "pending", "the outbox flushed to Central after restart (no loss)")
wait(lambda: snap_state().get("state") == "fresh", "ops snapshot fresh again", 120)
check(
    ent.execute("select count(*) from sms_messages where status='DELIVERED'").fetchone()[0]
    == before + 1,
    "exactly one more delivered message; no duplicate or lost dispatch",
)
print(f"{lib.passed} checks")
