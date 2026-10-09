"""`cp.sender.v1` — GJENDJA autoritative e politikës së shtetit për sender-a dhe e regjistrit global, Central → Enterprise (vetëm formë, pa transport/auth; vetëm stdlib).
NUK është `cp.v1` (enterprise/produkt) as `cp.money.v1` (fakte financiare): feed, kursor, skop (`sender:read`) dhe snapshot të ndarë.

Zarfi: {"schema":"cp.sender.v1","event_id","seq","event_type","enterprise_id"|null,"entity":{"type","id"},"revision","group":{"id","size"},"occurred_at","data":{...}}
- `seq`: pozicioni në feed-in e sender-ave (kursor global). RENDI I GJENDJES së një entiteti vendoset VETËM nga `revision` (per entitet): më i madh → apliko; i barabartë me
  përmbajtje identike → no-op; i barabartë me përmbajtje tjetër → konflikt (fail-closed); më i vogël → i vjetruar. `seq` nuk vendos kurrë rendin e gjendjes.
- Dy tipe, GJENDJE e plotë (jo komandë): `sender.policy.upserted` (global: `enterprise_id=null`; entiteti = (shtet, lloj); `revision` = revizioni i politikës) dhe
  `sender.registry.upserted` (me `enterprise_id`; entiteti = rreshti i regjistrit; `revision` = numri i vendimit të fundit për rreshtin).
- `group`: ngjarjet e krijuara nga i njëjti transaksion Central (p.sh. politikë `allowed=false` + revokimet që shkakton) kanë të njëjtin `group.id` dhe `group.size` = numri i tyre,
  me `seq` të njëpasnjëshëm. Central s'e ndan kurrë një grup nëpër faqe dhe konsumatori e aplikon grupin VETËM të plotë ⇒ asnjë gjendje e përzier (politikë e re + miratim i vjetër).
- Pa histori vendimesh dhe pa tekst të lirë (arsye): historia mbetet autoritative në Central.

Pajtueshmëria (konsumator): fusha të panjohura në ZARF → refuzim (`ContractError`); `schema` tjetër → `UnsupportedSchemaError`; `event_type` i panjohur → `UnknownEventTypeError`;
fusha të panjohura te `data` → INJOROHEN brenda të njëjtës version major (si `cp.v1`); fusha të njohura mungojnë/të pavlefshme → refuzim. Ndryshim thyes → `cp.sender.v2`.
Serializimi kanonik: `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True)`."""

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SCHEMA = "cp.sender.v1"
EVENT_POLICY = "sender.policy.upserted"
EVENT_REGISTRY = "sender.registry.upserted"
EVENT_TYPES = frozenset({EVENT_POLICY, EVENT_REGISTRY})
ENTITY_POLICY = "sender_policy"
ENTITY_REGISTRY = "sender_registry"
ENTITY_BY_EVENT = {EVENT_POLICY: ENTITY_POLICY, EVENT_REGISTRY: ENTITY_REGISTRY}
KINDS = frozenset({"alphanumeric", "numeric"})
STATUSES = frozenset({"pending", "approved", "rejected", "revoked"})
DECISIONS = frozenset({"requested", "approved", "rejected", "revoked", "resubmitted"})
POLICY_SOURCES = frozenset({"explicit", "default"})
INT64_MAX = 2**63 - 1
POLICY_NAMESPACE = uuid.UUID(
    "5a0b6c10-0c3e-4f6e-9d5e-53e1d10a5e11"
)  # i qëndrueshëm: identiteti i entitetit-politikë

_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_REF = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_DISPLAY = re.compile(r"^[A-Za-z0-9 ]{3,16}$")
_NORM = re.compile(r"^[a-z0-9 ]{3,16}$")
_KEY = re.compile(r"^[A-Z]{2}:[a-z0-9 ]{3,16}$")
_ENVELOPE_KEYS = frozenset(
    {
        "schema",
        "event_id",
        "seq",
        "event_type",
        "enterprise_id",
        "entity",
        "revision",
        "group",
        "occurred_at",
        "data",
    }
)


