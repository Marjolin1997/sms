"""Gjeneron `cases.json` dhe `*.body` të golden-ve `cp.v1` nga kodi AKTUAL (payload real → mapper → bytes).

VETËM për ndryshim të miratuar të kontratës (version i ri ose shtim i rishikuar). Nga rrënja e repos:
    .venv/bin/python -m tests.golden.control_plane.regenerate
Testet NUK e thërrasin; krahasojnë me skedarët statikë. Mos rigjenero pa miratim të qartë.
"""

import json
import uuid
from datetime import datetime
from pathlib import Path

from apps.central.models import Enterprise, EnterpriseProduct, Product, SyncOutbox
from apps.central.services import sync, sync_contract

HERE = Path(__file__).parent
U = lambda n: str(uuid.UUID(int=n))  # noqa: E731  (UUID fiks, kanonik)
ENT, ENT2 = uuid.UUID(U(0x111)), uuid.UUID(U(0x222))
P_SMS, P_EMAIL = uuid.UUID(U(0xA1)), uuid.UUID(U(0xA2))
T = datetime.fromisoformat


def enterprise_row(name, status, **kw):
    e = Enterprise(id=ENT, name=name, status=status)
    return dict(entity_type="enterprise", entity_id=ENT, enterprise_id=ENT,
                event_type=sync.EVENT_ENTERPRISE, payload=sync.enterprise_payload(e), **kw)  # fmt: skip


def assignment_row(code, channel, status, a_id, **kw):
    pid = P_SMS if channel == "sms" else P_EMAIL
    product = Product(id=pid, code=code, name=code, channel=channel)
    ep = EnterpriseProduct(id=a_id, enterprise_id=ENT, product_id=pid, status=status)
    return dict(entity_type="enterprise_product", entity_id=a_id, enterprise_id=ENT,
                event_type=sync.EVENT_ASSIGNMENT, payload=sync.assignment_payload(ep, product), **kw)  # fmt: skip


def cases():
    a1, a2 = uuid.UUID(U(0xB1)), uuid.UUID(U(0xB2))
    ts = "2030-01-01T12:00:00+00:00"
    return [
        ("enterprise_active", enterprise_row("Acme", "active", seq=1, revision=1, event_id=U(1), created_at=ts)),
        ("enterprise_suspended", enterprise_row("Acme", "suspended", seq=2, revision=2, event_id=U(2), created_at=ts)),
        ("enterprise_non_ascii", enterprise_row("Shoqëria Ç 你好 😀", "active", seq=3, revision=3, event_id=U(3), created_at=ts)),
        ("sms_active", assignment_row("sms", "sms", "active", a1, seq=4, revision=1, event_id=U(4), created_at=ts)),
        ("sms_suspended", assignment_row("sms", "sms", "suspended", a1, seq=5, revision=2, event_id=U(5), created_at=ts)),
        ("email_active", assignment_row("email", "email", "active", a2, seq=6, revision=1, event_id=U(6), created_at=ts)),
        ("email_suspended", assignment_row("email", "email", "suspended", a2, seq=7, revision=2, event_id=U(7), created_at=ts)),
        ("edge_high_seq", enterprise_row("Acme", "active", seq=9007199254740993, revision=1, event_id=U(8), created_at=ts)),
        ("edge_high_revision", enterprise_row("Acme", "active", seq=9, revision=4294967296, event_id=U(9), created_at=ts)),
        ("edge_microseconds_zero", enterprise_row("Acme", "active", seq=10, revision=1, event_id=U(10), created_at="2030-01-01T12:00:00+00:00")),
        ("edge_microseconds_nonzero", enterprise_row("Acme", "active", seq=11, revision=1, event_id=U(11), created_at="2030-01-01T12:00:00.123456+00:00")),
        ("edge_naive_created_at", enterprise_row("Acme", "active", seq=12, revision=1, event_id=U(12), created_at="2030-01-01T12:00:00")),
        ("edge_offset_created_at", enterprise_row("Acme", "active", seq=13, revision=1, event_id=U(13), created_at="2030-01-01T14:00:00+02:00")),
    ]  # fmt: skip


def to_outbox(row: dict) -> SyncOutbox:
    return SyncOutbox(
        seq=row["seq"], event_id=uuid.UUID(row["event_id"]), enterprise_id=row["enterprise_id"],
        entity_type=row["entity_type"], entity_id=row["entity_id"], revision=row["revision"],
        event_type=row["event_type"], payload=row["payload"], created_at=T(row["created_at"]),
    )  # fmt: skip


def main() -> None:
    out = []
    for name, row in cases():
        body = sync_contract.to_bytes(to_outbox(row))
        (HERE / f"{name}.body").write_bytes(body)
        out.append({
            "name": name, "body_file": f"{name}.body", "parsed": json.loads(body),
            "row": {**row, "enterprise_id": str(row["enterprise_id"]),
                    "entity_id": str(row["entity_id"])},
        })  # fmt: skip
    (HERE / "cases.json").write_text(json.dumps(out, indent=1, ensure_ascii=True) + "\n")
    print(len(out), "cases")


if __name__ == "__main__":
    main()
