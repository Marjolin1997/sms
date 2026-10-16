"""M9-c — kontrata `cp.money.v1`: paketë leaf, serializim kanonik, golden, mapper nga payload i ngrirë."""

import ast
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from apps.central.services import money_contract
from packages.contracts.control_plane.money import v1
from tests.golden.control_plane_money import regenerate as gen

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).parent / "golden" / "control_plane_money"
CASES = json.loads((GOLDEN / "cases.json").read_text())
U = lambda n: str(uuid.UUID(int=n))  # noqa: E731
T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)
REF = "cd" * 32


def data(**kw):
    base = dict(account_id=U(2), product_id=U(3), currency="EUR", amount=Decimal("40.000000"))
    return v1.GrantDataV1(**{**base, **kw})


def event(**kw):
    base = dict(event_id=U(1), seq=1, event_type=v1.EVENT_GRANT_ISSUED, enterprise_id=U(4),
                grant_id=U(5), occurred_at=T0, data=data())  # fmt: skip
    return v1.MoneyEventV1(**{**base, **kw})


def test_contract_package_is_a_stdlib_only_leaf():
    src = (ROOT / "packages/contracts/control_plane/money/v1.py").read_text()
    mods = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add((n.module or "").split(".")[0])
    assert mods <= {"json", "re", "uuid", "dataclasses", "datetime", "decimal", "typing"}, mods


def test_fixture_inventory_and_names():
    names = [c["name"] for c in CASES]
    assert len(names) == len(set(names)) == 11
    assert {p.name for p in GOLDEN.glob("*.body")} == {c["body_file"] for c in CASES}
    assert {"issued_standard", "reversed_standard", "issued_bootstrap", "reversed_bootstrap",
            "legacy_payload_without_purpose", "edge_smallest_amount", "edge_largest_amount",
            "edge_high_seq"} <= set(names)  # fmt: skip


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_money_event_row_to_contract_to_bytes_matches_golden_byte_for_byte(case):
    body = (GOLDEN / case["body_file"]).read_bytes()
    row = gen.to_row({**case["row"], "enterprise_id": uuid.UUID(case["row"]["enterprise_id"]),
                      "entity_id": uuid.UUID(case["row"]["entity_id"])})  # fmt: skip
    assert money_contract.to_bytes(row) == body
    assert json.loads(body) == case["parsed"]
    ev = v1.MoneyEventV1.from_bytes(body)
    assert ev.to_bytes() == body  # round-trip identik
    assert money_contract.to_event(row) == ev


def test_golden_fixtures_are_reproduced_by_the_generator_without_writing():
    rebuilt = list(gen.cases())
    assert [n for n, _ in rebuilt] == [c["name"] for c in CASES]
    for (name, r), case in zip(rebuilt, CASES, strict=True):
        assert (
            money_contract.to_bytes(gen.to_row(r)) == (GOLDEN / case["body_file"]).read_bytes()
        ), name


def test_legacy_m9b_payload_without_purpose_reads_as_standard():
    case = next(c for c in CASES if c["name"] == "legacy_payload_without_purpose")
    assert case["row"]["payload"].keys().isdisjoint({"purpose", "baseline_ref"})
    assert (
        case["parsed"]["data"]["purpose"] == "standard"
        and case["parsed"]["data"]["baseline_ref"] is None
    )


def test_serialization_is_canonical_and_ascii():
    b = event().to_bytes()
    assert b.decode("ascii") and b" " not in b
    keys = json.loads(b, object_pairs_hook=lambda p: [k for k, _ in p])
    assert keys == sorted(keys)
    assert json.loads(b)["data"]["amount"] == "40.000000"  # string, kurrë numër JSON