class ContractError(ValueError):
    """Zarf/payload i pavlefshëm sipas `cp.sender.v1`."""


class UnsupportedSchemaError(ContractError):
    pass


class UnknownEventTypeError(ContractError):
    pass


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


def _int(value: Any, field: str, lo: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= INT64_MAX:
        raise ContractError(f"{field} must be an integer in {lo}..{INT64_MAX}")
    return value


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{field} must be a boolean")
    return value


def _enum(value: Any, allowed, field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ContractError(f"{field} must be one of {sorted(allowed)}")
    return value


def _match(value: Any, rx: re.Pattern, field: str) -> str:
    if not isinstance(value, str) or not rx.match(value):
        raise ContractError(f"{field} is invalid")
    return value


def format_ts(dt: datetime) -> str:
    if not isinstance(dt, datetime):
        raise ContractError("timestamp must be a datetime")
    d = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return d.isoformat(timespec="microseconds")


def parse_ts(text: Any, field: str) -> datetime:
    if not isinstance(text, str) or not _TS.match(text):
        raise ContractError(f"{field} must match YYYY-MM-DDTHH:MM:SS.ffffff+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError as e:
        raise ContractError(f"{field} is not a valid timestamp") from e


def policy_entity_id(country: str, sender_kind: str) -> str:
    """Identiteti i qëndrueshëm i entitetit-politikë (shtet, lloj): UUIDv5."""
    return str(uuid.uuid5(POLICY_NAMESPACE, f"{country}:{sender_kind}"))


@dataclass(frozen=True, slots=True)
class PolicyStateV1:
    country: str
    sender_kind: str
    allowed: bool
    requires_approval: bool
    policy_id: str
    effective_from: datetime

    def __post_init__(self) -> None:
        _match(self.country, _COUNTRY, "data.country")
        _enum(self.sender_kind, KINDS, "data.sender_kind")
        _bool(self.allowed, "data.allowed")
        _bool(self.requires_approval, "data.requires_approval")
        if not self.allowed and not self.requires_approval:
            raise ContractError("a disallowed country/kind must keep requires_approval=true")
        _uuid(self.policy_id, "data.policy_id")
        format_ts(self.effective_from)

    def to_dict(self) -> dict[str, Any]:
        return {"country": self.country, "sender_kind": self.sender_kind, "allowed": self.allowed, "requires_approval": self.requires_approval,
                "policy_id": self.policy_id, "effective_from": format_ts(self.effective_from)}  # fmt: skip

    @classmethod
    def from_dict(cls, d: Any) -> "PolicyStateV1":
        if not isinstance(d, dict):
            raise ContractError("data must be an object")
        need = (
            "country",
            "sender_kind",
            "allowed",
            "requires_approval",
            "policy_id",
            "effective_from",
        )
        miss = [k for k in need if k not in d]
        if miss:
            raise ContractError(f"data is missing {miss}")
        return cls(
            d["country"],
            d["sender_kind"],
            d["allowed"],
            d["requires_approval"],
            d["policy_id"],
            parse_ts(d["effective_from"], "data.effective_from"),
        )


@dataclass(frozen=True, slots=True)
class RegistryStateV1:
    enterprise_id: str
    external_ref: str
    country: str
    sender_kind: str
    display_value: str
    norm_value: str
    status: str
    approved_key: str | None
    decision_id: str
    decision: str
    decided_at: datetime
    policy_source: str
    policy_id: str | None
    policy_revision: int | None

    def __post_init__(self) -> None:
        _uuid(self.enterprise_id, "data.enterprise_id")
        _match(self.external_ref, _REF, "data.external_ref")
        _match(self.country, _COUNTRY, "data.country")
        _enum(self.sender_kind, KINDS, "data.sender_kind")
        _match(self.display_value, _DISPLAY, "data.display_value")
        _match(self.norm_value, _NORM, "data.norm_value")
        if self.norm_value != self.display_value.lower().lstrip("+"):
            raise ContractError(
                "data.norm_value must be the canonical (lowercase) form of display_value"
            )
        _enum(self.status, STATUSES, "data.status")
        if (self.status == "approved") != (self.approved_key is not None):
            raise ContractError("data.approved_key is set exactly when status=approved")
        if (
            self.approved_key is not None
            and self.approved_key != f"{self.country}:{self.norm_value}"
        ):
            raise ContractError("data.approved_key must be '<COUNTRY>:<norm_value>'")
        _uuid(self.decision_id, "data.decision_id")
        _enum(self.decision, DECISIONS, "data.decision")
        format_ts(self.decided_at)
        _enum(self.policy_source, POLICY_SOURCES, "data.policy_source")
        if (self.policy_source == "explicit") != (
            self.policy_id is not None and self.policy_revision is not None
        ):
            raise ContractError(
                "data.policy_id/policy_revision are present exactly when policy_source=explicit"
            )
        if self.policy_id is not None:
            _uuid(self.policy_id, "data.policy_id")
            _int(self.policy_revision, "data.policy_revision")

    def to_dict(self) -> dict[str, Any]:
        return {"enterprise_id": self.enterprise_id, "external_ref": self.external_ref, "country": self.country, "sender_kind": self.sender_kind,
                "display_value": self.display_value, "norm_value": self.norm_value, "status": self.status, "approved_key": self.approved_key,
                "decision_id": self.decision_id, "decision": self.decision, "decided_at": format_ts(self.decided_at), "policy_source": self.policy_source,
                "policy_id": self.policy_id, "policy_revision": self.policy_revision}  # fmt: skip

    @classmethod
    def from_dict(cls, d: Any) -> "RegistryStateV1":
        if not isinstance(d, dict):
            raise ContractError("data must be an object")
        need = ("enterprise_id", "external_ref", "country", "sender_kind", "display_value", "norm_value", "status", "approved_key", "decision_id", "decision",
                "decided_at", "policy_source", "policy_id", "policy_revision")  # fmt: skip
        miss = [k for k in need if k not in d]
        if miss:
            raise ContractError(f"data is missing {miss}")
        return cls(d["enterprise_id"], d["external_ref"], d["country"], d["sender_kind"], d["display_value"], d["norm_value"], d["status"], d["approved_key"],
                   d["decision_id"], d["decision"], parse_ts(d["decided_at"], "data.decided_at"), d["policy_source"], d["policy_id"], d["policy_revision"])  # fmt: skip


_STATE_BY_EVENT = {EVENT_POLICY: PolicyStateV1, EVENT_REGISTRY: RegistryStateV1}


@dataclass(frozen=True, slots=True)
class SenderEventV1:
    event_id: str
    seq: int
    event_type: str
    enterprise_id: str | None
    entity_id: str
    revision: int
    group_id: str
    group_size: int
    occurred_at: datetime
    data: PolicyStateV1 | RegistryStateV1
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise UnsupportedSchemaError(f"unsupported schema {self.schema!r}")
        _uuid(self.event_id, "event_id")
        _int(self.seq, "seq")
        if self.event_type not in EVENT_TYPES:
            raise UnknownEventTypeError(f"unknown event type {self.event_type!r}")
        _uuid(self.entity_id, "entity.id")
        _int(self.revision, "revision")
        _uuid(self.group_id, "group.id")
        _int(self.group_size, "group.size")
        format_ts(self.occurred_at)
        if not isinstance(self.data, _STATE_BY_EVENT[self.event_type]):
            raise ContractError("data does not match the event type")
        if self.event_type == EVENT_POLICY:
            if self.enterprise_id is not None:
                raise ContractError("policy events are global: enterprise_id must be null")
            if self.entity_id != policy_entity_id(self.data.country, self.data.sender_kind):
                raise ContractError("entity.id does not match the policy scope")
        else:
            _uuid(self.enterprise_id, "enterprise_id")
            if self.enterprise_id != self.data.enterprise_id:
                raise ContractError("data.enterprise_id differs from the envelope enterprise_id")

    @property
    def entity_type(self) -> str:
        return ENTITY_BY_EVENT[self.event_type]

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "event_id": self.event_id, "seq": self.seq, "event_type": self.event_type, "enterprise_id": self.enterprise_id,
                "entity": {"type": self.entity_type, "id": self.entity_id}, "revision": self.revision, "group": {"id": self.group_id, "size": self.group_size},
                "occurred_at": format_ts(self.occurred_at), "data": self.data.to_dict()}  # fmt: skip

    def to_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(), separators=(",", ":"), sort_keys=True, ensure_ascii=True
        ).encode("utf-8")

    @classmethod
    def from_dict(cls, d: Any) -> "SenderEventV1":
        if not isinstance(d, dict):
            raise ContractError("event must be an object")
        if d.get("schema") != SCHEMA:
            raise UnsupportedSchemaError(f"unsupported schema {d.get('schema')!r}")
        extra, missing = d.keys() - _ENVELOPE_KEYS, _ENVELOPE_KEYS - d.keys()
        if extra or missing:
            raise ContractError(
                f"envelope fields: unexpected {sorted(extra)}, missing {sorted(missing)}"
            )
        etype = d["event_type"]
        if etype not in EVENT_TYPES:
            raise UnknownEventTypeError(f"unknown event type {etype!r}")
        ent, grp = d["entity"], d["group"]
        if (
            not isinstance(ent, dict)
            or ent.keys() != {"type", "id"}
            or ent["type"] != ENTITY_BY_EVENT[etype]
        ):
            raise ContractError("entity must be {type,id} matching the event type")
        if not isinstance(grp, dict) or grp.keys() != {"id", "size"}:
            raise ContractError("group must be {id,size}")
        return cls(d["event_id"], d["seq"], etype, d["enterprise_id"], ent["id"], d["revision"], grp["id"], grp["size"],
                   parse_ts(d["occurred_at"], "occurred_at"), _STATE_BY_EVENT[etype].from_dict(d["data"]))  # fmt: skip

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SenderEventV1":
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ContractError("event is not valid JSON") from e
        return cls.from_dict(decoded)


@dataclass(frozen=True, slots=True)
class SnapshotItemV1:
    """Një element i snapshot-it: e njëjta gjendje + revision si ngjarja, pa seq (kufiri është `snapshot_seq`)."""

    event_type: str
    enterprise_id: str | None
    entity_id: str
    revision: int
    data: PolicyStateV1 | RegistryStateV1

    def __post_init__(self) -> None:
        # rivlerëso invariantet e zarfit përmes një ngjarje fiktive të vlefshme
        SenderEventV1("00000000-0000-0000-0000-000000000001", 1, self.event_type, self.enterprise_id, self.entity_id, self.revision,
                      "00000000-0000-0000-0000-000000000002", 1, datetime(2000, 1, 1, tzinfo=UTC), self.data)  # fmt: skip

    def to_dict(self) -> dict[str, Any]:
        return {"event_type": self.event_type, "enterprise_id": self.enterprise_id, "entity": {"type": ENTITY_BY_EVENT[self.event_type], "id": self.entity_id},
                "revision": self.revision, "data": self.data.to_dict()}  # fmt: skip

    @classmethod
    def from_dict(cls, d: Any) -> "SnapshotItemV1":
        if not isinstance(d, dict) or d.keys() != {
            "event_type",
            "enterprise_id",
            "entity",
            "revision",
            "data",
        }:
            raise ContractError("snapshot item has unexpected or missing fields")
        etype = d["event_type"]
        if etype not in EVENT_TYPES:
            raise UnknownEventTypeError(f"unknown event type {etype!r}")
        ent = d["entity"]
        if (
            not isinstance(ent, dict)
            or ent.keys() != {"type", "id"}
            or ent["type"] != ENTITY_BY_EVENT[etype]
        ):
            raise ContractError("snapshot item entity is invalid")
        return cls(
            etype,
            d["enterprise_id"],
            ent["id"],
            d["revision"],
            _STATE_BY_EVENT[etype].from_dict(d["data"]),
        )
