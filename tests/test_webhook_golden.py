"""M3-d1 — golden të kontratës së webhook-ut (sjellja e sotme, e ngrirë para çdo extraction).

Fixture-t statike (`tests/golden/webhooks/`) janë gjeneruar nga kodi i prodhimit dhe verifikuar
të pavarur me `openssl dgst -sha256 -hmac`. Testet thërrasin `webhooks.envelope/sign/deliver_next`
të prodhimit dhe krahasojnë BYTES me fixture-n; nuk rindërtojnë serializer-in këtu.

Kjo ngrin sjelljen aktuale, jo e miraton: rreziqet e karakterizuara (p.sh. mbishkrimi i `resource_*`
nga `ev.data`) mbeten të hapura për versionim ose vendim të veçantë.
"""

import ast
import enum
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.core import crypto
from app.core.db import SessionLocal
from app.models.campaigns import CampaignStatus
from app.models.email import EmailStatus
from app.models.events import DeliveryStatus, Event, WebhookDelivery
from app.models.sending import MessageStatus
from app.services import events, net_guard, webhooks
from app.services.webhook_queue import queue

GOLDEN = Path(__file__).parent / "golden" / "webhooks"
CASES = json.loads((GOLDEN / "cases.json").read_text())
BY_NAME = {c["name"]: c for c in CASES}
DB_CASES = [c for c in CASES if c["db"]]
SECRET = "whsec_test_contract_v1"
TS = 1700000000
NOW = datetime.fromtimestamp(TS, UTC)
ROOT = Path(__file__).resolve().parents[1] / "app"
IS_PG = os.environ.get("SMS_TEST_DATABASE_URL", "").startswith("postgresql")


def expected_body(case) -> bytes:
    return (GOLDEN / case["body_file"]).read_bytes()


def make_event(case) -> Event:
    e = case["event"]
    return Event(
        id=e["id"], owner_ref="c1", type=e["type"], resource_type=e["resource_type"],
        resource_id=e["resource_id"], data=e["data"],
        created_at=datetime.fromisoformat(e["created_at"]),
    )  # fmt: skip


@pytest.fixture(autouse=True)
def public_dns():
    old = net_guard.get_resolver()
    net_guard.set_resolver(lambda host: ["93.184.216.34"])
    yield
    net_guard.set_resolver(old)
    webhooks.set_client(None)


class Capture:
    def __init__(self, statuses=(200,)):
        self.requests, self._statuses = [], list(statuses)

    def install(self):
        def handler(request: httpx.Request):
            self.requests.append(request)
            s = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
            return httpx.Response(s)

        webhooks.set_client(httpx.Client(transport=httpx.MockTransport(handler)))
        return self


def seed(case, *, fixed_secret=True):
    """Endpoint + Event (id eksplicit) + delivery; commit; sesioni mbyllet (rrugë e vërtetë DB)."""
    with SessionLocal() as db:
        ep, _ = webhooks.create_endpoint(db, "c1", "https://hooks.example.com/sms", None)
        if fixed_secret:
            ep.secret_enc = crypto.encrypt(SECRET.encode())
        db.add(make_event(case))
        db.flush()
        d = WebhookDelivery(endpoint_id=ep.id, event_id=case["event"]["id"], next_attempt_at=NOW)
        queue.publish(db, [d])
        db.commit()
        return ep.id, d.id


# --- fixture-t: bytes, JSON, nënshkrim -----------------------------------------------


