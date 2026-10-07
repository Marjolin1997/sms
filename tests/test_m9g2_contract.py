"""M9-g2 — kontrata `cp.billing.usage.v1`: goldens, validim strikt, kanonikalitet, hash-e."""

import json
import uuid
from pathlib import Path

import pytest

from packages.contracts.control_plane.billing import usage_v1 as bv

GOLD = Path(__file__).parent / "golden" / "control_plane_billing_usage"
CASES = json.loads((GOLD / "cases.json").read_text())
U = lambda n: str(uuid.UUID(int=n))  # noqa: E731


def base(**kw):
    d = {"schema": bv.SCHEMA, "report_id": U(1), "report_seq": 1, "enterprise_id": U(2), "product_id": U(3),
         "generated_at": "2030-01-01T12:00:00.000000+00:00", "watermark": 5, "cumulative_billable_count": 3}  # fmt: skip
    d.update(kw)
    return d


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_goldens_are_byte_exact_and_round_trip(case):
    r = bv.BillingUsageReportV1.parse(case["input"])
    body = (GOLD / case["body_file"]).read_bytes()
    assert r.to_bytes() == body and r.to_dict() == case["parsed"]
    assert r.payload_hash() == case["payload_hash"] and r.content_hash() == case["content_hash"]
    assert bv.BillingUsageReportV1.from_bytes(body).payload_hash() == case["payload_hash"]


def test_content_hash_ignores_identity_and_time_but_not_state():
    a = bv.BillingUsageReportV1.parse(base())
    b = bv.BillingUsageReportV1.parse(
        base(report_id=U(9), report_seq=7, generated_at="2031-01-01T00:00:00.000000+00:00")
    )
    c = bv.BillingUsageReportV1.parse(base(cumulative_billable_count=4))
    assert (
        a.content_hash() == b.content_hash() != c.content_hash()
        and a.payload_hash() != b.payload_hash()
    )


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema": "cp.billing.usage.v2"},
        {"extra": 1},
        {"report_id": "X"},
        {"report_id": "AAAAAAAA-0000-0000-0000-00000000000A"},
        {"enterprise_id": 5},
        {"report_seq": 0},
        {"report_seq": -1},
        {"report_seq": True},
        {"report_seq": 1.0},
        {"report_seq": bv.INT64_MAX + 1},
        {"watermark": -1},
        {"watermark": "5"},
        {"cumulative_billable_count": 6},
        {"watermark": 0},
        {"cumulative_billable_count": 0},
        {"generated_at": "2030-01-01T12:00:00+00:00"},
        {"generated_at": "2030-01-01T12:00:00.000000+02:00"},
        {"generated_at": "2030-13-01T12:00:00.000000+00:00"},
        {"generated_at": 5},
    ],
)
def test_strict_validation_rejects(mutation):
    with pytest.raises(bv.ContractError):
        bv.BillingUsageReportV1.parse(base(**mutation))


def test_missing_fields_and_non_objects_are_rejected():
    for key in list(base()):
        d = base()
        del d[key]
        with pytest.raises(bv.ContractError):
            bv.BillingUsageReportV1.parse(d)
    for bad in (None, [], "x", 5):
        with pytest.raises(bv.ContractError):
            bv.BillingUsageReportV1.parse(bad)
    with pytest.raises(bv.UnsupportedSchemaError):
        bv.BillingUsageReportV1.parse(base(schema="nope"))


def test_zero_state_and_gaps_are_valid():
    z = bv.BillingUsageReportV1.parse(base(watermark=0, cumulative_billable_count=0))
    g = bv.BillingUsageReportV1.parse(base(watermark=10**6, cumulative_billable_count=2))
    assert z.count == 0 and g.watermark == 10**6 and g.count == 2
