"""M10-S2 — kontrata `cp.sender.v1`: paketë leaf (stdlib), zarf strikt, payload additiv, golden byte-për-byte, round-trip, validime."""

import ast
import json
from pathlib import Path

import pytest

from packages.contracts.control_plane.sender import v1
from tests.golden.control_plane_sender import regenerate as gen

GOLDEN = Path(__file__).parent / "golden" / "control_plane_sender"
CASES = json.loads((GOLDEN / "cases.json").read_text())


def test_contract_is_a_leaf_package_using_only_the_stdlib():
    src = (
        Path(__file__).resolve().parents[1]
        / "packages"
        / "contracts"
        / "control_plane"
        / "sender"
        / "v1.py"
    ).read_text()
    mods = {
        n.module.split(".")[0] if isinstance(n, ast.ImportFrom) else a.name.split(".")[0]
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.ImportFrom | ast.Import)
        for a in (n.names if isinstance(n, ast.Import) else [n])
    }
    assert mods <= {"json", "re", "uuid", "dataclasses", "datetime", "typing"}, mods


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_goldens_round_trip_and_match_bytes(case):
    body = (GOLDEN / case["body_file"]).read_bytes()
    events = [v1.SenderEventV1.from_bytes(line) for line in body.split(b"\n")]
    assert [e.to_dict() for e in events] == case["events"]
    assert b"\n".join(e.to_bytes() for e in events) == body


def test_golden_fixtures_are_reproduced_by_the_generator_without_writing():
    for name, events in gen.cases():
        assert (GOLDEN / f"{name}.body").read_bytes() == b"\n".join(e.to_bytes() for e in events)


def d(name):
    return json.loads(json.dumps(next(c for c in CASES if c["name"] == name)["events"][0]))


def test_strict_envelope_rejects_unknown_missing_and_wrong_schema_or_type():
    base = d("sender_approved")
    for mutate in (lambda e: e.update(extra=1), lambda e: e.pop("group"), lambda e: e.pop("revision"), lambda e: e.update(entity={"type": "sender_registry"}),
                   lambda e: e.update(entity={"type": "sender_policy", "id": e["entity"]["id"]}), lambda e: e.update(group={"id": e["group"]["id"]}),
                   lambda e: e.update(seq=0), lambda e: e.update(seq=True), lambda e: e.update(revision=0), lambda e: e.update(occurred_at="2030-01-01")):  # fmt: skip
        e = json.loads(json.dumps(base))
        mutate(e)
        with pytest.raises(v1.ContractError):
            v1.SenderEventV1.from_dict(e)
    e = json.loads(json.dumps(base))
    e["schema"] = "cp.sender.v2"
    with pytest.raises(v1.UnsupportedSchemaError):
        v1.SenderEventV1.from_dict(e)
    e = json.loads(json.dumps(base))
    e["event_type"] = "sender.other"
    with pytest.raises(v1.UnknownEventTypeError):
        v1.SenderEventV1.from_dict(e)


def test_payload_is_additive_unknown_data_fields_are_ignored_but_known_ones_are_validated():
    e = d("sender_approved")
    e["data"]["future_field"] = {"x": 1}
    parsed = v1.SenderEventV1.from_dict(e)
    assert "future_field" not in parsed.to_dict()["data"]
    p = d("policy_explicit")
    p["data"]["note"] = "ignored"
    assert v1.SenderEventV1.from_dict(p).data.allowed is True
    for field, bad in (
        ("status", "weird"),
        ("sender_kind", "x"),
        ("decided_at", "yesterday"),
        ("approved_key", None),
        ("policy_revision", 0),
    ):
        e = d("sender_approved")
        e["data"][field] = bad
        with pytest.raises(v1.ContractError):
            v1.SenderEventV1.from_dict(e)
    e = d("sender_approved")
    del e["data"]["norm_value"]
    with pytest.raises(v1.ContractError):
        v1.SenderEventV1.from_dict(e)


def test_cross_field_invariants_policy_global_registry_scoped_and_consistent_keys():
    p = d("policy_explicit")
    p["enterprise_id"] = "00000000-0000-0000-0000-000000000111"
    with pytest.raises(v1.ContractError):
        v1.SenderEventV1.from_dict(p)
    p = d("policy_explicit")
    p["entity"]["id"] = "00000000-0000-0000-0000-000000000001"
    with pytest.raises(v1.ContractError):
        v1.SenderEventV1.from_dict(p)
    r = d("sender_approved")
    r["enterprise_id"] = "00000000-0000-0000-0000-000000000222"
    with pytest.raises(v1.ContractError):
        v1.SenderEventV1.from_dict(r)
    r = d("sender_pending")
    r["data"]["approved_key"] = "AL:acme"  # çelës pa miratim
    with pytest.raises(v1.ContractError):
        v1.SenderEventV1.from_dict(r)
    r = d("sender_approved")
    r["data"]["approved_key"] = "AL:other"
    with pytest.raises(v1.ContractError):
        v1.SenderEventV1.from_dict(r)
    r = d("sender_approved")
    r["data"]["policy_id"] = None  # explicit pa id
    with pytest.raises(v1.ContractError):
        v1.SenderEventV1.from_dict(r)
    deny = d("policy_denied")
    deny["data"]["requires_approval"] = False
    with pytest.raises(v1.ContractError):
        v1.SenderEventV1.from_dict(deny)
    r = d("sender_pending")
    r["data"]["norm_value"] = "ACME"  # jo kanonik
    with pytest.raises(v1.ContractError):
        v1.SenderEventV1.from_dict(r)


def test_default_policy_is_virtual_and_the_policy_entity_id_is_stable():
    assert v1.policy_entity_id("AL", "alphanumeric") == v1.policy_entity_id("AL", "alphanumeric")
    assert v1.policy_entity_id("AL", "alphanumeric") != v1.policy_entity_id("AL", "numeric")
    assert (
        v1.policy_entity_id("AL", "alphanumeric") == "2f2692d9-f84c-5c33-a0c4-19e3677f0578"
    )  # golden: s'ndryshon kurrë (identitet i entitetit)
    assert not any(
        c["events"][0]["data"].get("policy_source") == "default"
        and c["events"][0]["event_type"] == v1.EVENT_POLICY
        for c in CASES
    )  # politika default s'bartet kurrë si ngjarje


def test_snapshot_item_round_trip_and_wrong_list_detection():
    ev = d("sender_approved")
    item = {
        "event_type": ev["event_type"],
        "enterprise_id": ev["enterprise_id"],
        "entity": ev["entity"],
        "revision": ev["revision"],
        "data": ev["data"],
    }
    assert v1.SnapshotItemV1.from_dict(item).to_dict() == item
    for bad in ({**item, "seq": 1}, {k: v for k, v in item.items() if k != "revision"}):
        with pytest.raises(v1.ContractError):
            v1.SnapshotItemV1.from_dict(bad)
