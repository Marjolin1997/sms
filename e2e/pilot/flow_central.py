"""M11-H · faza 2 (authority=central): rrjedha e plotë e sender-it + campaign + dështime të kontrolluara, kundër uvicorn REAL + PostgreSQL.

    . $PILOT_DIR/pilot.env && python -m e2e.pilot.flow_central

Tenant B kërkon sender → Central (admin) → projeksion → efektiv approved → mesazh + campaign (FakeProvider) → revokim → rikontroll dispatch.
Pastaj skenarët e dështimit (sender i pavlefshëm / pending / not_synchronized / revoked / bilanc / marrës / provider / 409 / 422).
"""

import subprocess
import time

from e2e.pilot import lib
from e2e.pilot.lib import check, wait

B, tb = lib.tenant("b")
A, ta = lib.tenant("a")
admin = lib.central("PILOT_ADMIN_PASSWORD")
approver, finance = lib.staff("approver"), lib.staff("finance")
STACK = f"{lib.os.path.dirname(__file__)}/stack.sh"


def worker(action, name):
    subprocess.run(
        [STACK, f"{action}-worker", name], check=True, capture_output=True, stdin=subprocess.DEVNULL
    )


def eff(api, sid):
    return api.get(f"/v1/sender-ids/{sid}")[1]


print("1 · tenant B: sender request → Central → projection (authority=central)")
st, s = B.post("/v1/sender-ids", {"owner_ref": "tenant-b", "country": "AL", "value": "PILOTB"})
check(st == 201, "Enterprise accepts the request")
check(
    s["authority_mode"] == "central" and s["effective_status"] in ("not_synchronized", "pending"),
    f"under Central authority the new request starts as {s['effective_status']} (no projection yet)",
)
cs = wait(lambda: lib.central_sender(admin, "PILOTB"), "Central received the request")
check(
    cs["status"] == "pending" and cs["enterprise_id"] == tb["enterprise_id"],
    "Central has it for tenant B's enterprise",
)
check(
    lib.central_sender(admin, "PILOTB")["enterprise_id"] != ta["enterprise_id"],
    "…and not for tenant A",
)
wait(
    lambda: eff(B, s["id"])["effective_status"] == "pending",
    "Central's pending decision is projected back",
)
check(
    eff(B, s["id"])["sync_status"] == "synchronized",
    "projection is synchronized (pending at Central)",
)
st, body = lib.send_sms(B, "tenant-b", f"{tb['phone_prefix']}0011", "PILOTB")
check(st == 403 and body["detail"]["code"] == "sender_not_allowed", "a pending sender cannot send")
st, body = approver.post(f"/v1/sender-ids/{s['id']}/approve", {})
check(
    st == 409 and body["detail"]["code"] == "sender_authority_frozen",
    "local review is frozen under Central authority",
)

print("2 · Central admin approves → Enterprise projection → effective approved")
st, row = admin.post(f"/admin/senders/{cs['id']}/approve", {})
check(st == 200 and row["status"] == "approved", "admin approves in Central")
wait(
    lambda: eff(B, s["id"])["effective_status"] == "approved",
    "projection synchronised to Enterprise",
)
e = eff(B, s["id"])
check(
    e["central_status"] == "approved" and e["sync_status"] == "synchronized",
    "customer view: approved + synchronized",
)
st, m = lib.send_sms(B, "tenant-b", f"{tb['phone_prefix']}0011", "PILOTB")
check(st == 202, "an approved sender can send")
wait(
    lambda: B.get(f"/v1/messages/{m['id']}")[1]["status"] in ("sent", "delivered"),
    "dispatched by the real worker via FakeProvider",
)
pm = B.get(f"/v1/messages/{m['id']}")[1]["provider_message_id"]
check(lib.dlr(pm)[0] == 200, "DLR webhook")
wait(lambda: B.get(f"/v1/messages/{m['id']}")[1]["status"] == "delivered", "delivered")

print("3 · campaign (tenant B): draft → estimate → schedule → run → results")
st, c = B.post(
    "/v1/campaigns",
    {
        "owner_ref": "tenant-b",
        "name": "Pilot B campaign",
        "list_id": tb["list"],
        "sender": "PILOTB",
        "text": "Hello from pilot B",
    },
)
check(st == 201 and c["status"] == "draft", "campaign draft created")
st, est = B.get(f"/v1/campaigns/{c['id']}/estimate")
check(
    st == 200 and est["recipients"] == 12,
    f"server estimate: {est['recipients']} recipients, total {est['total']} {est['currency']}",
)
st, c2 = B.post(f"/v1/campaigns/{c['id']}/schedule", {})
check(
    st == 200 and c2["status"] in ("scheduled", "preparing", "running"),
    f"scheduled/started ({c2['status']})",
)
st, again = B.post(f"/v1/campaigns/{c['id']}/schedule", {})
check(st == 409, "second schedule is a clean 409 (race / double-click)")
done = wait(
    lambda: (
        B.get(f"/v1/campaigns/{c['id']}")[1]
        if B.get(f"/v1/campaigns/{c['id']}")[1]["status"] == "completed"
        else None
    ),
    "campaign completes",
    timeout=90,
)
check(done["stats"]["recipients"].get("queued") == 12, "12 recipients queued (server count)")
msgs = wait(
    lambda: (lambda r: r if len(r["items"]) == 12 else None)(
        B.get("/v1/messages", campaign_id=c["id"], limit=50)[1]
    ),
    "12 campaign messages",
)
retry = [it for it in msgs["items"] if it["to"].endswith("0001")]
rest = [it for it in msgs["items"] if not it["to"].endswith("0001")]
for it in rest:
    wait(
        lambda it=it: (
            B.get(f"/v1/messages/{it['id']}")[1]["status"] in ("sent", "delivered", "failed")
        ),
        "campaign message sent/terminal",
        timeout=90,
    )
