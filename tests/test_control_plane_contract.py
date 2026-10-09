"""M7-b2 — kontrata `cp.v1` (Central → Enterprise): paketë leaf, serializim kanonik, golden, mapper."""

import ast
import dataclasses
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.central.models import SyncOutbox
from apps.central.services import enterprise_products as asg
from apps.central.services import enterprises as ent
from apps.central.services import products as prod
from apps.central.services import sync, sync_contract
from packages.contracts.control_plane import v1
from tests.golden.control_plane import regenerate as gen
from tests.test_central import ROOT, make_db  # noqa: F401
from tests.test_central_products import cdb, db  # noqa: F401  (fixtures)

GOLDEN = Path(__file__).parent / "golden" / "control_plane"
CASES = json.loads((GOLDEN / "cases.json").read_text())
PKG = ROOT / "packages" / "contracts"
E_ID = str(uuid.UUID(int=0x111))
EV_ID = str(uuid.UUID(int=1))
A_ID = str(uuid.UUID(int=0xB1))
P_ID = str(uuid.UUID(int=0xA1))
TS = datetime(2030, 1, 1, 12, tzinfo=UTC)


def row_of(case) -> SyncOutbox:
    r = case["row"]
    return SyncOutbox(
        seq=r["seq"], event_id=uuid.UUID(r["event_id"]), enterprise_id=uuid.UUID(r["enterprise_id"]),
        entity_type=r["entity_type"], entity_id=uuid.UUID(r["entity_id"]), revision=r["revision"],
        event_type=r["event_type"], payload=r["payload"], created_at=datetime.fromisoformat(r["created_at"]),
    )  # fmt: skip


def enterprise_event(**kw):
    base = dict(
        event_id=EV_ID, seq=1, type=v1.EVENT_ENTERPRISE_UPSERTED, enterprise_id=E_ID, entity_id=E_ID,
        revision=1, occurred_at=TS, data=v1.EnterpriseStateV1(E_ID, "Acme", "active"),
    )  # fmt: skip
    return v1.ControlPlaneEventV1(**{**base, **kw})


def assignment_event(**kw):
    state = v1.EnterpriseProductStateV1(A_ID, E_ID, P_ID, "sms", "sms", "active")
    base = dict(event_id=EV_ID, seq=1, type=v1.EVENT_ENTERPRISE_PRODUCT_UPSERTED,
                enterprise_id=E_ID, entity_id=A_ID, revision=1, occurred_at=TS, data=state)  # fmt: skip
    return v1.ControlPlaneEventV1(**{**base, **kw})


# --- paketa dhe kufijtë ----------------------------------------------------------------------------


def test_package_is_leaf_and_stdlib_only():
    files = sorted(p.relative_to(PKG).as_posix() for p in PKG.rglob("*.py"))
    assert files == [
        "control_plane/__init__.py",
        "control_plane/billing/__init__.py",
        "control_plane/billing/legacy_export_v1.py",
        "control_plane/billing/usage_v1.py",
        "control_plane/money/__init__.py",
        "control_plane/money/usage_v1.py",
        "control_plane/money/v1.py",
        "control_plane/pricing/__init__.py",
        "control_plane/pricing/v1.py",
        "control_plane/sender/__init__.py",
        "control_plane/sender/v1.py",
        "control_plane/v1.py",
    ]
    forbidden = {"app", "apps", "sqlalchemy", "fastapi", "pydantic", "httpx", "requests", "starlette",
                 "packages", "alembic"}  # fmt: skip
    for f in PKG.rglob("*.py"):
        for n in ast.walk(ast.parse(f.read_text())):
            mods = ([n.module] if isinstance(n, ast.ImportFrom) and n.module else
                    [a.name for a in n.names] if isinstance(n, ast.Import) else [])  # fmt: skip
            if isinstance(n, ast.ImportFrom):
                assert n.level == 0, "pa importe relative"
            for m in mods:
                root = m.split(".")[0]
                assert root in sys.stdlib_module_names and root not in forbidden, (f.name, m)


