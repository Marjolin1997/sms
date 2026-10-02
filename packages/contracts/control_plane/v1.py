"""`cp.v1` — kontrata e gjendjes autoritative Central → Enterprise (vetëm formë, pa transport/auth).

Zarfi:  {"schema":"cp.v1","event_id","seq","type","enterprise_id","entity":{"type","id"},
         "revision","occurred_at","data":{...}}
- `seq`: pozicioni në feed-in global (kursor). RENDI I GJENDJES SË ENTITETIT vendoset VETËM nga
  `revision` (per entitet): më e madhe → apliko; e barabartë → no-op; më e vogël → injoro.
  Konsumatori nuk duhet të përdorë `seq` vetëm për të vendosur nëse gjendja është më e re.
- `occurred_at`: vetëm informativ (UTC, gjerësi fikse me mikrosekonda); asnjëherë autoritet rendi.
- `data`: GJENDJA E PLOTË e entitetit në çastin e ndryshimit (state-based; asnjë histori veprimesh).
- Dy tipe: `enterprise.upserted`, `enterprise_product.upserted`. S'ka delete/tombstone.

Rregullat e pajtueshmërisë (konsumator):
- fusha të panjohura në ZARF → refuzim (`ContractError`);
- `schema` ≠ `cp.v1` → `UnsupportedSchemaError` (mos aplikoni; rikonsilimi dështon qartë);
- `type` i panjohur → `UnknownEventTypeError` (mos kaloni pa trajtim);
- fusha të panjohura te `data` (dhe `data.product`) brenda të njëjtës version major → **injorohen**
  (konsumatori përdor vetëm fushat e njohura); shtimi i fushave të reja është ndryshim i rishikuar;
- ndryshim thyes → `cp.v2`.

Serializimi: `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")`.
Pa nënshkrim këtu: autentikimi i transportit është çështje e M7-c.
"""

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SCHEMA = "cp.v1"

ENTITY_ENTERPRISE = "enterprise"
ENTITY_ENTERPRISE_PRODUCT = "enterprise_product"
EVENT_ENTERPRISE_UPSERTED = "enterprise.upserted"
EVENT_ENTERPRISE_PRODUCT_UPSERTED = "enterprise_product.upserted"
ENTITY_BY_EVENT = {
    EVENT_ENTERPRISE_UPSERTED: ENTITY_ENTERPRISE,
    EVENT_ENTERPRISE_PRODUCT_UPSERTED: ENTITY_ENTERPRISE_PRODUCT,
}

ENTERPRISE_STATUSES = frozenset({"active", "suspended"})
ASSIGNMENT_STATUSES = frozenset({"active", "suspended"})
CHANNELS = frozenset({"sms", "email"})  # kanalet e sotme; një kanal i ri = ndryshim i rishikuar

NAME_MAX = 200
INT64_MAX = 2**63 - 1
_CODE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_OCCURRED_AT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00$")


class ContractError(ValueError):
    """Zarf/gjendje e pavlefshme sipas `cp.v1`."""


class UnsupportedSchemaError(ContractError):
    pass


class UnknownEventTypeError(ContractError):
    pass


# --- validim (i vogël, eksplicit) ---


def _uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as e:
        raise ContractError(f"{field} is not a valid UUID") from e
    if str(parsed) != value:
        raise ContractError(f"{field} must be a canonical lowercase hyphenated UUID")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= INT64_MAX:
        raise ContractError(f"{field} must be an integer in 1..{INT64_MAX}")
    return value


