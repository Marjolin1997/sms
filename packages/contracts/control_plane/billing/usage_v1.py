"""`cp.billing.usage.v1` — raporti KUMULATIV i email-eve të faturueshme, Enterprise → Central (M9-g2). Vetëm formë (stdlib-only; pa
transport/auth). Central s'varet nga skema operacionale e email-it: merr vetëm numërues dhe watermark.

Zarfi (të gjitha fushat të detyrueshme; fusha të panjohura → refuzim):
  schema, report_id, report_seq, enterprise_id, product_id (produkti EMAIL i enterprise-it), generated_at,
  watermark (id-ja më e madhe e provës së faturueshmërisë e dukshme në snapshot), cumulative_billable_count (sa email kanë hyrë
  KURRË për herë të parë në SENT/DELIVERED/BOUNCED/COMPLAINED, të dukshëm në të njëjtin snapshot).

Semantika (e miratuar; dokumentuar te `docs/M9G_BILLING.md`):
  · Një email numërohet SAKTËSISHT një herë, në çastin e tranzicionit të parë të faturueshëm (provë e pandryshueshme, UNIQUE(email_id)).
  · `cumulative_billable_count` dhe `watermark` vijnë nga NJË snapshot i vetëm: count = numri i provave të dukshme, watermark = id-ja
    maksimale e tyre ⇒ count ≤ watermark; count = 0 ⇔ watermark = 0. Të dyja janë jo-zbritëse në kohë (prova s'fshihet).
  · `generated_at` është çasti PARA hapjes së snapshot-it: raporti pretendon vetëm tranzicionet e commit-uara para tij. Tranzicione ende
    të hapura në atë çast hyjnë në raportin e radhës (ndarja e periudhës është raporti, jo `created_at` i email-it).
  · `report_seq` është numërues lokal monoton per (enterprise, product); koha s'është kursor.
Serializim kanonik: `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True)`."""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SCHEMA = "cp.billing.usage.v1"
INT64_MAX = 2**63 - 1
_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00$")
_TOP = frozenset({
    "schema", "report_id", "report_seq", "enterprise_id", "product_id", "generated_at", "watermark",
    "cumulative_billable_count",
})  # fmt: skip


class ContractError(ValueError):
    """Raport i pavlefshëm sipas `cp.billing.usage.v1`."""


class UnsupportedSchemaError(ContractError):
    pass


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


def _int(v: Any, f: str, lo: int = 0) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= INT64_MAX:
        raise ContractError(f"{f} must be an integer in {lo}..{INT64_MAX}")
    return v


def _ts(v: Any, f: str) -> str:
    if not isinstance(v, str) or not _TS.match(v):
        raise ContractError(f"{f} must match YYYY-MM-DDTHH:MM:SS.ffffff+00:00")
    try:
        datetime.fromisoformat(v)
    except ValueError as e:
        raise ContractError(f"{f} is not a valid timestamp") from e
    return v


def format_ts(dt: datetime) -> str:
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True)
class BillingUsageReportV1:
    """Raport i VALIDUAR dhe i normalizuar (`doc` është dict kanonik; s'ndryshohet pas ndërtimit)."""

    doc: dict

    @property
    def report_id(self) -> str:
        return self.doc["report_id"]

    @property
    def report_seq(self) -> int:
        return self.doc["report_seq"]

    @property
    def enterprise_id(self) -> str:
        return self.doc["enterprise_id"]

    @property
    def product_id(self) -> str:
        return self.doc["product_id"]

    @property
    def generated_at(self) -> datetime:
        return datetime.fromisoformat(self.doc["generated_at"])

    @property
    def watermark(self) -> int:
        return self.doc["watermark"]

    @property
    def count(self) -> int:
        return self.doc["cumulative_billable_count"]

    def to_dict(self) -> dict:
        return json.loads(self.to_bytes())

    def to_bytes(self) -> bytes:
        return json.dumps(
            self.doc, separators=(",", ":"), sort_keys=True, ensure_ascii=True
        ).encode("utf-8")

    def payload_hash(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def content_hash(self) -> str:
        """Hash i gjendjes (pa report_id/report_seq/generated_at): dy raporte me të njëjtin (watermark, count) janë të njëjta."""
        d = {
            k: v
            for k, v in self.doc.items()
            if k not in ("report_id", "report_seq", "generated_at")
        }
        return hashlib.sha256(
            json.dumps(d, separators=(",", ":"), sort_keys=True, ensure_ascii=True).encode()
        ).hexdigest()

    @classmethod
    def parse(cls, d: Any) -> "BillingUsageReportV1":
        if not isinstance(d, dict):
            raise ContractError("report must be an object")
        if d.get("schema") != SCHEMA:
            raise UnsupportedSchemaError(f"unsupported schema {d.get('schema')!r}")
        extra, missing = set(d) - _TOP, _TOP - set(d)
        if extra or missing:
            raise ContractError(
                f"report fields: unexpected {sorted(extra)}, missing {sorted(missing)}"
            )
        watermark = _int(d["watermark"], "watermark")
        count = _int(d["cumulative_billable_count"], "cumulative_billable_count")
        if count > watermark:
            raise ContractError("cumulative_billable_count cannot exceed watermark")
        if (count == 0) != (watermark == 0):
            raise ContractError(
                "watermark and cumulative_billable_count are both zero or both positive"
            )
        doc = {
            "schema": SCHEMA,
            "report_id": _uuid(d["report_id"], "report_id"),
            "report_seq": _int(d["report_seq"], "report_seq", 1),
            "enterprise_id": _uuid(d["enterprise_id"], "enterprise_id"),
            "product_id": _uuid(d["product_id"], "product_id"),
            "generated_at": _ts(d["generated_at"], "generated_at"),
            "watermark": watermark,
            "cumulative_billable_count": count,
        }
        return cls(doc)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "BillingUsageReportV1":
        try:
            return cls.parse(json.loads(raw))
        except ValueError as e:
            if isinstance(e, ContractError):
                raise
            raise ContractError(f"report is not valid JSON: {e}") from e
