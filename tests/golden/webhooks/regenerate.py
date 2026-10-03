"""Gjeneron `cases.json` dhe `*.body` nga kodi AKTUAL i prodhimit (envelope/sign/deliver_next).

VETËM për një ndryshim të miratuar të kontratës së webhook-ut (d.m.th. një version i ri): çdo ndryshim
i fixture-ve është ndryshim i bytes që marrësit e klientëve do ta shohin. Ekzekutim nga rrënja e repos:
    .venv/bin/python -m tests.golden.webhooks.regenerate
Testet NUK e thërrasin këtë skript; ato krahasojnë me skedarët statikë.
"""

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

os.environ["SMS_DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/gen.db"
os.environ["SMS_ADMIN_API_KEY"] = "gen"
os.environ["SMS_PII_HMAC_KEY"] = "gen"
os.environ["SMS_SECRETS_KEY"] = "wV0dVQ1nH7xk2m3bYw0m8y7QbKpZ0o1o9mGQ0mF0dJQ="

import httpx  # noqa: E402

import app.models  # noqa: E402, F401
from app.core import crypto  # noqa: E402
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.models.events import Event, WebhookDelivery  # noqa: E402
from app.services import net_guard, webhooks  # noqa: E402
from app.services.webhook_queue import queue  # noqa: E402

HERE = Path(__file__).parent
SECRET = "whsec_test_contract_v1"
TS = 1700000000
CREATED = "2030-01-01T12:00:00+00:00"
UUID = "6f1c2a9e-3b7d-4c55-9a10-0d2e5f7a8b11"

SENT = {"message_id": UUID, "status": "sent", "segments": 1}
CASES = [  # (emri, tipi, resource_type, resource_id, data, opsione)
    ("message.sent", "message.sent", "message", UUID, SENT, {}),
    (
        "message.delivered",
        "message.delivered",
        "message",
        UUID,
        {"message_id": UUID, "status": "delivered", "segments": 2},
        {},
    ),
    (
        "message.failed.error_code_null",
        "message.failed",
        "message",
        UUID,
        {"message_id": UUID, "status": "failed", "segments": 1, "error_code": None},
        {},
    ),
    (
        "message.failed.error_code_value",
        "message.failed",
        "message",
        UUID,
        {
            "message_id": UUID,
            "status": "failed",
            "segments": 1,
            "error_code": "rejected:invalid_number",
        },
        {},
    ),
    (
        "message.received.non_ascii",
        "message.received",
        "inbound",
        UUID,
        {
            "from": "355691234567",
            "to": "355690000001",
            "text": 'Përshëndetje 你好 😀 "ok" \\ \n',
            "action": None,
            "keyword": None,
        },
        {},
    ),
    ("email.sent", "email.sent", "email", UUID, {"email_id": UUID, "status": "sent"}, {}),
    (
        "email.delivered",
        "email.delivered",
        "email",
        UUID,
        {"email_id": UUID, "status": "delivered"},
        {},
    ),
    (
        "email.bounced",
        "email.bounced",
        "email",
        UUID,
        {"email_id": UUID, "status": "bounced", "reason": "550 5.1.1 user unknown"},
        {},
    ),
    (
        "email.complained",
        "email.complained",
        "email",
        UUID,
        {"email_id": UUID, "status": "complained", "reason": "abuse"},
        {},
    ),
    (
        "email.failed",
        "email.failed",
        "email",
        UUID,
        {"email_id": UUID, "status": "failed", "reason": None},
        {},
    ),
    (
        "campaign.running",
        "campaign.running",
        "campaign",
        "42",
        {"campaign_id": 42, "name": "Black Friday", "status": "running", "pause_reason": None},
        {},
    ),
    (
        "campaign.paused",
        "campaign.paused",
        "campaign",
        "42",
        {
            "campaign_id": 42,
            "name": "Black Friday",
            "status": "paused",
            "pause_reason": "insufficient_funds",
        },
        {},
    ),
    (
        "campaign.completed",
        "campaign.completed",
        "campaign",
        "42",
        {"campaign_id": 42, "name": "Black Friday", "status": "completed", "pause_reason": None},
        {},
    ),
    (
        "campaign.cancelled",
        "campaign.cancelled",
        "campaign",
        "42",
        {"campaign_id": 42, "name": "Black Friday", "status": "cancelled", "pause_reason": None},
        {},
    ),
    (
        "consent.opted_in",
        "consent.opted_in",
        "consent",
        "7",
        {"channel": "sms", "address": "355691234567", "reason": None, "hard": False},
        {},
    ),
    (
        "consent.opted_out",
        "consent.opted_out",
        "consent",
        "7",
        {"channel": "email", "address": "ana@example.com", "reason": "stop", "hard": True},
        {},
    ),
    (
        "invoice.issued",
        "invoice.issued",
        "invoice",
        "INV-2030-0001",
        {
            "invoice_id": "INV-2030-0001",
            "total": "12.500000",
            "currency": "EUR",
            "due_at": "2030-01-15T12:00:00+00:00",
        },
        {},
    ),
    (
        "invoice.paid",
        "invoice.paid",
        "invoice",
        "INV-2030-0001",
        {"invoice_id": "INV-2030-0001", "total": "12.500000", "via": "wallet"},
        {},
    ),
    (
        "payment.succeeded",
        "payment.succeeded",
        "payment",
        "9",
        {"payment_id": 9, "purpose": "topup", "amount": "10.000000", "currency": "EUR"},
        {},
    ),
    ("payment.failed", "payment.failed", "payment", "9", {"payment_id": 9, "purpose": "topup"}, {}),
    (
        "wallet.low_balance",
        "wallet.low_balance",
        "wallet",
        "3",
        {"currency": "EUR", "available": "4.950000", "threshold": "5.000000"},
        {},
    ),
    ("webhook.ping", "webhook.ping", "endpoint", "1", {"ok": True}, {}),
    # --- raste kufi ---
    (
        "edge.microseconds_zero",
        "message.sent",
        "message",
        UUID,
        SENT,
        {"created_at": "2030-01-01T12:00:00+00:00"},
    ),
    (
        "edge.microseconds_nonzero",
        "message.sent",
        "message",
        UUID,
        SENT,
        {"created_at": "2030-01-01T12:00:00.123456+00:00"},
    ),
    (
        "edge.naive_created_at",
        "message.sent",
        "message",
        UUID,
        SENT,
        {"created_at": "2030-01-01T12:00:00", "db": False},
    ),
    (
        "edge.offset_created_at",
        "message.sent",
        "message",
        UUID,
        SENT,
        {"created_at": "2030-01-01T14:00:00+02:00", "db": False},
    ),
    (
        "edge.decimal_scale_short",
        "wallet.low_balance",
        "wallet",
        "3",
        {"currency": "EUR", "available": "5", "threshold": "5"},
        {},
    ),
    (
        "edge.decimal_scale_long",
        "wallet.low_balance",
        "wallet",
        "3",
        {"currency": "EUR", "available": "5.0000", "threshold": "5.000000"},
        {},
    ),
    ("edge.data_none", "webhook.ping", "endpoint", "1", None, {}),
    ("edge.data_empty", "webhook.ping", "endpoint", "1", {}, {}),
    (
        "edge.key_ordering",
        "message.sent",
        "message",
        UUID,
        {"zeta": 1, "alpha": 2, "message_id": UUID, "Beta": 3, "segments": 1, "status": "sent"},
        {},
    ),
    (
        "edge.nested_structure",
        "message.sent",
        "message",
        UUID,
        {"z": {"y": [3, 1, {"b": None, "a": True}], "x": {}}, "id": "inner", "type": "inner"},
        {},
    ),
    (
        "edge.overwrite_resource_fields",
        "message.sent",
        "message",
        "m1",
        {"resource_type": "EVIL", "resource_id": "x", "status": "sent"},
        {},
    ),
]


def _reset():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def main() -> None:
    out, now = [], datetime.fromtimestamp(TS, UTC)
    net_guard.set_resolver(lambda host: ["93.184.216.34"])
    for i, (name, type_, rt, rid, data, opt) in enumerate(CASES):
        ev_id = 100 + i
        created = opt.get("created_at", CREATED)
        ev = Event(
            id=ev_id,
            owner_ref="c1",
            type=type_,
            resource_type=rt,
            resource_id=rid,
            data=data,
            created_at=datetime.fromisoformat(created),
        )
        body = webhooks.envelope(ev)
        sig = webhooks.sign(SECRET, TS, body)
        headers = None
        if opt.get("db", True):  # rruga e plotë: DB → deliver_next → kërkesa e kapur
            _reset()
            with SessionLocal() as db:
                ep, _ = webhooks.create_endpoint(db, "c1", "https://hooks.example.com/sms", None)
                ep.secret_enc = crypto.encrypt(SECRET.encode())
                db.add(
                    Event(
                        id=ev_id,
                        owner_ref="c1",
                        type=type_,
                        resource_type=rt,
                        resource_id=rid,
                        data=data,
                        created_at=datetime.fromisoformat(created),
                    )
                )
                db.flush()
                queue.publish(
                    db, [WebhookDelivery(endpoint_id=ep.id, event_id=ev_id, next_attempt_at=now)]
                )
                db.commit()
            seen = []
            webhooks.set_client(
                httpx.Client(
                    transport=httpx.MockTransport(
                        lambda r, seen=seen: (seen.append(r), httpx.Response(200))[1]
                    )
                )
            )
            with SessionLocal() as db:  # sesion i ri: bytes vijnë nga DB, jo nga identity map
                assert webhooks.deliver_next(db, now) is not None
            req = seen[0]
            assert req.content == body, name
            headers = {
                k: req.headers[k]
                for k in (
                    "content-type",
                    "user-agent",
                    "x-sms-signature",
                    "x-sms-event-id",
                    "x-sms-delivery-id",
                )
            }
            assert headers["x-sms-signature"] == sig, name
        (HERE / f"{name}.body").write_bytes(body)
        out.append(
            {
                "name": name,
                "db": opt.get("db", True),
                "event": {
                    "id": ev_id,
                    "type": type_,
                    "resource_type": rt,
                    "resource_id": rid,
                    "data": data,
                    "created_at": created,
                },
                "body_file": f"{name}.body",
                "parsed": json.loads(body),
                "signature": {"secret": SECRET, "timestamp": TS, "header": sig},
                "headers": headers,
            }
        )
    (HERE / "cases.json").write_text(json.dumps(out, indent=1, ensure_ascii=True) + "\n")
    print(len(out), "cases")


if __name__ == "__main__":
    main()