def test_fixture_inventory():
    assert len(CASES) == 33 and len(BY_NAME) == 33
    assert {p.name for p in GOLDEN.glob("*.body")} == {c["body_file"] for c in CASES}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_envelope_bytes_match_golden_byte_for_byte(case):
    body = webhooks.envelope(make_event(case))
    assert isinstance(body, bytes)
    assert body == expected_body(case)  # bytes, jo vetëm dict
    assert json.loads(body) == case["parsed"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_signature_matches_golden(case):
    sig = case["signature"]
    assert sig["secret"] == SECRET and sig["timestamp"] == TS
    assert webhooks.sign(SECRET, TS, expected_body(case)) == sig["header"]
    assert re.fullmatch(r"t=1700000000,v1=[0-9a-f]{64}", sig["header"])


@pytest.mark.parametrize("case", DB_CASES, ids=lambda c: c["name"])
def test_full_delivery_path_headers_and_bytes_after_db_round_trip(db, case):
    """DB → sesion i ri → deliver_next → kërkesa e kapur: bytes + headers = golden."""
    seed(case)
    cap = Capture().install()
    with SessionLocal() as fresh:  # jo identity map: `data`/`created_at` vijnë nga kolona JSON
        d = webhooks.deliver_next(fresh, NOW)
        assert d.status == DeliveryStatus.SUCCEEDED
    req = cap.requests[0]
    assert req.content == expected_body(case)
    got = {k: req.headers[k] for k in case["headers"]}
    assert got == case["headers"]
    assert case["headers"]["x-sms-event-id"] == f"evt_{case['event']['id']}"
    assert case["headers"]["x-sms-delivery-id"] == "1"
    assert case["headers"]["content-type"] == "application/json"
    assert case["headers"]["user-agent"] == "sms-platform-webhooks/1"
    assert case["headers"]["x-sms-signature"] == case["signature"]["header"]


# --- invariantët e serializimit ---------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_serialization_invariants_hold_for_every_fixture(case):
    body = expected_body(case)
    text = body.decode("ascii")  # ensure_ascii=True → bytes thjesht ASCII
    assert " " not in re.sub(r'"(?:[^"\\]|\\.)*"', "", text)  # pa hapësira jashtë stringjeve
    assert "\n" not in text and ": " not in re.sub(r'"(?:[^"\\]|\\.)*"', "", text)
    top = json.loads(body, object_pairs_hook=lambda p: [k for k, _ in p])
    assert top == sorted(top)  # çelësat top-level alfabetikë
    assert top == ["created_at", "data", "id", "type"]

    def sorted_everywhere(node):
        if isinstance(node, dict):
            assert list(node) == sorted(node)
            [sorted_everywhere(v) for v in node.values()]
        elif isinstance(node, list):
            [sorted_everywhere(v) for v in node]

    sorted_everywhere(json.loads(body))
    assert re.fullmatch(r"evt_\d+", json.loads(body)["id"])
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{6})?\+00:00", json.loads(body)["created_at"]
    )


def test_non_ascii_is_escaped_not_utf8():
    body = expected_body(BY_NAME["message.received.non_ascii"])
    assert b"\\u00eb" in body and b"\\ud83d\\ude00" in body  # ë dhe emoji si surrogate pair
    assert all(b < 0x80 for b in body)
    assert json.loads(body)["data"]["text"] == 'Përshëndetje 你好 😀 "ok" \\ \n'


def test_microseconds_are_emitted_only_when_nonzero():
    zero = json.loads(expected_body(BY_NAME["edge.microseconds_zero"]))["created_at"]
    micro = json.loads(expected_body(BY_NAME["edge.microseconds_nonzero"]))["created_at"]
    assert zero == "2030-01-01T12:00:00+00:00"
    assert micro == "2030-01-01T12:00:00.123456+00:00"  # gjatësia e fushës VARION (rrezik i njohur)


def test_naive_created_at_is_treated_as_utc_and_offsets_are_converted():
    ref = expected_body(BY_NAME["edge.microseconds_zero"])
    for name in ("edge.naive_created_at", "edge.offset_created_at"):
        got = webhooks.envelope(make_event(BY_NAME[name]))
        assert got == expected_body(BY_NAME[name])
        assert json.loads(got)["created_at"] == "2030-01-01T12:00:00+00:00"
    # fixture-t kanë id të ndryshme, përndryshe bytes janë identike me rastin aware
    assert (
        json.loads(ref)["created_at"]
        == json.loads(expected_body(BY_NAME["edge.naive_created_at"]))["created_at"]
    )