def test_webhook_and_control_plane_contracts_do_not_import_each_other():
    for f in (ROOT / "app" / "contracts").glob("*.py"):
        assert "control_plane" not in f.read_text() and "packages" not in f.read_text()
    assert sorted(p.name for p in (ROOT / "app" / "contracts").glob("*.py")) == [
        "__init__.py",
        "events.py",
        "signature.py",
    ]  # webhook V1: i pandryshuar
    assert "EventEnvelopeV1" not in (PKG / "control_plane" / "v1.py").read_text()


def test_names_are_versioned_and_event_types_are_the_existing_db_values():
    for cls in (v1.ControlPlaneEventV1, v1.EnterpriseStateV1, v1.EnterpriseProductStateV1):
        assert cls.__name__.endswith("V1")
    assert v1.SCHEMA == "cp.v1"
    assert (sync.EVENT_ENTERPRISE, sync.EVENT_ASSIGNMENT) == (
        "enterprise.upserted",
        "enterprise_product.upserted",
    )
    assert sync.ENTITY_ENTERPRISE == "enterprise" and sync.ENTITY_ASSIGNMENT == "enterprise_product"
    assert set(v1.ENTITY_BY_EVENT) == {
        "enterprise.upserted",
        "enterprise_product.upserted",
    }  # vetëm dy tipe


def test_mapper_uses_only_the_outbox_row_no_session_no_queries():
    src = (ROOT / "apps/central/services/sync_contract.py").read_text()
    roots = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.ImportFrom) and n.module:
            roots.add(n.module.split(".")[0])
        elif isinstance(n, ast.Import):
            roots |= {a.name.split(".")[0] for a in n.names}
    assert roots <= {"apps", "packages"}  # asnjë sqlalchemy/Session
    assert "select(" not in src and "db." not in src and ".get(" not in src


# --- pandryshueshmëria dhe forma e saktë ---------------------------------------------------------------


def test_event_and_states_are_frozen_slotted_dataclasses():
    e = enterprise_event()
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.seq = 2
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.data.name = "x"
    assert not hasattr(e, "__dict__") and not hasattr(e.data, "__dict__")
    assert e.schema == "cp.v1"


def test_exact_envelope_shape_and_state_shapes():
    d = enterprise_event().to_dict()
    assert list(d) == ["schema", "event_id", "seq", "type", "enterprise_id", "entity", "revision",
                       "occurred_at", "data"]  # fmt: skip
    assert d["entity"] == {"type": "enterprise", "id": E_ID} and d["data"] == {
        "id": E_ID,
        "name": "Acme",
        "status": "active",
    }
    a = assignment_event().to_dict()
    assert a["entity"] == {"type": "enterprise_product", "id": A_ID}
    assert a["data"] == {"assignment_id": A_ID, "enterprise_id": E_ID,
                         "product": {"id": P_ID, "code": "sms", "channel": "sms"}, "status": "active",
                         "rate_limit_per_min": None}  # fmt: skip
    assert "rate_limit_per_min" in json.dumps(a)  # M7-g: shtim additiv (null = default lokal)


# --- golden: bytes, JSON, round-trip ------------------------------------------------------------------------


def test_fixture_inventory():
    names = [c["name"] for c in CASES]
    assert len(names) == 15 and len(set(names)) == 15
    assert {p.name for p in GOLDEN.glob("*.body")} == {c["body_file"] for c in CASES}
    required = {"enterprise_active", "enterprise_suspended", "enterprise_non_ascii", "sms_active", "sms_suspended",
                "email_active", "email_suspended", "edge_high_seq", "edge_high_revision",
                "edge_microseconds_zero", "edge_microseconds_nonzero"}  # fmt: skip
    assert required <= set(names)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_outbox_row_to_contract_to_bytes_matches_golden_byte_for_byte(case):
    body = (GOLDEN / case["body_file"]).read_bytes()
    assert sync_contract.to_bytes(row_of(case)) == body  # bytes, jo dict
    assert json.loads(body) == case["parsed"]
    ev = v1.ControlPlaneEventV1.from_bytes(body)
    assert ev.to_bytes() == body  # round-trip identik
    assert sync_contract.to_event(row_of(case)) == ev
    r = case["row"]
    assert (ev.seq, ev.revision, ev.event_id) == (
        r["seq"],
        r["revision"],
        r["event_id"],
    )  # saktësisht


