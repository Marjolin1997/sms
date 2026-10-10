"""`sender.request.v1` — kërkesa e sender-it, Enterprise → Central (M10-S3). Vetëm formë (stdlib-only; pa transport/auth).

Drejtim dhe semantikë të ndryshme nga `cp.sender.v1` (Central → Enterprise), prandaj skemë e veçantë.

Zarfi (të gjitha fushat të detyrueshme; fusha të panjohura ose që mungojnë → refuzim):
  schema          `sender.request.v1`
  operation_id    UUID kanonik: VEPRIMI logjik lokal (një për çdo kërkesë/ridërgim). Riprovimi i transportit e mban të njëjtin.
  operation       `request` | `resubmit`
  enterprise_id   UUID kanonik (Central e verifikon kundrejt enterprise-ve të autorizuar të klientit — kurrë nuk besohet vetëm nga trupi)
  external_ref    identiteti i qëndrueshëm i sender-it (`sms-sender-<id lokal>`), i pandryshueshëm; NUK është vlera e sender-it
  country         ISO alpha-2 me shkronja të mëdha
  sender_kind     `alphanumeric` | `numeric` (Central e rinormalizon dhe verifikon paritetin)
  display_value   vlera siç u shtyp (3..16); Central nxjerr çelësin kanonik vetë — `norm_value` NUK dërgohet
  evidence_ref    null ose tekst ≤128 (referencë, jo dokument)
`owner_ref` NUK është pjesë e kontratës (identiteti kanonik është enterprise + external_ref).
Serializim kanonik: `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True)`."""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

SCHEMA = "sender.request.v1"
OPERATIONS = ("request", "resubmit")
KINDS = ("alphanumeric", "numeric")
EVIDENCE_MAX = 128
EXTERNAL_REF_PREFIX = "sms-sender-"
MAX_BODY_BYTES = 2_048
_REF = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_TOP = frozenset({
    "schema", "operation_id", "operation", "enterprise_id", "external_ref", "country", "sender_kind",
    "display_value", "evidence_ref",
})  # fmt: skip


class ContractError(ValueError):
    """Kërkesë e pavlefshme sipas `sender.request.v1`."""


class UnsupportedSchemaError(ContractError):
    pass


def external_ref_for(sender_id: int) -> str:
    """Harta e vetme lokal-id → external_ref (e qëndrueshme; nuk varet nga vlera e sender-it, koha ose operacioni)."""
    if isinstance(sender_id, bool) or not isinstance(sender_id, int) or sender_id < 1:
        raise ContractError("sender id must be a positive integer")
    return f"{EXTERNAL_REF_PREFIX}{sender_id}"


def _uuid(v: Any, f: str) -> str:
    if not isinstance(v, str):
        raise ContractError(f"{f} must be a UUID string")
    try:
        ok = str(uuid.UUID(v)) == v
    except ValueError:
        ok = False
    if not ok:
        raise ContractError(f"{f} must be a canonical lowercase UUID")
    return v


def _printable(v: str) -> bool:
    return v.isprintable()


@dataclass(frozen=True, slots=True)
class SenderRequestV1:
    doc: dict

    @property
    def operation_id(self) -> str:
        return self.doc["operation_id"]

    @property
    def operation(self) -> str:
        return self.doc["operation"]

    @property
    def enterprise_id(self) -> str:
        return self.doc["enterprise_id"]

    @property
    def external_ref(self) -> str:
        return self.doc["external_ref"]

    @property
    def country(self) -> str:
        return self.doc["country"]

    @property
    def sender_kind(self) -> str:
        return self.doc["sender_kind"]

    @property
    def display_value(self) -> str:
        return self.doc["display_value"]

    @property
    def evidence_ref(self) -> str | None:
        return self.doc["evidence_ref"]

    def to_dict(self) -> dict:
        return json.loads(self.to_bytes())

    def to_bytes(self) -> bytes:
        return json.dumps(
            self.doc, separators=(",", ":"), sort_keys=True, ensure_ascii=True
        ).encode("utf-8")

    def request_hash(self) -> str:
        """Hash i veprimit të plotë (përfshin `operation_id` dhe `operation`): i njëjti operation_id me përmbajtje tjetër ⇒ konflikt."""
        return hashlib.sha256(self.to_bytes()).hexdigest()

    @classmethod
    def build(
        cls,
        *,
        operation_id: str,
        operation: str,
        enterprise_id: str,
        external_ref: str,
        country: str,
        sender_kind: str,
        display_value: str,
        evidence_ref: str | None = None,
    ) -> "SenderRequestV1":
        return cls.parse({
            "schema": SCHEMA, "operation_id": operation_id, "operation": operation,
            "enterprise_id": enterprise_id, "external_ref": external_ref, "country": country,
            "sender_kind": sender_kind, "display_value": display_value, "evidence_ref": evidence_ref,
        })  # fmt: skip

    @classmethod
    def parse(cls, d: Any) -> "SenderRequestV1":
        if not isinstance(d, dict):
            raise ContractError("request must be an object")
        if d.get("schema") != SCHEMA:
            raise UnsupportedSchemaError(f"unsupported schema {d.get('schema')!r}")
        extra, missing = set(d) - _TOP, _TOP - set(d)
        if extra or missing:
            raise ContractError(
                f"request fields: unexpected {sorted(extra)}, missing {sorted(missing)}"
            )
        if d["operation"] not in OPERATIONS:
            raise ContractError(f"operation must be one of {OPERATIONS}")
        ref = d["external_ref"]
        if not isinstance(ref, str) or not _REF.match(ref):
            raise ContractError("external_ref must be 1..64 chars of [A-Za-z0-9._:-]")
        country = d["country"]
        if not isinstance(country, str) or not _COUNTRY.match(country):
            raise ContractError("country must be two uppercase letters")
        if d["sender_kind"] not in KINDS:
            raise ContractError(f"sender_kind must be one of {KINDS}")
        val = d["display_value"]
        if not isinstance(val, str) or not 3 <= len(val) <= 16 or not _printable(val):
            raise ContractError("display_value must be 3..16 printable characters")
        ev = d["evidence_ref"]
        if ev is not None and (
            not isinstance(ev, str) or not 1 <= len(ev) <= EVIDENCE_MAX or not _printable(ev)
        ):
            raise ContractError(
                f"evidence_ref must be null or 1..{EVIDENCE_MAX} printable characters"
            )
        return cls({
            "schema": SCHEMA, "operation_id": _uuid(d["operation_id"], "operation_id"),
            "operation": d["operation"], "enterprise_id": _uuid(d["enterprise_id"], "enterprise_id"),
            "external_ref": ref, "country": country, "sender_kind": d["sender_kind"],
            "display_value": val, "evidence_ref": ev,
        })  # fmt: skip

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SenderRequestV1":
        try:
            return cls.parse(json.loads(raw))
        except ValueError as e:
            if isinstance(e, ContractError):
                raise
            raise ContractError(f"request is not valid JSON: {e}") from e
