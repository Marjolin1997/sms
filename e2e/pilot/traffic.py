"""Trafik shadow pas bootstrap-it (mostra për provën e cutover-it): N mesazhe tenant A → FakeProvider → DLR delivered."""

import sys

from e2e.pilot import lib
from e2e.pilot.lib import check, wait

n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
A, ta = lib.tenant("a")
ids = []
for i in range(n):
    st, m = lib.send_sms(A, "tenant-a", f"{ta['phone_prefix']}{i + 31:04d}", "PILOTA")
    check(st == 202, f"message {i + 1} accepted")
    ids.append(m["id"])
for mid in ids:
    wait(
        lambda mid=mid: A.get(f"/v1/messages/{mid}")[1].get("status") in ("sent", "delivered"),
        "dispatched",
    )
    st, _ = lib.dlr(A.get(f"/v1/messages/{mid}")[1]["provider_message_id"])
    check(st == 200, "DLR applied")