def _choice(value: Any, allowed: frozenset[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ContractError(f"{field} must be one of {sorted(allowed)}")
    return value


def _as_utc(dt: datetime) -> datetime:
    """Naive = UTC; me timezone → UTC (e njëjta semantikë si `core.timeutil.as_utc`, kopje leaf)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def format_occurred_at(dt: datetime) -> str:
    """UTC, gjerësi fikse `YYYY-MM-DDTHH:MM:SS.ffffff+00:00` (mikrosekonda gjithmonë)."""
    if not isinstance(dt, datetime):
        raise ContractError("occurred_at must be a datetime")
    return _as_utc(dt).isoformat(timespec="microseconds")


def parse_occurred_at(text: Any) -> datetime:
    if not isinstance(text, str) or not _OCCURRED_AT.match(text):
        raise ContractError("occurred_at must match YYYY-MM-DDTHH:MM:SS.ffffff+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError as e:
        raise ContractError("occurred_at is not a valid timestamp") from e


# --- gjendjet ---


@dataclass(frozen=True, slots=True)
class EnterpriseStateV1:
    id: str
    name: str
    status: str

    def __post_init__(self) -> None:
        _uuid(self.id, "data.id")
        if (
            not isinstance(self.name, str)
            or not self.name.strip()
            or len(self.name) > NAME_MAX
            or _CONTROL.search(self.name)
        ):
            raise ContractError(f"data.name must be 1..{NAME_MAX} characters, no control chars")
        _choice(self.status, ENTERPRISE_STATUSES, "data.status")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "status": self.status}

    @classmethod
    def from_dict(cls, d: Any) -> "EnterpriseStateV1":
        if not isinstance(d, dict):
            raise ContractError("data must be an object")
        missing = {"id", "name", "status"} - d.keys()
        if missing:
            raise ContractError(f"data is missing {sorted(missing)}")
        return cls(d["id"], d["name"], d["status"])  # fusha shtesë: injorohen


@dataclass(frozen=True, slots=True)
class EnterpriseProductStateV1:
    assignment_id: str
    enterprise_id: str
    product_id: str
    product_code: str
    channel: str
    status: str

    def __post_init__(self) -> None:
        _uuid(self.assignment_id, "data.assignment_id")
        _uuid(self.enterprise_id, "data.enterprise_id")
        _uuid(self.product_id, "data.product.id")
        if not isinstance(self.product_code, str) or not _CODE.match(self.product_code):
            raise ContractError("data.product.code must match [a-z][a-z0-9_]{1,31}")
        _choice(self.channel, CHANNELS, "data.product.channel")
        _choice(self.status, ASSIGNMENT_STATUSES, "data.status")

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignment_id": self.assignment_id,
            "enterprise_id": self.enterprise_id,
            "product": {"id": self.product_id, "code": self.product_code, "channel": self.channel},
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: Any) -> "EnterpriseProductStateV1":
        if not isinstance(d, dict):
            raise ContractError("data must be an object")
        missing = {"assignment_id", "enterprise_id", "product", "status"} - d.keys()
        product = d.get("product")
        if missing or not isinstance(product, dict):
            raise ContractError("data requires assignment_id, enterprise_id, product{}, status")
        missing_p = {"id", "code", "channel"} - product.keys()
        if missing_p:
            raise ContractError(f"data.product is missing {sorted(missing_p)}")
        return cls(d["assignment_id"], d["enterprise_id"], product["id"], product["code"],
                   product["channel"], d["status"])  # fmt: skip


# --- zarfi ---

_ENVELOPE_KEYS = frozenset(
    {
        "schema",
        "event_id",
        "seq",
        "type",
        "enterprise_id",
        "entity",
        "revision",
        "occurred_at",
        "data",
    }
)
_STATE_BY_EVENT = {
    EVENT_ENTERPRISE_UPSERTED: EnterpriseStateV1,
    EVENT_ENTERPRISE_PRODUCT_UPSERTED: EnterpriseProductStateV1,
}


@dataclass(frozen=True, slots=True)
class ControlPlaneEventV1:
    event_id: str
    seq: int
    type: str
    enterprise_id: str
    entity_id: str
    revision: int
    occurred_at: datetime
    data: EnterpriseStateV1 | EnterpriseProductStateV1
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise UnsupportedSchemaError(f"unsupported schema {self.schema!r}")
        _uuid(self.event_id, "event_id")
        _positive_int(self.seq, "seq")
        if self.type not in ENTITY_BY_EVENT:
            raise UnknownEventTypeError(f"unknown event type {self.type!r}")
        _uuid(self.enterprise_id, "enterprise_id")
        _uuid(self.entity_id, "entity.id")
        _positive_int(self.revision, "revision")
        format_occurred_at(self.occurred_at)
        object.__setattr__(self, "occurred_at", _as_utc(self.occurred_at))  # gjithmonë UTC aware
        if not isinstance(self.data, _STATE_BY_EVENT[self.type]):
            raise ContractError("data does not match the event type")
        if isinstance(self.data, EnterpriseStateV1):
            if not self.entity_id == self.enterprise_id == self.data.id:
                raise ContractError(
                    "enterprise event: entity.id, enterprise_id and data.id must match"
                )
        elif (
            self.entity_id != self.data.assignment_id
            or self.enterprise_id != self.data.enterprise_id
        ):
            raise ContractError("assignment event: entity.id/enterprise_id must match data")

    @property
    def entity_type(self) -> str:
        return ENTITY_BY_EVENT[self.type]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "event_id": self.event_id,
            "seq": self.seq,
            "type": self.type,
            "enterprise_id": self.enterprise_id,
            "entity": {"type": self.entity_type, "id": self.entity_id},
            "revision": self.revision,
            "occurred_at": format_occurred_at(self.occurred_at),
            "data": self.data.to_dict(),
        }

    def to_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(), separators=(",", ":"), sort_keys=True, ensure_ascii=True
        ).encode("utf-8")

    @classmethod
    def from_dict(cls, d: Any) -> "ControlPlaneEventV1":
        if not isinstance(d, dict):
            raise ContractError("event must be an object")
        schema = d.get("schema")
        if schema != SCHEMA:
            raise UnsupportedSchemaError(f"unsupported schema {schema!r}")
        extra, missing = d.keys() - _ENVELOPE_KEYS, _ENVELOPE_KEYS - d.keys()
        if extra or missing:
            raise ContractError(
                f"envelope fields: unexpected {sorted(extra)}, missing {sorted(missing)}"
            )
        etype = d["type"]
        if etype not in ENTITY_BY_EVENT:
            raise UnknownEventTypeError(f"unknown event type {etype!r}")
        entity = d["entity"]
        if not isinstance(entity, dict) or entity.keys() != {"type", "id"}:
            raise ContractError("entity must be exactly {type, id}")
        if entity["type"] != ENTITY_BY_EVENT[etype]:
            raise ContractError("entity.type does not match the event type")
        return cls(
            event_id=d["event_id"], seq=d["seq"], type=etype, enterprise_id=d["enterprise_id"],
            entity_id=entity["id"], revision=d["revision"],
            occurred_at=parse_occurred_at(d["occurred_at"]),
            data=_STATE_BY_EVENT[etype].from_dict(d["data"]),
        )  # fmt: skip

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ControlPlaneEventV1":
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ContractError("event is not valid JSON") from e
        return cls.from_dict(decoded)