def test_null_data_and_empty_data():
    none = json.loads(expected_body(BY_NAME["edge.data_none"]))["data"]
    empty = json.loads(expected_body(BY_NAME["edge.data_empty"]))["data"]
    assert none == empty == {"resource_id": "1", "resource_type": "endpoint"}
    nulls = json.loads(expected_body(BY_NAME["message.failed.error_code_null"]))["data"]
    assert "error_code" in nulls and nulls["error_code"] is None  # None → null, çelësi mbetet
    assert b'"error_code":null' in expected_body(BY_NAME["message.failed.error_code_null"])
    assert b"error_code" not in expected_body(BY_NAME["message.sent"])  # mungon në sent


def test_decimal_strings_are_passed_through_verbatim():
    short = json.loads(expected_body(BY_NAME["edge.decimal_scale_short"]))["data"]
    long_ = json.loads(expected_body(BY_NAME["edge.decimal_scale_long"]))["data"]
    assert (short["available"], long_["available"]) == ("5", "5.0000")  # shkalla = e thirrësit
    assert short["threshold"] == "5" and long_["threshold"] == "5.000000"


def test_nested_structure_is_sorted_recursively_and_data_id_type_do_not_collide():
    body = expected_body(BY_NAME["edge.nested_structure"])
    assert b'"z":{"x":{},"y":[3,1,{"a":true,"b":null}]}' in body  # listat ruajnë renditjen
    parsed = json.loads(body)
    assert parsed["id"] == "evt_131" and parsed["type"] == "message.sent"
    assert parsed["data"]["id"] == "inner" and parsed["data"]["type"] == "inner"


def test_key_ordering_is_codepoint_alphabetical_not_insertion_order():
    body = expected_body(BY_NAME["edge.key_ordering"])
    assert list(json.loads(body)["data"]) == [
        "Beta", "alpha", "message_id", "resource_id", "resource_type", "segments", "status", "zeta",
    ]  # fmt: skip


def test_characterization_event_data_overwrites_resource_fields():
    """RREZIK I HAPUR (nuk miratohet): `**ev.data` vjen PAS resource_*, ndaj data fiton.
    Asnjë producer sot nuk e bën; ndryshimi kërkon versionim ose vendim të veçantë."""
    case = BY_NAME["edge.overwrite_resource_fields"]
    assert case["event"]["resource_type"] == "message" and case["event"]["resource_id"] == "m1"
    data = json.loads(webhooks.envelope(make_event(case)))["data"]
    assert data["resource_type"] == "EVIL" and data["resource_id"] == "x"


# --- nënshkrimi ---------------------------------------------------------------------------


def test_signature_invariants():
    body = expected_body(BY_NAME["message.sent"])
    sig = webhooks.sign(SECRET, TS, body)
    assert sig == webhooks.sign(SECRET, TS, body)  # deterministik
    flipped = bytes([body[0] ^ 1]) + body[1:]
    assert webhooks.sign(SECRET, TS, flipped) != sig  # 1 bit në body
    assert webhooks.sign(SECRET, TS, body + b" ") != sig
    assert webhooks.sign(SECRET, TS + 1, body) != sig  # ts
    assert webhooks.sign("whsec_other", TS, body) != sig  # secret
    assert sig.split(",")[0] == f"t={TS}" and sig.split(",")[1].startswith("v1=")
    assert webhooks.verify_signature(SECRET, sig, body, now=TS + 299)
    assert not webhooks.verify_signature(SECRET, sig, body, now=TS + 301)  # tolerancë 300s
    assert not webhooks.verify_signature(SECRET, sig.upper(), body, now=TS)  # hex lowercase


def test_secret_is_signed_as_the_full_whsec_string():
    body = expected_body(BY_NAME["message.sent"])
    assert webhooks.sign("test_contract_v1", TS, body) != webhooks.sign(SECRET, TS, body)


