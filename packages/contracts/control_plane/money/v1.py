"""`cp.money.v1` — ngjarje të pandryshueshme të grant-eve të kredisë, Central → Enterprise (vetëm formë,
pa transport/auth; vetëm stdlib). NUK është `cp.v1`: ai bart GJENDJE me `revision`; ky bart FAKTE financiare
additive me `seq` në feed-in e parave dhe identitet `grant_id`.

Zarfi: {"schema":"cp.money.v1","event_id","seq","event_type","enterprise_id","grant_id","occurred_at",
        "data":{"account_id","product_id","currency","amount","purpose","baseline_ref"}}
- `event_type`: `credit_grant.issued` | `credit_grant.reversed` (një ngjarje për grant e për tip).
- `data` është payload-i i NGRIRË në çastin e ngjarjes: i njëjti grant jep të njëjtat fusha në të dyja
  tipet (reversal përsërit identitetin dhe shumën). Mapper-i s'duhet të rilexojë kurrë gjendje të ndryshueshme.
- `amount`: string decimal me SAKTËSISHT 6 shifra pas presjes, > 0 (kurrë float/JSON number).
- `currency`: 3 shkronja të mëdha; `purpose`: `standard` | `bootstrap`;
  `baseline_ref`: SHA-256 hex (64, shkronja të vogla) kur `purpose=bootstrap`, përndryshe `null`.
- `seq`: pozicioni në feed-in e parave (kursor). `occurred_at`: informativ (UTC, mikrosekonda fikse).

Pajtueshmëria (konsumator): fusha të panjohura në ZARF ose në `data` → refuzim (para është: asnjë fushë
financiare nuk injorohet në heshtje); `schema` tjetër → `UnsupportedSchemaError`; `event_type` i panjohur →
`UnknownEventTypeError`. Ndryshim thyes → `cp.money.v2`.

Serializimi kanonik: `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()`.
"""

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

SCHEMA = "cp.money.v1"
EVENT_GRANT_ISSUED = "credit_grant.issued"
EVENT_GRANT_REVERSED = "credit_grant.reversed"
EVENT_TYPES = frozenset({EVENT_GRANT_ISSUED, EVENT_GRANT_REVERSED})
PURPOSE_STANDARD = "standard"
PURPOSE_BOOTSTRAP = "bootstrap"
PURPOSES = frozenset({PURPOSE_STANDARD, PURPOSE_BOOTSTRAP})
INT64_MAX = 2**63 - 1
MAX_AMOUNT = Decimal("99999999999999.999999")  # NUMERIC(20,6)