for it in rest:
    cur = B.get(f"/v1/messages/{it['id']}")[1]
    if cur["status"] == "sent":
        lib.dlr(cur["provider_message_id"])
wait(
    lambda: all(
        B.get(f"/v1/messages/{it['id']}")[1]["status"] in ("delivered", "failed") for it in rest
    ),
    "all non-retry campaign messages final",
)
fin = {it["to"][-4:]: B.get(f"/v1/messages/{it['id']}")[1] for it in rest}
check(
    fin["0002"]["status"] == "failed" and fin["0002"]["error_code"] == "fake_rejected",
    "FakeProvider permanent rejection (…0002) → FAILED fake_rejected",
)
check(
    all(v["status"] == "delivered" for k, v in fin.items() if k != "0002"),
    f"the other {len(rest) - 1} messages delivered via signed DLR",
)
check(
    len(retry) == 1
    and B.get(f"/v1/messages/{retry[0]['id']}")[1]["status"] in ("queued", "sending"),
    "temporary provider error (…0001) stays queued for retry — never shown as failed or UNKNOWN",
)
st, ov = B.get("/v1/reports/overview")
check(
    st == 200
    and ov["messages"]["total"] >= 13
    and ov["campaigns"]["by_status"].get("completed", 0) >= 1,
    "reports reflect the campaign",
)

print(
    "4 · Central revokes → projection → dispatch recheck blocks a queued message (provider not called)"
)
worker("stop", "w_sms")
st, q = lib.send_sms(B, "tenant-b", f"{tb['phone_prefix']}0012", "PILOTB")
check(st == 202, "message accepted while the dispatch worker is stopped (authorised at submit)")
st, rv = admin.post(f"/admin/senders/{cs['id']}/revoke", {"reason": "pilot revoke scenario"})
check(st == 200 and rv["status"] == "revoked", "admin revokes in Central")
wait(lambda: eff(B, s["id"])["effective_status"] == "revoked", "revocation projected to Enterprise")
check(eff(B, s["id"])["can_resubmit"] in (True, False), "UI fields present on the revoked sender")
st, body = lib.send_sms(B, "tenant-b", f"{tb['phone_prefix']}0013", "PILOTB")
check(st == 403, f"new submit is refused after revoke ({body['detail']['code']})")
worker("start", "w_sms")
fq = wait(
    lambda: (lambda r: r if r["status"] == "failed" else None)(B.get(f"/v1/messages/{q['id']}")[1]),
    "recheck result",
    timeout=60,
)
check(
    fq["error_code"] == "sender_revoked" and not fq["provider_message_id"],
    f"recheck failed the queued message without calling the provider ({fq['error_code']})",
)
w = [x for x in B.get("/v1/wallets")[1] if True] if B.get("/v1/wallets")[0] == 200 else []

print("5 · failure scenarios")
st, body = lib.send_sms(A, "tenant-a", f"{ta['phone_prefix']}0011", "NOSUCH")
check(
    st == 403 and body["detail"]["code"] == "sender_not_allowed",
    "unknown sender → 403 sender_not_allowed",
)
st, body = lib.send_sms(A, "tenant-a", "not-a-number", "PILOTA")
check(st in (400, 422), f"invalid recipient → {st}")
st, body = A.post("/v1/campaigns", {"owner_ref": "tenant-a", "name": ""})
check(st == 422, "validation error → 422")
worker("stop", "w_sender_req")
st, s3 = A.post("/v1/sender-ids", {"owner_ref": "tenant-a", "country": "AL", "value": "NOSYNC"})
check(
    st == 201 and s3["effective_status"] != "approved",
    f"new local request while the reporter is down (effective {s3['effective_status']}, sync {s3.get('sync_status')})",
)
st, body = lib.send_sms(A, "tenant-a", f"{ta['phone_prefix']}0011", "NOSYNC")
check(st == 403, f"not-synchronized/pending sender cannot send ({body['detail']['code']})")
worker("start", "w_sender_req")
st, m2 = lib.send_sms(A, "tenant-a", f"{ta['phone_prefix']}0002", "PILOTA")
check(st == 202, "number ending 0002 is accepted…")
fm = wait(
    lambda: (lambda r: r if r["status"] == "failed" else None)(
        A.get(f"/v1/messages/{m2['id']}")[1]
    ),
    "FakeProvider permanent rejection",
)
check(
    fm["error_code"] == "fake_rejected",
    "…and FakeProvider's permanent failure surfaces as FAILED fake_rejected",
)
# bilanc: zbrazim wallet-in e A, dërgimi jep 402, rikthejmë
wid = ta["wallet"]
bal = (
    float(A.get("/v1/me")[1] and [x for x in A.get("/v1/wallets")[1]][0]["available"])
    if A.get("/v1/wallets")[0] == 200
    else 100.0
)
st, _ = finance.post(
    f"/v1/wallets/{wid}/adjustments",
    {"delta": f"-{bal - 0.01:.6f}", "key": f"drain-{time.time_ns()}", "note": "pilot drain"},
)
check(st == 201, "finance drains tenant A's wallet (staff adjustment)")
st, body = lib.send_sms(A, "tenant-a", f"{ta['phone_prefix']}0015", "PILOTA")
check(
    st == 402 and body["detail"]["code"] == "insufficient_funds",
    "insufficient balance → 402 insufficient_funds",
)
st, _ = finance.post(
    f"/v1/wallets/{wid}/adjustments",
    {"delta": f"{bal - 0.01:.6f}", "key": f"restore-{time.time_ns()}", "note": "pilot restore"},
)
check(st == 201, "balance restored")
print(f"{lib.passed} checks")