def test_golden_fixtures_are_reproduced_by_the_generator_without_writing():
    """Rerun në memorie = skedarët statikë (asnjë drift; regenerate.py mbetet manual-only)."""
    rebuilt = list(gen.cases())
    assert [n for n, _ in rebuilt] == [c["name"] for c in CASES]
    for (name, row), case in zip(rebuilt, CASES, strict=True):
        assert (
            sync_contract.to_bytes(gen.to_outbox(row)) == (GOLDEN / case["body_file"]).read_bytes()
        ), name


def test_serialization_invariants():
    b = enterprise_event(data=v1.EnterpriseStateV1(E_ID, "Shoqëria 你好 😀", "active")).to_bytes()
    assert b.decode("ascii") and all(x < 0x80 for x in b)  # ensure_ascii
    assert b"\\u00eb" in b and b"\\ud83d\\ude00" in b
    top = json.loads(b, object_pairs_hook=lambda p: [k for k, _ in p])
    assert top == sorted(top)  # sort_keys
    assert (
        b" " not in b.replace(b"\\u", b"").split(b'"name"')[0] and b'": ' not in b
    )  # separatorë kompakt
    assert json.loads(b)["data"]["name"] == "Shoqëria 你好 😀"


def test_seq_beyond_js_safe_integer_and_int64_max_are_preserved_exactly():
    for n in (2**53 + 1, v1.INT64_MAX):
        e = enterprise_event(seq=n, revision=n)
        assert (
            f'"seq":{n}'.encode() in e.to_bytes()
            and v1.ControlPlaneEventV1.from_bytes(e.to_bytes()).seq == n
        )


def test_occurred_at_is_fixed_width_utc_and_informational_only():
    zero = enterprise_event(occurred_at=datetime(2030, 1, 1, 12, tzinfo=UTC)).to_dict()[
        "occurred_at"
    ]
    micro = enterprise_event(
        occurred_at=datetime(2030, 1, 1, 12, 0, 0, 123456, tzinfo=UTC)
    ).to_dict()["occurred_at"]
    naive = enterprise_event(occurred_at=datetime(2030, 1, 1, 12)).to_dict()["occurred_at"]
    from datetime import timedelta, timezone

    plus2 = enterprise_event(
        occurred_at=datetime(2030, 1, 1, 14, tzinfo=timezone(timedelta(hours=2)))
    ).to_dict()["occurred_at"]
    assert zero == naive == plus2 == "2030-01-01T12:00:00.000000+00:00"
    assert micro == "2030-01-01T12:00:00.123456+00:00" and len(zero) == len(micro)
    # asnjë përdorim i occurred_at për rendin: dy ngjarje me ora të kundërt, revision vendos
    older_clock_higher_rev = enterprise_event(
        revision=5, occurred_at=datetime(2001, 1, 1, tzinfo=UTC)
    )
    assert older_clock_higher_rev.revision > enterprise_event(revision=4).revision


# --- validim strikt -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, True, "1", 1.5, None, 2**63])
def test_invalid_seq_and_revision(bad):
    with pytest.raises(v1.ContractError):
        enterprise_event(seq=bad)
    with pytest.raises(v1.ContractError):
        enterprise_event(revision=bad)


@pytest.mark.parametrize(
    "bad", ["", "not-a-uuid", A_ID.upper(), A_ID.replace("-", ""), "{" + A_ID + "}", None, 5]
)
def test_malformed_or_non_canonical_uuids(bad):
    for field in ("event_id", "enterprise_id", "entity_id"):
        with pytest.raises(v1.ContractError):
            enterprise_event(**{field: bad})
    with pytest.raises(v1.ContractError):
        v1.EnterpriseStateV1(bad, "Acme", "active")
    with pytest.raises(v1.ContractError):
        v1.EnterpriseProductStateV1(A_ID, E_ID, bad, "sms", "sms", "active")