def test_amount_is_an_exact_six_decimal_string_never_float():
    assert v1.format_amount(Decimal("10.5")) == "10.500000"
    for bad in (
        1.5,
        "1.5",
        "10",
        "10.5000001",
        40,
        None,
        "-1.000000",
        "0.000000",
        "1e3",
        " 1.000000",
    ):
        with pytest.raises(v1.ContractError):
            v1.parse_amount(bad)
    with pytest.raises(v1.ContractError):
        v1.format_amount(1.5)  # type: ignore[arg-type]
    with pytest.raises(v1.ContractError):
        v1.format_amount(Decimal("1.0000001"))
    with pytest.raises(v1.ContractError):
        data(amount=Decimal("0"))
    with pytest.raises(v1.ContractError):
        data(amount=Decimal("100000000000000.000000"))
    d = event().to_dict()
    d["data"]["amount"] = 40.0
    with pytest.raises(v1.ContractError):
        v1.MoneyEventV1.from_dict(d)


def test_purpose_and_baseline_ref_rules():
    assert data(purpose="bootstrap", baseline_ref=REF).baseline_ref == REF
    for kw in (dict(purpose="bootstrap"), dict(purpose="bootstrap", baseline_ref="x"),
               dict(purpose="bootstrap", baseline_ref=REF.upper()), dict(purpose="standard", baseline_ref=REF),
               dict(purpose="other")):  # fmt: skip
        with pytest.raises(v1.ContractError):
            data(**kw)


def test_unknown_fields_schema_and_event_type_are_rejected():
    good = event().to_dict()
    for mutate in (
        lambda d: d.update(extra=1),
        lambda d: d["data"].update(extra=1),
        lambda d: d.pop("grant_id"),
        lambda d: d["data"].pop("purpose"),
    ):
        d = json.loads(json.dumps(good))
        mutate(d)
        with pytest.raises(v1.ContractError):
            v1.MoneyEventV1.from_dict(d)
    d = json.loads(json.dumps(good))
    d["schema"] = "cp.money.v2"
    with pytest.raises(v1.UnsupportedSchemaError):
        v1.MoneyEventV1.from_dict(d)
    d = json.loads(json.dumps(good))
    d["event_type"] = "credit_grant.deleted"
    with pytest.raises(v1.UnknownEventTypeError):
        v1.MoneyEventV1.from_dict(d)
    with pytest.raises(v1.ContractError):
        v1.MoneyEventV1.from_bytes(b"{nope")


def test_identifiers_seq_and_currency_are_validated():
    for kw in (dict(event_id="X"), dict(seq=0), dict(seq=True), dict(seq=v1.INT64_MAX + 1),
               dict(grant_id=U(0xAB).upper()), dict(enterprise_id="1")):  # fmt: skip
        with pytest.raises(v1.ContractError):
            event(**kw)
    for cur in ("eur", "EU", "EURO", "E1R", None, 3):
        with pytest.raises(v1.ContractError):
            data(currency=cur)
    assert event(seq=v1.INT64_MAX).seq == v1.INT64_MAX


def test_occurred_at_format_is_fixed_width_utc():
    assert v1.format_occurred_at(datetime(2030, 1, 1, 12)) == "2030-01-01T12:00:00.000000+00:00"
    with pytest.raises(v1.ContractError):
        v1.parse_occurred_at("2030-01-01T12:00:00Z")


def test_mapper_uses_only_the_frozen_row_payload_and_checks_identity():
    case = next(c for c in CASES if c["name"] == "issued_standard")
    r = dict(case["row"], enterprise_id=uuid.UUID(case["row"]["enterprise_id"]),
             entity_id=uuid.UUID(case["row"]["entity_id"]))  # fmt: skip
    row = gen.to_row(r)
    row.payload = {**row.payload, "amount": "99.000000"}  # payload i ngrirë = e vetmja burim
    assert money_contract.to_event(row).data.amount == Decimal("99.000000")
    row.payload = {k: v for k, v in row.payload.items() if k != "currency"}
    with pytest.raises(v1.ContractError):
        money_contract.to_event(row)
    row = gen.to_row(r)
    row.payload = {**row.payload, "grant_id": U(999)}
    with pytest.raises(v1.ContractError):
        money_contract.to_event(row)