def test_retry_keeps_body_and_delivery_id_but_changes_signature_timestamp(db):
    case = BY_NAME["message.sent"]
    _, delivery_id = seed(case)
    cap = Capture(statuses=(500, 200)).install()
    with SessionLocal() as s:
        assert webhooks.deliver_next(s, NOW).status == DeliveryStatus.PENDING
    later = NOW + timedelta(hours=1)
    with SessionLocal() as s:
        assert webhooks.deliver_next(s, later).status == DeliveryStatus.SUCCEEDED
    a, b = cap.requests
    assert a.content == b.content == expected_body(case)  # bytes identike
    assert a.headers["x-sms-delivery-id"] == b.headers["x-sms-delivery-id"] == str(delivery_id)
    assert a.headers["x-sms-event-id"] == b.headers["x-sms-event-id"] == "evt_100"
    assert a.headers["x-sms-signature"] == case["headers"]["x-sms-signature"]
    assert b.headers["x-sms-signature"] == webhooks.sign(SECRET, int(later.timestamp()), a.content)
    assert a.headers["x-sms-signature"] != b.headers["x-sms-signature"]


def test_replay_keeps_delivery_id_and_body(db):
    case = BY_NAME["email.bounced"]
    _, delivery_id = seed(case)
    cap = Capture().install()
    with SessionLocal() as s:
        webhooks.deliver_next(s, NOW)
    with SessionLocal() as s:
        d = webhooks.redeliver(s, "c1", delivery_id)
        s.commit()
        assert d.id == delivery_id
    with SessionLocal() as s:
        # redeliver vë next_attempt_at = ora reale, ndaj dërgojmë pas saj
        assert webhooks.deliver_next(s, datetime.now(UTC) + timedelta(minutes=1)) is not None
    a, b = cap.requests
    assert a.content == b.content == expected_body(case)
    assert a.headers["x-sms-delivery-id"] == b.headers["x-sms-delivery-id"] == str(delivery_id)
    assert a.headers["x-sms-signature"] != b.headers["x-sms-signature"]


def test_rotate_secret_invalidates_old_signatures_immediately_no_grace(db):
    """Dokumentim i sjelljes (vëzhgim sigurie, pa ndryshim): rotate_secret s'ka dritare grace."""
    ep_id, _ = seed(BY_NAME["message.sent"])
    with SessionLocal() as s:
        _, new = webhooks.rotate_secret(s, "c1", ep_id)
        s.commit()
    assert new != SECRET and new.startswith("whsec_")
    cap = Capture().install()
    with SessionLocal() as s:
        webhooks.deliver_next(s, NOW)
    sig = cap.requests[0].headers["x-sms-signature"]
    body = cap.requests[0].content
    assert webhooks.verify_signature(new, sig, body, now=TS)
    assert not webhooks.verify_signature(SECRET, sig, body, now=TS)  # i vjetri hidhet menjëherë


def test_signature_input_is_only_timestamp_and_body():
    """Vëzhgim sigurie (pa ndryshim): sign() merr vetëm (secret, timestamp, body); headerat
    X-SMS-Event-Id / X-SMS-Delivery-Id NUK nënshkruhen, dedup është përgjegjësi e marrësit."""
    import inspect

    assert list(inspect.signature(webhooks.sign).parameters) == ["secret", "timestamp", "body"]


# --- producer reale → bytes ------------------------------------------------------------


def test_emit_then_deliver_produces_the_golden_shape_for_ping(db):
    ep_id, _ = seed(BY_NAME["webhook.ping"], fixed_secret=True)
    with SessionLocal() as s:
        ev = webhooks.send_test(s, "c1", ep_id)
        s.commit()
        ev_id = ev.id
    cap = Capture().install()
    with SessionLocal() as s:
        while webhooks.deliver_next(s, datetime.now(UTC) + timedelta(seconds=5)):
            pass
    bodies = [json.loads(r.content) for r in cap.requests]
    mine = next(b for b in bodies if b["id"] == f"evt_{ev_id}")
    assert mine["type"] == "webhook.ping"
    assert mine["data"] == {"ok": True, "resource_id": str(ep_id), "resource_type": "endpoint"}
    assert sorted(mine) == ["created_at", "data", "id", "type"]