def test_schema_and_event_type_are_validated():
    with pytest.raises(v1.UnsupportedSchemaError):
        enterprise_event(schema="cp.v2")
    d = enterprise_event().to_dict()
    for s in ("cp.v2", "cp.v1 ", "CP.V1", None):
        with pytest.raises(v1.UnsupportedSchemaError):
            v1.ControlPlaneEventV1.from_dict({**d, "schema": s})
    with pytest.raises(v1.UnknownEventTypeError):
        enterprise_event(type="enterprise.deleted")
    with pytest.raises(v1.UnknownEventTypeError):
        v1.ControlPlaneEventV1.from_dict({**d, "type": "enterprise.renamed"})
    assert issubclass(v1.UnsupportedSchemaError, v1.ContractError) and issubclass(
        v1.ContractError, ValueError
    )


def test_channel_status_name_and_code_validation():
    for bad in ("voice", "SMS", "", None, "sms "):
        with pytest.raises(v1.ContractError):
            v1.EnterpriseProductStateV1(A_ID, E_ID, P_ID, "sms", bad, "active")
    for bad in ("pending", "retired", "", None):
        with pytest.raises(v1.ContractError):
            v1.EnterpriseProductStateV1(A_ID, E_ID, P_ID, "sms", "sms", bad)
        with pytest.raises(v1.ContractError):
            v1.EnterpriseStateV1(E_ID, "Acme", bad)
    for bad in ("", "   ", "x" * 201, "a\nb", "a\x00b", None, 7):
        with pytest.raises(v1.ContractError):
            v1.EnterpriseStateV1(E_ID, bad, "active")
    for bad in ("SMS", "1sms", "s", "sms-x", "x" * 33, "", None):
        with pytest.raises(v1.ContractError):
            v1.EnterpriseProductStateV1(A_ID, E_ID, P_ID, bad, "sms", "active")
    assert v1.EnterpriseStateV1(E_ID, "x" * 200, "suspended").status == "suspended"


def test_payload_shape_and_identity_consistency():
    with pytest.raises(v1.ContractError):  # data i gabuar për llojin
        enterprise_event(data=assignment_event().data)
    with pytest.raises(v1.ContractError):
        assignment_event(data=enterprise_event().data)
    with pytest.raises(v1.ContractError):  # entity.id ≠ data.id
        enterprise_event(entity_id=str(uuid.UUID(int=9)))
    with pytest.raises(v1.ContractError):  # enterprise_id ≠ data.id
        enterprise_event(enterprise_id=str(uuid.UUID(int=9)))
    with pytest.raises(v1.ContractError):  # assignment: enterprise_id ≠ data.enterprise_id
        assignment_event(enterprise_id=str(uuid.UUID(int=9)))
    with pytest.raises(v1.ContractError):  # assignment: entity.id ≠ data.assignment_id
        assignment_event(entity_id=str(uuid.UUID(int=9)))


def test_from_dict_rejects_envelope_problems_and_ignores_unknown_data_fields():
    d = enterprise_event().to_dict()
    for broken in ({**d, "extra": 1}, {k: v for k, v in d.items() if k != "seq"},
                   {**d, "entity": {"type": "enterprise"}}, {**d, "entity": {"type": "enterprise_product", "id": E_ID}},
                   {**d, "entity": {"type": "enterprise", "id": E_ID, "x": 1}}, {**d, "entity": "x"},
                   {**d, "occurred_at": "2030-01-01T12:00:00+00:00"}, {**d, "occurred_at": "2030-01-01T12:00:00.000000Z"},
                   {**d, "data": []}, {**d, "data": {"id": E_ID, "name": "Acme"}}, [], "x", None):  # fmt: skip
        with pytest.raises(v1.ContractError):
            v1.ControlPlaneEventV1.from_dict(broken)
    with_extra = {**d, "data": {**d["data"], "tier": "gold"}}  # fushë shtesë në `data`: injorohet
    assert v1.ControlPlaneEventV1.from_dict(with_extra) == enterprise_event()
    a = assignment_event().to_dict()
    a["data"]["product"]["tier"] = "x"
    a["data"]["note"] = "x"
    assert v1.ControlPlaneEventV1.from_dict(a) == assignment_event()
    with pytest.raises(v1.ContractError):
        v1.ControlPlaneEventV1.from_bytes(b"\xff\xfe")
    with pytest.raises(v1.ContractError):
        v1.ControlPlaneEventV1.from_bytes(b"{not json")


