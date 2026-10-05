"""M9-d — kontrata `cp.money.usage.v1`: golden, kanonike, validim strikt, ekuacioni, hash-et."""

import copy
import json
from decimal import Decimal as D
from pathlib import Path

import pytest

from packages.contracts.control_plane.money import usage_v1 as uv
from tests.golden.control_plane_money_usage import regenerate as gen

GOLDEN = Path(__file__).parent / "golden" / "control_plane_money_usage"
CASES = json.loads((GOLDEN / "cases.json").read_text())


def base():
    return copy.deepcopy(
        next(c for c in CASES if c["name"] == "central_applied_and_reversed")["input"]
    )


def test_fixture_inventory():
    names = [c["name"] for c in CASES]
    assert len(names) == len(set(names)) == 9
    assert {p.name for p in GOLDEN.glob("*.body")} == {c["body_file"] for c in CASES}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_golden_bytes_roundtrip_and_hashes(case):
    body = (GOLDEN / case["body_file"]).read_bytes()
    r = uv.UsageReportV1.parse(case["input"])
    assert r.to_bytes() == body and json.loads(body) == case["parsed"]
    again = uv.UsageReportV1.from_bytes(body)
    assert (
        again.to_bytes() == body
        and again.payload_hash() == case["payload_hash"] == r.payload_hash()
    )
    assert again.content_hash() == case["content_hash"]


def test_golden_is_reproduced_by_the_generator_without_writing():
    for (name, d), case in zip(gen.cases(), CASES, strict=True):
        assert uv.UsageReportV1.parse(d).to_bytes() == (GOLDEN / case["body_file"]).read_bytes(), (
            name
        )


def test_canonical_form_is_independent_of_key_and_grant_order():
    d = base()
    shuffled = copy.deepcopy(d)
    shuffled["grants"].reverse()
    shuffled = {k: shuffled[k] for k in reversed(list(shuffled))}
    assert uv.UsageReportV1.parse(shuffled).to_bytes() == uv.UsageReportV1.parse(d).to_bytes()
    b = uv.UsageReportV1.parse(d).to_bytes()
    assert b.decode("ascii") and b" " not in b


def test_content_hash_ignores_identity_time_and_cursor_motion_but_not_money():
    d = base()
    r = uv.UsageReportV1.parse(d)
    d2 = copy.deepcopy(d)
    d2.update(
        report_id="00000000-0000-0000-0000-000000000999",
        report_seq=77,
        generated_at="2031-01-01T00:00:00.000000+00:00",
    )
    d2["cursor"].update(last_success_at="2031-01-01T00:00:00.000000+00:00", last_seq=999)
    r2 = uv.UsageReportV1.parse(d2)
    assert r.content_hash() == r2.content_hash() and r.payload_hash() != r2.payload_hash()
    d3 = copy.deepcopy(d)
    d3["flows"]["captured"] = "0.000001"
    assert uv.UsageReportV1.parse(d3).content_hash() != r.content_hash()
    d4 = copy.deepcopy(d)
    d4["grants"][0]["status"] = "reversed"
    assert uv.UsageReportV1.parse(d4).content_hash() != r.content_hash()


def test_conservation_equation_uses_the_real_ledger_semantics():
    d = base()
    assert uv.UsageReportV1.parse(d).conservation_gap() == 0  # 100 applied − 60 reversed = 40
    d["flows"]["captured"] = "1.000000"
    assert uv.UsageReportV1.parse(d).conservation_gap() == D(
        "1"
    )  # gross 40 ≠ 39: boshllëk i dukshëm
    d["flows"].update(captured="0.000000", positive_local_credit="2.000000")
    assert uv.UsageReportV1.parse(d).conservation_gap() == D("-2")
    local = copy.deepcopy(next(c for c in CASES if c["name"] == "local_plain")["input"])
    assert uv.UsageReportV1.parse(local).conservation_gap() == 0  # 10 credit − 0.5 captured = 9.5


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(x=1),
        lambda d: d.pop("integrity"),
        lambda d: d.update(schema="cp.money.usage.v2"),
        lambda d: d["wallet"].update(gross="1.000000"),
        lambda d: d["wallet"].update(held=1.5),
        lambda d: d["flows"].update(captured="-0.000001"),
        lambda d: d["flows"].pop("released"),
        lambda d: d["integrity"].update(orphan_grant_credit="-1.000000"),
        lambda d: d["grants"][0].update(status="weird"),
        lambda d: d["grants"][0].update(amount="1"),
        lambda d: d["grants"].append(copy.deepcopy(d["grants"][0])),  # grant_id i dyfishtë
        lambda d: d["grants"][0].update(extra=1),
        lambda d: d["grants"][0].update(detail="x" * 201),
        lambda d: d["cursor"].update(has_error="no"),
        lambda d: d["cursor"].update(generation=0),
        lambda d: d.update(report_seq=0),
        lambda d: d.update(ledger_max_id=-1),
        lambda d: d.update(ledger_max_id=True),
        lambda d: d.update(currency="eur"),
        lambda d: d.update(authority_mode="x"),
        lambda d: d.update(
            baseline={
                "baseline_ref": "short",
                "gross_at_cutover": "1.000000",
                "ledger_max_id": 0,
                "status": "active",
            }
        ),
        lambda d: d.update(generated_at="2030-01-01T12:00:00Z"),
        lambda d: d.update(
            report_id=d["report_id"].upper() if d["report_id"].upper() != d["report_id"] else "X"
        ),
    ],
)
def test_invalid_reports_are_rejected(mutate):
    d = base()
    mutate(d)
    with pytest.raises(uv.ContractError):
        uv.UsageReportV1.parse(d)


def test_unsupported_schema_and_invalid_json_and_float_amounts():
    d = base()
    d["schema"] = "other"
    with pytest.raises(uv.UnsupportedSchemaError):
        uv.UsageReportV1.parse(d)
    with pytest.raises(uv.ContractError):
        uv.UsageReportV1.from_bytes(b"{no")
    with pytest.raises(uv.ContractError):
        uv.format_amount(1.5)  # type: ignore[arg-type]
    with pytest.raises(uv.ContractError):
        uv.format_amount(D("1.0000001"))
    assert uv.format_amount(D("10.5")) == "10.500000"


def test_signed_balances_are_allowed_but_totals_are_not_negative():
    d = next(c for c in CASES if c["name"] == "negative_balance_is_representable")["input"]
    r = uv.UsageReportV1.parse(d)
    assert D(r.doc["wallet"]["available"]) < 0


def test_size_cap_on_grants():
    d = base()
    row = d["grants"][0]
    d["grants"] = [
        {**row, "grant_id": str(__import__("uuid").UUID(int=i + 1))}
        for i in range(uv.MAX_GRANTS + 1)
    ]
    with pytest.raises(uv.ContractError):
        uv.UsageReportV1.parse(d)
