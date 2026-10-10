"""Gjeneron `cases.json` dhe `*.body` të golden-ve `sender.request.v1`. VETËM për ndryshim të miratuar të kontratës.
Nga rrënja e repos:  .venv/bin/python -m tests.golden.control_plane_sender_request.regenerate
Testet NUK e thërrasin; krahasojnë me skedarët statikë."""

import json
import uuid
from pathlib import Path

from packages.contracts.control_plane.sender import request_v1 as rv

HERE = Path(__file__).parent
U = lambda n: str(uuid.UUID(int=n))  # noqa: E731
ENT = U(0x111)


def make(name, **over):
    base = dict(
        operation_id=U(0xA1), operation="request", enterprise_id=ENT,
        external_ref=rv.external_ref_for(7), country="AL", sender_kind="alphanumeric",
        display_value="Acme", evidence_ref=None,
    )  # fmt: skip
    base.update(over)
    return name, rv.SenderRequestV1.build(**base)


def cases():
    return [
        make("request_alphanumeric"),
        make("request_numeric", sender_kind="numeric", display_value="355691234567", external_ref=rv.external_ref_for(8), operation_id=U(0xA2)),
        make("resubmit", operation="resubmit", operation_id=U(0xA3)),
        make("request_with_evidence", evidence_ref="ticket-4711", operation_id=U(0xA4)),
        make("request_mixed_case", display_value="AcMe Co", operation_id=U(0xA5)),
    ]  # fmt: skip


def main() -> None:
    out = []
    for name, r in cases():
        (HERE / f"{name}.body").write_bytes(r.to_bytes())
        out.append({"name": name, "hash": r.request_hash()})
    (HERE / "cases.json").write_text(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    main()