def test_mapper_rejects_inconsistent_or_malformed_rows():
    base = row_of(CASES[0])
    for bad in (SyncOutbox(**{**_cols(base), "event_type": "enterprise.deleted"}),
                SyncOutbox(**{**_cols(base), "entity_type": "enterprise_product"}),
                SyncOutbox(**{**_cols(base), "payload": {"name": "x"}}),
                SyncOutbox(**{**_cols(base), "payload": {**base.payload, "status": "retired"}}),
                SyncOutbox(**{**_cols(base), "seq": 0})):  # fmt: skip
        with pytest.raises(v1.ContractError):
            sync_contract.to_event(bad)


def _cols(r: SyncOutbox) -> dict:
    return {c: getattr(r, c) for c in ("seq", "event_id", "enterprise_id", "entity_type", "entity_id",
                                       "revision", "event_type", "payload", "created_at")}  # fmt: skip


# --- rreshta realë → kontratë; riprodhim historik ------------------------------------------------------------


def outbox_rows(db, entity_id):
    from sqlalchemy import select

    return list(
        db.scalars(
            select(SyncOutbox).where(SyncOutbox.entity_id == entity_id).order_by(SyncOutbox.seq)
        )
    )


def test_real_service_rows_map_and_parse_back(db):
    e = ent.create(db, "Acme", now=TS)
    p = prod.create(db, "email", "Email", "email")
    ep, _ = asg.assign_product(db, e.id, p.id)
    asg.suspend_assignment(db, e.id, ep.id)
    db.commit()
    events = [sync_contract.to_event(r) for r in outbox_rows(db, e.id) + outbox_rows(db, ep.id)]
    assert [(x.type, x.revision) for x in events] == [("enterprise.upserted", 1), ("enterprise_product.upserted", 1),
                                                       ("enterprise_product.upserted", 2)]  # fmt: skip
    assert events[1].data == v1.EnterpriseProductStateV1(
        str(ep.id), str(e.id), str(p.id), "email", "email", "active"
    )
    assert events[2].data.status == "suspended" and events[0].data.name == "Acme"
    for r in outbox_rows(db, ep.id):
        raw = sync_contract.to_bytes(r)
        assert (
            v1.ControlPlaneEventV1.from_bytes(raw).to_bytes() == raw
            and sync_contract.to_bytes(r) == raw
        )  # stabil
    ids = [str(r.event_id) for r in outbox_rows(db, ep.id)]
    assert [x.event_id for x in events[1:]] == ids  # event_id vjen nga rreshti, jo i ri


def test_historical_replay_uses_the_frozen_outbox_snapshot_not_the_current_state(db):
    e = ent.create(db, "First", now=TS)  # revision 1
    ent.rename(db, e.id, "Second")  # revision 2, active
    p = prod.create(db, "sms", "SMS", "sms")
    ep, _ = asg.assign_product(db, e.id, p.id)  # revision 1 active
    asg.suspend_assignment(db, e.id, ep.id)  # revision 2 suspended
    ent.suspend(db, e.id)  # revision 3, suspended
    db.commit()
    r2 = outbox_rows(db, e.id)[1]
    ev2 = sync_contract.to_event(r2)
    assert ev2.revision == 2 and ev2.data.status == "active" and ev2.data.name == "Second"
    from apps.central.models import Enterprise, EnterpriseProduct

    now_state = db.get(Enterprise, e.id)
    assert (now_state.status, now_state.name, now_state.revision) == (
        "suspended",
        "Second",
        3,
    )  # tani ndryshe
    a1 = outbox_rows(db, ep.id)[0]
    assert sync_contract.to_event(a1).data.status == "active"
    assert db.get(EnterpriseProduct, ep.id).status == "suspended"
    db.expunge_all()  # rreshti i shkëputur nga sesioni: mapper-i prapë funksionon (pa DB)
    assert sync_contract.to_event(r2).data.status == "active" and sync_contract.to_bytes(
        a1
    ) == sync_contract.to_bytes(a1)
    db.rollback()
    prod_after = prod.update(db, p.id, name="Renamed")[0]
    assert prod_after.name == "Renamed" and sync_contract.to_event(a1).data.product_code == "sms"
