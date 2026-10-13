"""M11-H · faza 1 (authority=shadow): kërkesë sender → Central → miratim → mesazhe FakeProvider → DLR → prova e shadow për cutover.

. $PILOT_DIR/pilot.env && python -m e2e.pilot.flow_shadow
"""

from e2e.pilot import lib
from e2e.pilot.lib import check, wait

A, ta = lib.tenant("a")
admin = lib.central("PILOT_ADMIN_PASSWORD")
approver = lib.staff("approver")

print("sender request (tenant A) → Central")
st, s = A.post("/v1/sender-ids", {"owner_ref": "tenant-a", "country": "AL", "value": "PILOTA"})
check(
    st == 201 and s["status"] == "pending", f"Enterprise accepts the sender request (pending): {st}"
)
cs = wait(
    lambda: lib.central_sender(admin, "PILOTA"), "Central sees the request via the request reporter"
)
check(
    cs["status"] == "pending" and cs["enterprise_id"] == ta["enterprise_id"],
    "Central has it as pending for the right enterprise",
)
st, row = admin.post(f"/admin/senders/{cs['id']}/approve", {})
check(st == 200 and row["status"] == "approved", "Central admin approves")
wait(
    lambda: approver.get(f"/v1/sender-ids/{s['id']}")[1].get("central_status") == "approved",
    "Central's approval is projected to Enterprise before any traffic",
)
st, ls = approver.post(f"/v1/sender-ids/{s['id']}/approve", {})
check(st == 200, "shadow mode: the local approver still decides locally")

print("send in shadow mode with FakeProvider")
sent = []
for i in range(6):
    st, m = lib.send_sms(A, "tenant-a", f"{ta['phone_prefix']}{i + 11:04d}", "PILOTA")
    check(st == 202, f"message {i + 1} accepted (202)")
    sent.append(m["id"])
for mid in sent:
    wait(
        lambda mid=mid: A.get(f"/v1/messages/{mid}")[1].get("status") in ("sent", "delivered"),
        f"worker dispatched {mid}",
    )
for mid in sent:
    pm = A.get(f"/v1/messages/{mid}")[1]["provider_message_id"]
    check(pm and pm.startswith("fake-"), f"FakeProvider handled {mid}")
    st, _ = lib.dlr(pm, "delivered")
    check(st == 200, "signed DLR webhook applied")
for mid in sent:
    wait(
        lambda mid=mid: A.get(f"/v1/messages/{mid}")[1]["status"] == "delivered", f"{mid} delivered"
    )
check(True, "all shadow messages delivered via the in-process fake provider")
print(f"{lib.passed} checks")