def test_wallet_producer_formats_money_as_six_decimal_strings(db):
    from sqlalchemy import select

    from app.models.events import Event as Ev
    from app.services import wallet as wallets

    w = wallets.create_wallet(db, "c1", "EUR")
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "10", wallets.TopupMethod.CASH).id)
    wallets.set_low_balance_threshold(db, w.id, "50")
    db.commit()
    ev = db.scalars(select(Ev).where(Ev.type == "wallet.low_balance")).one()
    assert set(ev.data) == {"currency", "available", "threshold"}
    assert ev.data["threshold"] == "50.000000"  # Numeric(20,6)
    assert re.fullmatch(r"\d+\.\d{6}", ev.data["available"])


# --- DB round-trip (PG = burimi i së vërtetës) -------------------------------------------


@pytest.mark.parametrize("name", ["message.received.non_ascii", "edge.microseconds_nonzero",
                                  "edge.nested_structure", "edge.data_none"])  # fmt: skip
def test_db_round_trip_bytes_equal_golden(db, name):
    case = BY_NAME[name]
    db.add(make_event(case))
    db.commit()
    db.close()
    with SessionLocal() as fresh:
        ev = fresh.get(Event, case["event"]["id"])
        assert webhooks.envelope(ev) == expected_body(case)
        # SQLite kthen datetime naive, PostgreSQL aware: bytes janë të njëjtë (as_utc)
        assert (ev.created_at.tzinfo is not None) == IS_PG
        assert ev.created_at.microsecond == (123456 if "nonzero" in name else 0)


def test_all_catalog_cases_survive_json_column_round_trip(db):
    for c in DB_CASES:
        db.add(make_event(c))
    db.commit()
    db.close()
    with SessionLocal() as fresh:
        for c in DB_CASES:
            assert webhooks.envelope(fresh.get(Event, c["event"]["id"])) == expected_body(c)


# --- katalogu publik vs intern ----------------------------------------------------------

PUBLIC_CATALOG = {
    "message.sent", "message.delivered", "message.failed", "message.received",
    "email.sent", "email.delivered", "email.bounced", "email.complained", "email.failed",
    "campaign.running", "campaign.paused", "campaign.completed", "campaign.cancelled",
    "consent.opted_out", "consent.opted_in", "webhook.ping",
    "invoice.issued", "invoice.paid", "payment.succeeded", "payment.failed",
    "wallet.low_balance",
}  # fmt: skip


def test_known_types_is_exactly_the_expected_public_catalog():
    assert events.KNOWN_TYPES == PUBLIC_CATALOG and len(PUBLIC_CATALOG) == 21


def test_every_public_type_has_a_canonical_fixture():
    canon = {c["event"]["type"] for c in CASES if not c["name"].startswith("edge.")}
    assert canon == PUBLIC_CATALOG
    for c in CASES:
        assert c["event"]["type"] in PUBLIC_CATALOG  # asnjë tip jashtë katalogut


def test_internal_event_tables_do_not_leak_into_the_public_catalog():
    internal_prefixes = ("audit", "dlr", "message_event", "email_event", "inbound", "ledger")
    assert not [t for t in PUBLIC_CATALOG if t.startswith(internal_prefixes)]
    from app.models.events import Event as PublicEvent
    from app.models.sending import DlrReceipt, MessageEvent

    assert PublicEvent.__tablename__ == "sms_events"
    for internal in (MessageEvent, DlrReceipt):
        assert internal.__tablename__ != PublicEvent.__tablename__
        assert not issubclass(internal, PublicEvent)
    from app.models.admin import AuditLog
    from app.models.email import EmailEvent

    for internal in (AuditLog, EmailEvent):
        assert not issubclass(internal, PublicEvent)


def _py_files():
    return [p for p in ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def _emit_calls():
    for p in _py_files():
        tree = ast.parse(p.read_text())
        for n in ast.walk(tree):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "emit"
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "events"
            ):
                yield p.relative_to(ROOT).as_posix(), n


