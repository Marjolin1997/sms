"""M3-d3 — EventEnvelopeV1 + serializer eksplicit: bytes identike me `envelope()` të d1 dhe me golden."""

import ast
import dataclasses
import inspect
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.contracts.events import EventEnvelopeV1, _as_utc
from app.core.db import SessionLocal
from app.core.timeutil import as_utc
from app.models.events import Event
from app.services import events, webhooks
from tests.test_webhook_golden import BY_NAME, CASES, DB_CASES, expected_body, make_event

ROOT = Path(__file__).resolve().parents[1] / "app"
CREATED = datetime(2030, 1, 1, 12, tzinfo=UTC)


def legacy_envelope(ev: Event) -> bytes:
    """Kopje VERBATIM e serializer-it para d3 (referencë dual-path; mos e ndrysho)."""
    return json.dumps(
        {
            "id": f"evt_{ev.id}", "type": ev.type, "created_at": as_utc(ev.created_at).isoformat(),
            "data": {"resource_type": ev.resource_type, "resource_id": ev.resource_id,
                     **(ev.data or {})},
        },
        separators=(",", ":"), sort_keys=True,
    ).encode()  # fmt: skip


def env(data=None, created=CREATED, id_="evt_1", type_="message.sent"):
    return EventEnvelopeV1(
        id=id_, type=type_, created_at=created, data={} if data is None else data
    )


# --- përkufizimi ---------------------------------------------------------------------


def test_envelope_is_frozen_slotted_with_exactly_the_public_fields():
    e = env({"a": 1})
    assert [f.name for f in dataclasses.fields(e)] == ["id", "type", "created_at", "data"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.id = "x"
    with pytest.raises((AttributeError, TypeError)):
        e.extra = 1  # slots: pa atribute të tjera
    assert not hasattr(e, "__dict__")
    assert list(e.to_dict()) == ["id", "type", "created_at", "data"]


def test_to_dict_copies_data_and_normalizes_created_at():
    d = {"k": 1}
    out = env(d).to_dict()
    assert out["created_at"] == "2030-01-01T12:00:00+00:00" and out["data"] == d
    assert out["data"] is not d  # kopje e cekët


# --- barazia me golden dhe me rrugën legacy --------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_new_serializer_equals_legacy_equals_golden_bytes(case):
    ev = make_event(case)
    new = events.to_envelope_v1(ev).to_bytes()
    assert new == legacy_envelope(ev) == webhooks.envelope(ev) == expected_body(case)
    assert json.loads(new) == case["parsed"]


def test_all_33_fixtures_are_present():
    assert len(CASES) == 33


@pytest.mark.parametrize("case", DB_CASES, ids=lambda c: c["name"])
def test_db_round_trip_through_mapper_matches_golden(db, case):
    db.add(make_event(case))
    db.commit()
    db.close()
    with SessionLocal() as fresh:
        ev = fresh.get(Event, case["event"]["id"])
        assert events.to_envelope_v1(ev).to_bytes() == expected_body(case)
        assert webhooks.envelope(ev) == expected_body(case) == legacy_envelope(ev)


def test_legacy_envelope_returns_bytes_and_call_sites_are_unchanged():
    out = webhooks.envelope(make_event(BY_NAME["message.sent"]))
    assert type(out) is bytes
    assert "envelope(ev)" in (ROOT / "services" / "webhooks.py").read_text()


# --- datetime -----------------------------------------------------------------------------


def test_created_at_semantics():
    iso = "2030-01-01T12:00:00+00:00"
    assert env(created=datetime(2030, 1, 1, 12)).to_dict()["created_at"] == iso  # naive = UTC
    assert env(created=CREATED).to_dict()["created_at"] == iso  # +00:00, jo "Z"
    plus2 = datetime(2030, 1, 1, 14, tzinfo=timezone(timedelta(hours=2)))
    assert env(created=plus2).to_dict()["created_at"] == iso
    minus5 = datetime(2030, 1, 1, 7, tzinfo=timezone(timedelta(hours=-5)))
    assert env(created=minus5).to_dict()["created_at"] == iso
    micro = datetime(2030, 1, 1, 12, 0, 0, 123456, tzinfo=UTC)
    assert env(created=micro).to_dict()["created_at"] == "2030-01-01T12:00:00.123456+00:00"
    assert "." not in env(created=micro.replace(microsecond=0)).to_dict()["created_at"]
    assert "Z" not in env().to_bytes().decode()


@pytest.mark.parametrize(
    "dt",
    [datetime(2030, 1, 1, 12), CREATED, datetime(2030, 1, 1, 12, 0, 0, 5),
     datetime(2030, 6, 1, 14, tzinfo=timezone(timedelta(hours=2)))],
)  # fmt: skip
def test_local_as_utc_primitive_equals_core_timeutil(dt):
    assert _as_utc(dt) == as_utc(dt) and _as_utc(dt).tzinfo == as_utc(dt).tzinfo


# --- serializimi --------------------------------------------------------------------------


def test_serialization_invariants():
    e = env({"z": {"y": [3, 1, {"b": None, "a": True}], "x": {}}, "Beta": 1, "alpha": "ë😀"})
    b = e.to_bytes()
    assert b == (
        b'{"created_at":"2030-01-01T12:00:00+00:00","data":{"Beta":1,'
        b'"alpha":"\\u00eb\\ud83d\\ude00","z":{"x":{},"y":[3,1,{"a":true,"b":null}]}},'
        b'"id":"evt_1","type":"message.sent"}'
    )
    assert b.decode("ascii") and all(x < 0x80 for x in b)
    outside = re.sub(rb'"(?:[^"\\]|\\.)*"', b"", b)
    assert b" " not in outside and b"\n" not in outside
    assert json.loads(b)["data"]["alpha"] == "ë😀"


def test_none_and_empty_data_are_equal_at_the_mapper_level():
    def ev(data):
        return Event(id=1, type="webhook.ping", resource_type="endpoint", resource_id="1",
                     data=data, created_at=CREATED)  # fmt: skip

    assert events.to_envelope_v1(ev(None)).to_bytes() == events.to_envelope_v1(ev({})).to_bytes()
    assert events.to_envelope_v1(ev(None)).data == {"resource_type": "endpoint", "resource_id": "1"}
    assert b'"data":null' not in events.to_envelope_v1(ev(None)).to_bytes()


def test_none_values_and_list_order_preserved():
    b = env({"x": None, "l": [3, 1, 2]}).to_bytes()
    assert b'"x":null' in b and b'"l":[3,1,2]' in b


def test_resource_fields_overwrite_is_frozen_in_the_mapper():
    ev = make_event(BY_NAME["edge.overwrite_resource_fields"])
    out = events.to_envelope_v1(ev)
    assert out.data["resource_type"] == "EVIL" and out.data["resource_id"] == "x"
    assert ev.resource_type == "message" and ev.resource_id == "m1"  # ORM i pandryshuar


def test_decimal_strings_pass_verbatim_and_no_hidden_encoder():
    short = events.to_envelope_v1(make_event(BY_NAME["edge.decimal_scale_short"])).to_bytes()
    long_ = events.to_envelope_v1(make_event(BY_NAME["edge.decimal_scale_long"])).to_bytes()
    assert b'"available":"5"' in short and b'"available":"5.0000"' in long_
    with pytest.raises(TypeError):  # sjellja e json.dumps e ruajtur: pa `default=`
        env({"v": Decimal("5")}).to_bytes()


def test_id_and_type_pass_through_as_strings():
    ev = make_event(BY_NAME["wallet.low_balance"])
    e = events.to_envelope_v1(ev)
    assert e.id == f"evt_{ev.id}" and e.type == "wallet.low_balance" and type(e.id) is str


# --- kufijtë ---------------------------------------------------------------------------------

FORBIDDEN_ROOTS = {"app", "sqlalchemy", "fastapi", "pydantic", "httpx", "starlette"}
ALLOWED = {"json", "dataclasses", "datetime", "typing", "hashlib", "hmac", "time"}


def test_contracts_are_stdlib_only_no_orm_no_core():
    files = sorted((ROOT / "contracts").glob("*.py"))
    assert {f.name for f in files} == {"__init__.py", "events.py", "signature.py"}
    for f in files:
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.ImportFrom):
                assert n.level == 0
                roots = [(n.module or "").split(".")[0]]
            elif isinstance(n, ast.Import):
                roots = [a.name.split(".")[0] for a in n.names]
            else:
                continue
            for root in roots:
                assert root not in FORBIDDEN_ROOTS and root in ALLOWED, (f.name, root)


