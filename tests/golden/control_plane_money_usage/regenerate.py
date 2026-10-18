"""Gjeneron `cases.json` dhe `*.body` të golden-ve `cp.money.usage.v1` (dokumente eksplicite → kontrata → bytes
kanonike). VETËM për ndryshim të miratuar të kontratës. Nga rrënja e repos:
    .venv/bin/python -m tests.golden.control_plane_money_usage.regenerate
Testet NUK e thërrasin; krahasojnë me skedarët statikë."""

import json
import uuid
from pathlib import Path

from packages.contracts.control_plane.money import usage_v1 as uv

HERE = Path(__file__).parent
U = lambda n: str(uuid.UUID(int=n))  # noqa: E731
REF = "ab" * 32
Z = "0.000000"


def grant(
    n,
    status="applied",
    amount="40.000000",
    purpose="standard",
    ref=None,
    seq=None,
    rev=None,
    detail=None,
):
    return {"grant_id": U(0x100 + n), "status": status, "amount": amount, "currency": "EUR", "product_id": U(0xA1),
            "purpose": purpose, "baseline_ref": ref, "issued_seq": seq or n, "reversed_seq": rev,
            "updated_at": "2030-01-01T11:00:00.000000+00:00", "detail": detail}  # fmt: skip


def doc(n, *, mode="central", seq=1, avail="40.000000", held=Z, hold_total=None, hold_count=0, baseline=None, flows=None,
        integrity=None, cursor_seq=7, grants=(), ledger_max_id=12, currency="EUR"):  # fmt: skip
    gross = format(__import__("decimal").Decimal(avail) + __import__("decimal").Decimal(held), "f")
    f = {"baseline_gross": Z, "grants_applied": gross, "grant_reversals": Z, "captured": Z, "negative_adjustments": Z,
         "invoice_debits": Z, "other_debits": Z, "positive_local_credit": Z, "released": Z, **(flows or {})}  # fmt: skip
    return {
        "schema": uv.SCHEMA, "report_id": U(0x500 + n), "report_seq": seq, "enterprise_id": U(0x111), "product_id": U(0xA1),
        "currency": currency, "generated_at": "2030-01-01T12:00:00.000000+00:00", "authority_mode": mode,
        "ledger_max_id": ledger_max_id,
        "wallet": {"available": avail, "held": held, "gross": gross, "active_hold_total": hold_total or held,
                   "active_hold_count": hold_count},
        "baseline": baseline, "flows": f,
        "integrity": {"ledger_sum_available": avail, "ledger_sum_held": held, "orphan_grant_credit": Z,
                      "orphan_grant_reversal": Z, **(integrity or {})},
        "cursor": {"epoch": U(0xE0), "last_seq": cursor_seq, "generation": 1,
                   "last_success_at": "2030-01-01T11:59:30.000000+00:00", "has_error": False},
        "grants": list(grants),
    }  # fmt: skip


def cases():
    base = {
        "baseline_ref": REF,
        "gross_at_cutover": "12.000000",
        "ledger_max_id": 3,
        "status": "active",
    }
    return [
        ("local_plain", doc(1, mode="local", avail="9.500000", flows={"grants_applied": Z, "positive_local_credit": "10.000000", "captured": "0.500000"})),
        ("shadow_baseline_deferred", doc(2, mode="shadow", avail="10.000000", held="2.000000", hold_count=1, baseline=base,
            flows={"baseline_gross": "12.000000", "grants_applied": Z},
            grants=[grant(1, "matched_to_existing_balance", "12.000000", "bootstrap", REF), grant(2, "deferred_shadow", "5.000000")])),
        ("central_applied_and_reversed", doc(3, mode="central", avail="40.000000",
            flows={"grants_applied": "100.000000", "grant_reversals": "60.000000"},
            grants=[grant(1, "applied", "40.000000"), grant(2, "reversed", "60.000000", rev=9)])),
        ("central_unresolved_reversal", doc(4, mode="central", avail="1.000000", held="29.000000", hold_count=2,
            flows={"grants_applied": "30.000000"},
            grants=[grant(1, "reconciliation_required", "30.000000", rev=8, detail="insufficient available funds for reversal: available=1 amount=30")])),
        ("negative_balance_is_representable", doc(5, mode="central", avail="-1.000000", flows={"grants_applied": Z})),
        ("edge_micro_amounts", doc(6, mode="central", avail="0.000001", flows={"grants_applied": "0.000001"}, grants=[grant(1, "applied", "0.000001")])),
        ("edge_large_amounts", doc(7, mode="central", avail="99999999999999.999999", flows={"grants_applied": "99999999999999.999999"})),
        ("edge_high_seq", doc(8, seq=9007199254740993, cursor_seq=9007199254740993, ledger_max_id=9007199254740993)),
        ("shadow_orphan_integrity", doc(9, mode="shadow", avail="3.000000", flows={"grants_applied": "3.000000"}, integrity={"orphan_grant_credit": "3.000000"})),
    ]  # fmt: skip


def main() -> None:
    out = []
    for name, d in cases():
        r = uv.UsageReportV1.parse(d)
        (HERE / f"{name}.body").write_bytes(r.to_bytes())
        out.append({"name": name, "body_file": f"{name}.body", "input": d, "parsed": json.loads(r.to_bytes()),
                    "payload_hash": r.payload_hash(), "content_hash": r.content_hash()})  # fmt: skip
    (HERE / "cases.json").write_text(json.dumps(out, indent=1, ensure_ascii=True) + "\n")
    print(len(out), "cases")


if __name__ == "__main__":
    main()