def test_only_known_modules_emit_public_events():
    emitters = {p for p, _ in _emit_calls()}
    assert emitters == {
        "services/wallet.py", "services/messages.py", "services/inbox.py",
        "services/campaigns.py", "services/billing.py", "services/payments.py",
        "services/emails.py", "services/webhooks.py", "services/consent.py",
    }  # fmt: skip
    # moduli i brendshëm/audit/DLR nuk importon events
    for internal in ("services/audit.py", "services/dlr.py"):
        f = ROOT / internal
        if f.exists():
            assert "events" not in {
                a.name for n in ast.walk(ast.parse(f.read_text()))
                if isinstance(n, ast.ImportFrom) for a in n.names
            }  # fmt: skip


# status emetuar = enum minus të përjashtuarit (kushtet te transition(); matricë eksplicite)
DYNAMIC = {
    "message": (MessageStatus, {"queued", "sending"}),
    "email": (EmailStatus, {"queued", "sending"}),
    "campaign": (CampaignStatus, {"draft", "scheduled", "preparing"}),
}


def _status_values(enum_cls: type[enum.Enum]) -> set[str]:
    return {m.value for m in enum_cls}


def test_dynamic_event_types_cover_every_emitted_status():
    for prefix, (enum_cls, excluded) in DYNAMIC.items():
        assert excluded <= _status_values(enum_cls)
        emitted = _status_values(enum_cls) - excluded
        assert {f"{prefix}.{s}" for s in emitted} <= events.KNOWN_TYPES, prefix
        # e kundërta: çdo `prefix.*` në katalog vjen nga një status i emetuar
        in_catalog = {t.split(".", 1)[1] for t in events.KNOWN_TYPES if t.startswith(prefix + ".")}
        known_non_status = {"message": {"received"}}.get(prefix, set())
        assert in_catalog - known_non_status == emitted, prefix


def test_every_emit_call_uses_a_known_literal_or_a_covered_dynamic_prefix():
    literal, prefixes = set(), set()
    for path, call in _emit_calls():
        t = call.args[2]
        if isinstance(t, ast.Constant):
            literal.add(t.value)
        elif isinstance(t, ast.JoinedStr):
            prefixes.add(t.values[0].value.rstrip("."))  # f"message.{...}"
        else:  # pragma: no cover
            pytest.fail(f"emit type i pa analizueshëm në {path}")
    assert literal <= events.KNOWN_TYPES
    assert prefixes == {"message", "email", "campaign", "consent"}
    expanded = set(literal)
    for p in ("message", "email", "campaign"):
        enum_cls, excluded = DYNAMIC[p]
        expanded |= {f"{p}.{s}" for s in _status_values(enum_cls) - excluded}
    expanded |= {"consent.opted_in", "consent.opted_out"}  # kind ∈ {opted_in, opted_out}
    assert expanded == events.KNOWN_TYPES  # çdo tip i katalogut ka producer


def test_literal_data_shapes_in_producers_match_the_canonical_fixtures():
    canon = {}
    for c in CASES:
        if not c["name"].startswith("edge.") and c["event"]["data"] is not None:
            canon.setdefault(c["event"]["type"], []).append(set(c["event"]["data"]))
    checked = 0
    for path, call in _emit_calls():
        if len(call.args) < 6 or not isinstance(call.args[5], ast.Dict):
            continue
        keys = {k.value for k in call.args[5].keys if isinstance(k, ast.Constant)}
        t = call.args[2]
        if isinstance(t, ast.Constant):
            types = [t.value]
        elif t.values[0].value.rstrip(".") == "consent":
            types = ["consent.opted_in", "consent.opted_out"]
        else:
            types = [x for x in canon if x.startswith(t.values[0].value)]
        for typ in types:
            for fixture_keys in canon[typ]:
                assert fixture_keys == keys, (path, typ)
        checked += 1
    assert checked >= 8
