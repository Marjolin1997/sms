"""Gjeneron `cases.json` dhe `*.body` të golden-ve `cp.billing.usage.v1`. VETËM për ndryshim të miratuar të kontratës.
Nga rrënja e repos:  .venv/bin/python -m tests.golden.control_plane_billing_usage.regenerate
Testet NUK e thërrasin; krahasojnë me skedarët statikë."""

import json
import uuid
from pathlib import Path

from packages.contracts.control_plane.billing import usage_v1 as bv

HERE = Path(__file__).parent
U = lambda n: str(uuid.UUID(int=n))  # noqa: E731


def doc(n, *, seq=1, watermark=0, count=0, at="2030-01-01T12:00:00.000000+00:00"):
    return {"schema": bv.SCHEMA, "report_id": U(0x700 + n), "report_seq": seq, "enterprise_id": U(0x111), "product_id": U(0xB1),
            "generated_at": at, "watermark": watermark, "cumulative_billable_count": count}  # fmt: skip


def cases() -> list[tuple[str, dict]]:
    return [
        ("empty_first_report", doc(1)),
        (
            "with_usage",
            doc(2, seq=2, watermark=17, count=15, at="2030-01-02T00:00:00.000000+00:00"),
        ),
        ("gaps_in_ids_are_allowed", doc(3, seq=3, watermark=1000, count=3)),
        ("edge_high_values", doc(4, seq=2**62, watermark=bv.INT64_MAX, count=bv.INT64_MAX)),
    ]


def main() -> None:
    meta = []
    for name, d in cases():
        r = bv.BillingUsageReportV1.parse(d)
        (HERE / f"{name}.body").write_bytes(r.to_bytes())
        meta.append(
            {
                "name": name,
                "input": d,
                "parsed": r.to_dict(),
                "payload_hash": r.payload_hash(),
                "content_hash": r.content_hash(),
                "body_file": f"{name}.body",
            }
        )
    (HERE / "cases.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
