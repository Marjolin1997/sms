"""Gjeneron `cases.json` dhe `*.body` të golden-ve `cp.money.v1` nga kodi AKTUAL (grant real → payload i ngrirë
→ mapper → bytes). VETËM për ndryshim të miratuar të kontratës. Nga rrënja e repos:
    .venv/bin/python -m tests.golden.control_plane_money.regenerate
Testet NUK e thërrasin; krahasojnë me skedarët statikë. Mos rigjenero pa miratim të qartë."""

import json
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from apps.central.models import CreditGrant, MoneyEvent
from apps.central.models.money import EVENT_GRANT_ISSUED, EVENT_GRANT_REVERSED
from apps.central.services import grants, money_contract

HERE = Path(__file__).parent
U = lambda n: str(uuid.UUID(int=n))  # noqa: E731
ENT, ACCT, PROD, GRANT = (uuid.UUID(U(n)) for n in (0x111, 0x222, 0xA1, 0x333))
REF = "ab" * 32
T = datetime.fromisoformat


def grant(
    amount="40.000000",
    currency="EUR",
    purpose="standard",
    ref=None,
    created="2030-01-01T12:00:00+00:00",
):
    return CreditGrant(
        id=GRANT, account_id=ACCT, enterprise_id=ENT, product_id=PROD, currency=currency,
        amount=Decimal(amount), status="active", purpose=purpose, baseline_ref=ref,
        created_at=T(created), reversed_at=T("2030-01-02T12:00:00+00:00"),
    )  # fmt: skip


def row(g, etype, seq, event_no, created="2030-01-01T12:00:00+00:00", payload=None):
    return dict(
        seq=seq, event_id=U(event_no), event_type=etype, enterprise_id=ENT, entity_id=GRANT,
        payload=payload if payload is not None else grants._payload(g, etype), created_at=created,
    )  # fmt: skip


def cases():
    std, boot = grant(), grant("1200.000000", purpose="bootstrap", ref=REF)
    legacy = grants._payload(std, EVENT_GRANT_ISSUED)
    del legacy["purpose"], legacy["baseline_ref"]  # ngjarje e M9-b (para purpose)
    return [
        ("issued_standard", row(std, EVENT_GRANT_ISSUED, 1, 1)),
        ("reversed_standard", row(std, EVENT_GRANT_REVERSED, 2, 2)),
        ("issued_bootstrap", row(boot, EVENT_GRANT_ISSUED, 3, 3)),
        ("reversed_bootstrap", row(boot, EVENT_GRANT_REVERSED, 4, 4)),
        ("legacy_payload_without_purpose", row(std, EVENT_GRANT_ISSUED, 5, 5, payload=legacy)),
        ("edge_smallest_amount", row(grant("0.000001"), EVENT_GRANT_ISSUED, 6, 6)),
        ("edge_largest_amount", row(grant("99999999999999.999999"), EVENT_GRANT_ISSUED, 7, 7)),
        ("edge_high_seq", row(std, EVENT_GRANT_ISSUED, 9007199254740993, 8)),
        ("edge_microseconds_nonzero", row(std, EVENT_GRANT_ISSUED, 10, 9, created="2030-01-01T12:00:00.123456+00:00")),
        ("edge_offset_created_at", row(std, EVENT_GRANT_ISSUED, 11, 10, created="2030-01-01T14:00:00+02:00")),
        ("edge_trailing_zero_amount", row(grant("10.500000"), EVENT_GRANT_ISSUED, 12, 11)),
    ]  # fmt: skip


def to_row(r: dict) -> MoneyEvent:
    return MoneyEvent(
        seq=r["seq"], event_id=uuid.UUID(r["event_id"]), event_type=r["event_type"],
        enterprise_id=r["enterprise_id"], account_id=ACCT, entity_type="credit_grant",
        entity_id=r["entity_id"], payload=r["payload"], created_at=T(r["created_at"]),
    )  # fmt: skip


def main() -> None:
    out = []
    for name, r in cases():
        body = money_contract.to_bytes(to_row(r))
        (HERE / f"{name}.body").write_bytes(body)
        out.append({"name": name, "body_file": f"{name}.body", "parsed": json.loads(body),
                    "row": {**r, "enterprise_id": str(r["enterprise_id"]), "entity_id": str(r["entity_id"])}})  # fmt: skip
    (HERE / "cases.json").write_text(json.dumps(out, indent=1, ensure_ascii=True) + "\n")
    print(len(out), "cases")


if __name__ == "__main__":
    main()
