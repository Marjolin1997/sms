"""Gjeneron `cases.json` dhe `*.body` të golden-ve `cp.sender.v1`. VETËM për ndryshim të miratuar të kontratës.
Nga rrënja e repos:  .venv/bin/python -m tests.golden.control_plane_sender.regenerate
Testet NUK e thërrasin; krahasojnë me skedarët statikë."""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from packages.contracts.control_plane.sender import v1

HERE = Path(__file__).parent
U = lambda n: str(uuid.UUID(int=n))  # noqa: E731
AT = datetime(2030, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
ENT = U(0x111)


def policy(allowed=True, req=True, country="AL", kind="alphanumeric", pid=0xA1):
    return v1.PolicyStateV1(country, kind, allowed, req, U(pid), AT)


def reg(status="pending", decision="requested", display="Acme", country="AL", kind="alphanumeric", did=0xD1, src="default", pid=None, prev=None, ref="ext-1"):
    norm = display.lower()
    key = f"{country}:{norm}" if status == "approved" else None
    return v1.RegistryStateV1(ENT, ref, country, kind, display, norm, status, key, U(did), decision, AT, src, None if pid is None else U(pid), prev)


def ev(seq, etype, state, rev, gid=0x9001, gsize=1, eid=None):
    if etype == v1.EVENT_POLICY:
        ent, entity = None, v1.policy_entity_id(state.country, state.sender_kind)
    else:
        ent, entity = ENT, U(eid or 0xE1)
    return v1.SenderEventV1(U(0x500 + seq), seq, etype, ent, entity, rev, U(gid), gsize, AT, state)


def cases() -> list[tuple[str, list[v1.SenderEventV1]]]:
    P, R = v1.EVENT_POLICY, v1.EVENT_REGISTRY
    return [
        ("policy_explicit", [ev(1, P, policy(True, False), 1)]),
        ("policy_denied", [ev(2, P, policy(False, True, "XK", "numeric", 0xA2), 3)]),
        ("sender_pending", [ev(3, R, reg(), 1)]),
        ("sender_approved", [ev(4, R, reg("approved", "approved", did=0xD2, src="explicit", pid=0xA1, prev=1), 2)]),
        ("sender_rejected", [ev(5, R, reg("rejected", "rejected", did=0xD3), 2)]),
        ("sender_revoked", [ev(6, R, reg("revoked", "revoked", did=0xD4, src="explicit", pid=0xA2, prev=3), 3)]),
        (
            "policy_revocation_group",
            [
                ev(7, P, policy(False, True), 2, gid=0x9002, gsize=2),
                ev(8, R, reg("revoked", "revoked", did=0xD5, src="explicit", pid=0xA1, prev=2), 4, gid=0x9002, gsize=2),
            ],
        ),
    ]


def main() -> None:
    meta = []
    for name, events in cases():
        body = b"\n".join(e.to_bytes() for e in events)
        (HERE / f"{name}.body").write_bytes(body)
        meta.append({"name": name, "events": [e.to_dict() for e in events], "body_file": f"{name}.body"})
    (HERE / "cases.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