_AMOUNT = re.compile(r"^\d{1,14}\.\d{6}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_REF = re.compile(r"^[0-9a-f]{64}$")
_OCCURRED_AT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00$")
_ENVELOPE_KEYS = frozenset(
    {"schema", "event_id", "seq", "event_type", "enterprise_id", "grant_id", "occurred_at", "data"}
)
_DATA_KEYS = frozenset(
    {"account_id", "product_id", "currency", "amount", "purpose", "baseline_ref"}
)


class ContractError(ValueError):
    """Zarf/payload i pavlefshëm sipas `cp.money.v1`."""


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


def format_amount(amount: Decimal) -> str:
    """Decimal → string kanonik me 6 shifra (kurrë float)."""
    if isinstance(amount, float) or not isinstance(amount, Decimal):
        raise ContractError("amount must be a Decimal")
    if not amount.is_finite() or amount != amount.quantize(Decimal("0.000001")):
        raise ContractError("amount must have at most 6 decimal places")
    return format(amount.quantize(Decimal("0.000001")), "f")


def parse_amount(text: Any) -> Decimal:
    if not isinstance(text, str) or not _AMOUNT.match(text):
        raise ContractError("data.amount must be a decimal string with exactly 6 decimals")
    try:
        value = Decimal(text)
    except InvalidOperation as e:  # pragma: no cover (regex e mbron)
        raise ContractError("data.amount is not a decimal") from e
    if value <= 0 or value > MAX_AMOUNT:
        raise ContractError("data.amount must be > 0")
    return value


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def format_occurred_at(dt: datetime) -> str:
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


@dataclass(frozen=True, slots=True)
class GrantDataV1:
    account_id: str
    product_id: str
    currency: str
    amount: Decimal
    purpose: str = PURPOSE_STANDARD
    baseline_ref: str | None = None

    def __post_init__(self) -> None:
        _uuid(self.account_id, "data.account_id")
        _uuid(self.product_id, "data.product_id")
        if not isinstance(self.currency, str) or not _CURRENCY.match(self.currency):
            raise ContractError("data.currency must be 3 uppercase letters")
        format_amount(self.amount)
        if self.amount <= 0 or self.amount > MAX_AMOUNT:
            raise ContractError("data.amount must be > 0")
        if self.purpose not in PURPOSES:
            raise ContractError(f"data.purpose must be one of {sorted(PURPOSES)}")
        if self.purpose == PURPOSE_BOOTSTRAP:
            if not isinstance(self.baseline_ref, str) or not _REF.match(self.baseline_ref):
                raise ContractError("bootstrap data.baseline_ref must be a sha256 hex digest")
        elif self.baseline_ref is not None:
            raise ContractError("data.baseline_ref is only allowed for purpose=bootstrap")

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "product_id": self.product_id,
            "currency": self.currency,
            "amount": format_amount(self.amount),
            "purpose": self.purpose,
            "baseline_ref": self.baseline_ref,
        }

    @classmethod
    def from_dict(cls, d: Any) -> "GrantDataV1":
        if not isinstance(d, dict):
            raise ContractError("data must be an object")
        extra, missing = d.keys() - _DATA_KEYS, _DATA_KEYS - d.keys()
        if extra or missing:
            raise ContractError(
                f"data fields: unexpected {sorted(extra)}, missing {sorted(missing)}"
            )
        return cls(d["account_id"], d["product_id"], d["currency"], parse_amount(d["amount"]),
                   d["purpose"], d["baseline_ref"])  # fmt: skip


@dataclass(frozen=True, slots=True)
class MoneyEventV1:
    event_id: str
    seq: int
    event_type: str
    enterprise_id: str
    grant_id: str
    occurred_at: datetime
    data: GrantDataV1
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise UnsupportedSchemaError(f"unsupported schema {self.schema!r}")
        _uuid(self.event_id, "event_id")
        if (
            isinstance(self.seq, bool)
            or not isinstance(self.seq, int)
            or not 1 <= self.seq <= INT64_MAX
        ):
            raise ContractError(f"seq must be an integer in 1..{INT64_MAX}")
        if self.event_type not in EVENT_TYPES:
            raise UnknownEventTypeError(f"unknown event type {self.event_type!r}")
        _uuid(self.enterprise_id, "enterprise_id")
        _uuid(self.grant_id, "grant_id")
        format_occurred_at(self.occurred_at)
        object.__setattr__(self, "occurred_at", _as_utc(self.occurred_at))
        if not isinstance(self.data, GrantDataV1):
            raise ContractError("data must be GrantDataV1")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "event_id": self.event_id,
            "seq": self.seq,
            "event_type": self.event_type,
            "enterprise_id": self.enterprise_id,
            "grant_id": self.grant_id,
            "occurred_at": format_occurred_at(self.occurred_at),
            "data": self.data.to_dict(),
        }

    def to_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(), separators=(",", ":"), sort_keys=True, ensure_ascii=True
        ).encode("utf-8")

    @classmethod
    def from_dict(cls, d: Any) -> "MoneyEventV1":
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
        if d["event_type"] not in EVENT_TYPES:
            raise UnknownEventTypeError(f"unknown event type {d['event_type']!r}")
        return cls(
            event_id=d["event_id"], seq=d["seq"], event_type=d["event_type"],
            enterprise_id=d["enterprise_id"], grant_id=d["grant_id"],
            occurred_at=parse_occurred_at(d["occurred_at"]), data=GrantDataV1.from_dict(d["data"]),
        )  # fmt: skip

    @classmethod
    def from_bytes(cls, raw: bytes) -> "MoneyEventV1":
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ContractError("event is not valid JSON") from e
        return cls.from_dict(decoded)
