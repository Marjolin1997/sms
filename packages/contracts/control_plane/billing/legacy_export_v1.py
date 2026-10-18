"""`cp.billing.legacy_export.v1` — artifact i eksportit të faturimit legacy të Enterprise për importin në Central (M9-g4). Vetëm formë (stdlib-only).

Artifact offline (jo thirrje runtime): Enterprise e shkruan, Central e lexon; asnjëra s'lexon DB-në e tjetrës. Strikt: fusha të panjohura/mungesa ⇒ refuzim; rreshta të
renditur sipas `source_id` pa dublikime; `counts` duhet të përputhen me listat (kundër shkurtimit); `content_hash` = sha256 i JSON-it kanonik pa vetë fushën `content_hash`
(kontrollohet gjithmonë; mospërputhje ⇒ refuzim). Shumat janë string dhjetorë (kurrë float); koha `YYYY-MM-DDTHH:MM:SS.ffffff+00:00`. Pa sekrete; PII minimale
(snapshot-et ligjore të faturës `bill_to` mbahen siç u lëshuan)."""

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

SCHEMA = "cp.billing.legacy_export.v1"
INT64_MAX = 2**63 - 1
_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00$")
_DEC = re.compile(r"^(0|[1-9]\d{0,13})(\.\d{1,6})?$")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ContractError(ValueError):
    """Artifact i pavlefshëm sipas `cp.billing.legacy_export.v1`."""


def format_ts(dt: datetime) -> str:
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.isoformat(timespec="microseconds")


def _check(v: Any, spec: str, path: str) -> None:
    opt = spec.startswith("o:")
    if opt:
        spec = spec[2:]
        if v is None:
            return
    kind, _, arg = spec.partition(":")
    if kind == "uuid":
        ok = isinstance(v, str)
        if ok:
            try:
                ok = str(uuid.UUID(v)) == v
            except ValueError:
                ok = False
        if not ok:
            raise ContractError(f"{path}: canonical lowercase UUID expected")
    elif kind == "int":
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= INT64_MAX:
            raise ContractError(f"{path}: integer 0..{INT64_MAX} expected")
    elif kind == "bool":
        if not isinstance(v, bool):
            raise ContractError(f"{path}: boolean expected")
    elif kind == "str":
        if not isinstance(v, str) or len(v) > int(arg) or _CTRL.search(v):
            raise ContractError(f"{path}: string (<= {arg}, no control characters) expected")
    elif kind == "dec":
        if not isinstance(v, str) or not _DEC.match(v):
            raise ContractError(
                f"{path}: plain decimal string (<= 6 decimals, never a float) expected"
            )
    elif kind == "ts":
        if not isinstance(v, str) or not _TS.match(v):
            raise ContractError(f"{path}: timestamp YYYY-MM-DDTHH:MM:SS.ffffff+00:00 expected")
        try:
            datetime.fromisoformat(v)
        except ValueError as e:
            raise ContractError(f"{path}: invalid timestamp") from e
    elif kind == "enum":
        if v not in arg.split("|"):
            raise ContractError(f"{path}: one of {arg} expected")
    else:  # pragma: no cover - gabim programimi
        raise AssertionError(spec)


# Çdo entitet: emri → specifikim (prefiks `o:` = opsional/null). Çelësat janë të gjithë të detyrueshëm (vlera mund të jetë null kur `o:`).
LINE = {"source_id": "int", "description": "str:200", "quantity": "dec", "unit_price": "dec", "amount": "dec",
        "pricing_source": "o:str:12", "pricing_version_ref": "o:uuid"}  # fmt: skip
ENTITIES = {
    "plans": {
        "source_id": "int",
        "code": "str:32",
        "name": "str:80",
        "currency": "str:3",
        "monthly_fee": "dec",
        "included_emails": "int",
        "email_overage_price": "dec",
        "status": "enum:active|retired",
        "created_at": "ts",
    },  # fmt: skip
    "profiles": {
        "source_id": "int",
        "enterprise_id": "o:uuid",
        "owner_ref": "str:64",
        "legal_name": "str:120",
        "address": "str:300",
        "country": "str:2",
        "tax_id": "o:str:40",
        "email": "str:254",
        "vat_rate": "dec",
        "updated_at": "ts",
    },  # fmt: skip
    "subscriptions": {
        "source_id": "int",
        "enterprise_id": "o:uuid",
        "owner_ref": "str:64",
        "plan_source_id": "int",
        "pending_plan_source_id": "o:int",
        "status": "enum:active|cancelled",
        "started_at": "ts",
        "periods_billed": "int",
        "cancel_at_period_end": "bool",
        "auto_pay": "bool",
        "created_at": "ts",
    },  # fmt: skip
    "invoices": {
        "source_id": "int",
        "number": "str:24",
        "enterprise_id": "o:uuid",
        "owner_ref": "str:64",
        "subscription_source_id": "o:int",
        "period_start": "ts",
        "period_end": "ts",
        "currency": "str:3",
        "subtotal": "dec",
        "vat_rate": "dec",
        "tax": "dec",
        "total": "dec",
        "status": "enum:open|paid|void",
        "bill_to": "str:4000",
        "issued_at": "ts",
        "due_at": "ts",
        "paid_at": "o:ts",
        "paid_via": "o:str:24",
        "voided_reason": "o:str:200",
        "lines": "list",
    },  # fmt: skip
    "payments": {
        "source_id": "int",
        "enterprise_id": "o:uuid",
        "invoice_source_id": "int",
        "amount": "dec",
        "currency": "str:3",
        "provider": "str:32",
        "external_id": "str:128",
        "status": "enum:pending|succeeded|failed|expired",
        "completed_at": "o:ts",
    },  # fmt: skip
    "wallet_settlements": {
        "invoice_source_id": "int",
        "ledger_entry_id": "int",
        "amount": "dec",
        "currency": "str:3",
        "created_at": "ts",
    },
    "usage": {
        "enterprise_id": "uuid",
        "product_id": "o:uuid",
        "boundary": "ts",
        "cumulative_before_boundary": "int",
        "watermark_before_boundary": "int",
        "capture_active_since": "o:ts",
        "events_total": "int",
    },  # fmt: skip
}
_ID = {"plans": "source_id", "profiles": "source_id", "subscriptions": "source_id", "invoices": "source_id", "payments": "source_id",
       "wallet_settlements": "invoice_source_id", "usage": "enterprise_id"}  # fmt: skip