def test_internal_event_models_are_not_public_events():
    from app.models.admin import AuditLog
    from app.models.email import EmailEvent
    from app.models.sending import DlrReceipt, MessageEvent

    for m in (MessageEvent, EmailEvent, AuditLog, DlrReceipt):
        assert not issubclass(m, Event)
    assert inspect.signature(events.to_envelope_v1).parameters["ev"].annotation is Event


# --- katalogu publik V1 (d4) -------------------------------------------------------------

EXPECTED_V1 = {
    "message.sent", "message.delivered", "message.failed", "message.received",
    "email.sent", "email.delivered", "email.bounced", "email.complained", "email.failed",
    "campaign.running", "campaign.paused", "campaign.completed", "campaign.cancelled",
    "consent.opted_out", "consent.opted_in", "webhook.ping",
    "invoice.issued", "invoice.paid", "payment.succeeded", "payment.failed",
    "wallet.low_balance",
}  # fmt: skip


def test_public_event_types_v1_is_the_exact_immutable_catalog():
    from app.contracts.events import PUBLIC_EVENT_TYPES_V1

    assert isinstance(PUBLIC_EVENT_TYPES_V1, frozenset)
    assert PUBLIC_EVENT_TYPES_V1 == EXPECTED_V1 and len(PUBLIC_EVENT_TYPES_V1) == 21
    with pytest.raises(AttributeError):
        PUBLIC_EVENT_TYPES_V1.add("x")


def test_known_types_is_the_same_object_as_the_contract_catalog():
    from app.contracts.events import PUBLIC_EVENT_TYPES_V1

    assert events.KNOWN_TYPES is PUBLIC_EVENT_TYPES_V1
    assert events.valid_filter("message.*") and events.valid_filter("*")
    assert events.valid_filter("wallet.low_balance") and not events.valid_filter("nope.x")
    assert events.matches(["message.*"], "message.received")


def test_canonical_fixtures_map_one_to_one_to_the_catalog():
    from collections import Counter

    from app.contracts.events import PUBLIC_EVENT_TYPES_V1

    canon = [c for c in CASES if not c["name"].startswith("edge.")]
    counts = Counter(c["event"]["type"] for c in canon)
    assert set(counts) == PUBLIC_EVENT_TYPES_V1  # asnjë i panjohur, asnjë mungon
    assert {t for t, n in counts.items() if n > 1} == {"message.failed"}  # null + vlerë
    assert all(c["event"]["type"] in PUBLIC_EVENT_TYPES_V1 for c in CASES)


def test_emit_rejects_types_outside_the_catalog(db):
    with pytest.raises(ValueError):
        events.emit(db, "c1", "message.queued", "message", "x")  # status i brendshëm, jo publik
    with pytest.raises(ValueError):
        events.emit(db, "c1", "audit.created", "audit", "x")
