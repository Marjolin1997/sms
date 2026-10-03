"""Envelope V1 i webhook-ut dalës + serializer eksplicit (bytes të ngrira nga golden-et e d1).

Stdlib-only, pa ORM/FastAPI/Pydantic/httpx dhe pa `app.core`. `created_at` normalizohet në UTC me
një primitive të kopjuar qëllimisht nga `app.core.timeutil.as_utc` (një test e mban të barabartë),
që `contracts` të mbetet leaf pa varësi nga `core`.

Kontrata është vetëm ajo që del jashtë: id, type, created_at, data. `data` është objekti i gatshëm
(`resource_type`, `resource_id` + fushat e event-it; në përplasje fitojnë fushat e event-it):
ndërtimi i tij është përgjegjësi e mapper-it në shtresën e shërbimeve, jo e kontratës.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# Katalogu publik V1: çdo tip që del jashtë sistemit (webhook + `GET /v1/events`).
# Shtimi/heqja/rename është ndryshim kontrate (golden + versionim). Eventet e brendshme
# (MessageEvent, EmailEvent, AuditLog, DlrReceipt, jetëgjatësia e queue) NUK hyjnë këtu.
PUBLIC_EVENT_TYPES_V1: frozenset[str] = frozenset({
    "message.sent", "message.delivered", "message.failed", "message.received",
    "email.sent", "email.delivered", "email.bounced", "email.complained", "email.failed",
    "campaign.running", "campaign.paused", "campaign.completed", "campaign.cancelled",
    "consent.opted_out", "consent.opted_in", "webhook.ping",
    "invoice.issued", "invoice.paid", "payment.succeeded", "payment.failed",
    "wallet.low_balance",
})  # fmt: skip


def _as_utc(dt: datetime) -> datetime:
    """Naive = UTC; me timezone konvertohet në UTC (identike me `core.timeutil.as_utc`)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class EventEnvelopeV1:
    id: str  # "evt_<n>"
    type: str
    created_at: datetime
    data: dict[str, Any]

    def _wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "created_at": _as_utc(self.created_at).isoformat(),
            "data": self.data,
        }

    def to_dict(self) -> dict[str, Any]:
        out = self._wire()
        out["data"] = dict(self.data)  # kopje e cekët: s'ekspozohet objekti i brendshëm
        return out

    def to_bytes(self) -> bytes:
        return json.dumps(
            self._wire(), separators=(",", ":"), sort_keys=True, ensure_ascii=True
        ).encode("utf-8")