_TOP = {"schema", "export_id", "generated_at", "source", "authority", "counts", "plans", "profiles", "subscriptions", "invoices", "payments",
        "wallet_settlements", "sequences", "usage", "content_hash"}  # fmt: skip
_SOURCE = {"system": "enum:enterprise", "alembic_head": "str:32"}
_AUTH = {"mode": "enum:local|shadow|central", "due_unbilled_periods": "int"}
_SEQ = {"invoice_counters": "list", "credit_note_like": "int"}
_COUNTER = {"year": "int", "last_number": "int"}


def _obj(d: Any, spec: dict, path: str) -> None:
    if not isinstance(d, dict):
        raise ContractError(f"{path}: object expected")
    extra, missing = set(d) - set(spec), set(spec) - set(d)
    if extra or missing:
        raise ContractError(f"{path}: unexpected {sorted(extra)}, missing {sorted(missing)}")
    for k, s in spec.items():
        if s == "list":
            if not isinstance(d[k], list):
                raise ContractError(f"{path}.{k}: list expected")
        else:
            _check(d[k], s, f"{path}.{k}")


def canonical_bytes(doc: dict) -> bytes:
    body = {k: v for k, v in doc.items() if k != "content_hash"}
    return json.dumps(body, separators=(",", ":"), sort_keys=True, ensure_ascii=True).encode(
        "utf-8"
    )


def compute_hash(doc: dict) -> str:
    return hashlib.sha256(canonical_bytes(doc)).hexdigest()


def seal(doc: dict) -> dict:
    """Shton `content_hash` (përdoret nga eksportuesi)."""
    out = {k: v for k, v in doc.items() if k != "content_hash"}
    out["content_hash"] = compute_hash(out)
    return out


def parse(doc: Any) -> dict:
    """Validon plotësisht artifact-in (struktura, tipet, renditja, numëruesit, hash-i) dhe e kthen të pandryshuar."""
    if not isinstance(doc, dict):
        raise ContractError("artifact must be an object")
    if doc.get("schema") != SCHEMA:
        raise ContractError(f"unsupported schema {doc.get('schema')!r}")
    extra, missing = set(doc) - _TOP, _TOP - set(doc)
    if extra or missing:
        raise ContractError(
            f"artifact fields: unexpected {sorted(extra)}, missing {sorted(missing)}"
        )
    _check(doc["export_id"], "uuid", "export_id")
    _check(doc["generated_at"], "ts", "generated_at")
    _obj(doc["source"], _SOURCE, "source")
    _obj(doc["authority"], _AUTH, "authority")
    _obj(doc["sequences"], _SEQ, "sequences")
    for i, c in enumerate(doc["sequences"]["invoice_counters"]):
        _obj(c, _COUNTER, f"sequences.invoice_counters[{i}]")
    if not isinstance(doc["counts"], dict) or set(doc["counts"]) != set(ENTITIES):
        raise ContractError("counts must list every entity")
    for name, spec in ENTITIES.items():
        rows = doc[name]
        if not isinstance(rows, list):
            raise ContractError(f"{name}: list expected")
        if doc["counts"][name] != len(rows) or isinstance(doc["counts"][name], bool):
            raise ContractError(
                f"counts.{name} does not match the list length (truncated artifact?)"
            )
        seen = set()
        for i, row in enumerate(rows):
            _obj(row, spec, f"{name}[{i}]")
            if name == "invoices":
                for j, ln in enumerate(row["lines"]):
                    _obj(ln, LINE, f"invoices[{i}].lines[{j}]")
            key = row[_ID[name]]
            if key in seen:
                raise ContractError(f"{name}: duplicate {_ID[name]} {key}")
            seen.add(key)
        keys = [r[_ID[name]] for r in rows]
        if keys != sorted(keys):
            raise ContractError(f"{name}: rows must be sorted by {_ID[name]}")
    _check(doc["content_hash"], "str:64", "content_hash")
    if doc["content_hash"] != compute_hash(doc):
        raise ContractError("content_hash does not match the artifact content")
    return doc
