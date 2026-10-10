"""`cp.money.usage.v1` — raporti KUMULATIV i përdorimit financiar, Enterprise → Central (M9-d). Vetëm formë
(stdlib-only; pa transport/auth). NUK është burim i dytë i së vërtetës: është pamje e rillogaritshme e ledger-it
të pandryshueshëm lokal, e marrë në NJË snapshot, që Central e krahason me atë që ka autorizuar.

Zarfi (të gjitha fushat e detyrueshme; fusha të panjohura → refuzim):
  schema, report_id, report_seq, enterprise_id, product_id, currency, generated_at, authority_mode,
  ledger_max_id, wallet{available,held,gross,active_hold_total,active_hold_count},
  baseline{baseline_ref,gross_at_cutover,ledger_max_id,status}|null,
  flows{baseline_gross,grants_applied,grant_reversals,captured,negative_adjustments,invoice_debits,
        other_debits,positive_local_credit,released}      # çdo gjë PAS baseline-it (ose gjithçka pa baseline)
  integrity{ledger_sum_available,ledger_sum_held,orphan_grant_credit,orphan_grant_reversal},
  cursor{epoch,last_seq,generation,last_success_at,has_error},
  grants[{grant_id,status,amount,currency,product_id,purpose,baseline_ref,issued_seq,reversed_seq,
          updated_at,detail}]

Ekuacioni i ruajtjes (nga llojet reale të ledger-it; HOLD/RELEASE janë neto 0 mbi gross):
  gross = baseline_gross + grants_applied − grant_reversals − captured − negative_adjustments
          − invoice_debits − other_debits + positive_local_credit
`report_seq` është numërues lokal monoton per (enterprise, product, currency): urdhëri kryesor, jo koha.
Shumat: string decimal me SAKTËSISHT 6 shifra (kurrë float). Gjendjet e wallet-it pranojnë shenjë (një
bilanc negativ duhet të dukët si CRITICAL, jo të refuzohet); të gjitha totalet kumulative janë ≥ 0.
Serializim kanonik: `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True)`.
"""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

SCHEMA = "cp.money.usage.v1"
MODES = frozenset({"local", "shadow", "central"})
GRANT_STATUSES = frozenset({
    "applied", "matched_to_existing_balance", "deferred_shadow", "baseline_mismatch", "unmapped",
    "reversed", "voided_before_apply", "reconciliation_required",
})  # fmt: skip
BASELINE_STATUSES = frozenset({"active", "superseded"})
PURPOSES = frozenset({"standard", "bootstrap"})
INT64_MAX = 2**63 - 1
MAX_GRANTS = 5000
MAX_DETAIL = 200

_AMOUNT = re.compile(r"^-?\d{1,14}\.\d{6}$")
_CUR = re.compile(r"^[A-Z]{3}$")
_REF = re.compile(r"^[0-9a-f]{64}$")
_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00$")
_TOP = frozenset({
    "schema", "report_id", "report_seq", "enterprise_id", "product_id", "currency", "generated_at",
    "authority_mode", "ledger_max_id", "wallet", "baseline", "flows", "integrity", "cursor", "grants",
})  # fmt: skip
_WALLET = ("available", "held", "gross", "active_hold_total")
_FLOWS = ("baseline_gross", "grants_applied", "grant_reversals", "captured", "negative_adjustments",
          "invoice_debits", "other_debits", "positive_local_credit", "released")  # fmt: skip
_INTEGRITY = (
    "ledger_sum_available",
    "ledger_sum_held",
    "orphan_grant_credit",
    "orphan_grant_reversal",
)
_GRANT_KEYS = frozenset({"grant_id", "status", "amount", "currency", "product_id", "purpose",
                         "baseline_ref", "issued_seq", "reversed_seq", "updated_at", "detail"})  # fmt: skip


class ContractError(ValueError):
    """Raport i pavlefshëm sipas `cp.money.usage.v1`."""


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


def _amount(v: Any, f: str, *, signed: bool = False) -> str:
    if not isinstance(v, str) or not _AMOUNT.match(v):
        raise ContractError(f"{f} must be a decimal string with exactly 6 decimals")
    if not signed and v.startswith("-"):
        raise ContractError(f"{f} must not be negative")
    return v


def _ts(v: Any, f: str) -> str:
    if not isinstance(v, str) or not _TS.match(v):
        raise ContractError(f"{f} must match YYYY-MM-DDTHH:MM:SS.ffffff+00:00")
    try:
        datetime.fromisoformat(v)
    except ValueError as e:
        raise ContractError(f"{f} is not a valid timestamp") from e
    return v


def _obj(v: Any, keys, f: str) -> dict:
    if not isinstance(v, dict) or set(v) != set(keys):
        raise ContractError(f"{f} must be an object with exactly {sorted(keys)}")
    return v


def format_ts(dt: datetime) -> str:
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.isoformat(timespec="microseconds")


def format_amount(d: Decimal) -> str:
    if isinstance(d, float) or not isinstance(d, Decimal):
        raise ContractError("amount must be a Decimal")
    q = Decimal("0.000001")
    if not d.is_finite() or d != d.quantize(q):
        raise ContractError("amount must have at most 6 decimal places")
    return format(d.quantize(q), "f")


def dec(s: str) -> Decimal:
    return Decimal(s)


@dataclass(frozen=True, slots=True)
class UsageReportV1:
    """Raport i VALIDUAR dhe i normalizuar (`doc` është dict kanonik; s'ndryshohet pas ndërtimit)."""

    doc: dict

    # -- aksesorë të shpeshtë
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
    def currency(self) -> str:
        return self.doc["currency"]

    @property
    def generated_at(self) -> datetime:
        return datetime.fromisoformat(self.doc["generated_at"])

    def to_dict(self) -> dict:
        return json.loads(self.to_bytes())

    def to_bytes(self) -> bytes:
        return json.dumps(
            self.doc, separators=(",", ":"), sort_keys=True, ensure_ascii=True
        ).encode("utf-8")

    def payload_hash(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def content_hash(self) -> str:
        """Hash i përmbajtjes pa identitetin/kohën (report_id, report_seq, generated_at, cursor.last_success_at/last_seq):
        dy raporte me të njëjtën gjendje financiare kanë të njëjtin content_hash (shmang dublikime)."""
        d = {
            k: v
            for k, v in self.doc.items()
            if k not in ("report_id", "report_seq", "generated_at")
        }
        d["cursor"] = {
            k: v for k, v in d["cursor"].items() if k not in ("last_success_at", "last_seq")
        }  # lëvizin pa efekt financiar
        return hashlib.sha256(
            json.dumps(d, separators=(",", ":"), sort_keys=True, ensure_ascii=True).encode()
        ).hexdigest()

    @classmethod
    def parse(cls, d: Any) -> "UsageReportV1":
        if not isinstance(d, dict):
            raise ContractError("report must be an object")
        if d.get("schema") != SCHEMA:
            raise UnsupportedSchemaError(f"unsupported schema {d.get('schema')!r}")
        extra, missing = set(d) - _TOP, _TOP - set(d)
        if extra or missing:
            raise ContractError(
                f"report fields: unexpected {sorted(extra)}, missing {sorted(missing)}"
            )
        cur = d["currency"]
        if not isinstance(cur, str) or not _CUR.match(cur):
            raise ContractError("currency must be 3 uppercase letters")
        if d["authority_mode"] not in MODES:
            raise ContractError(f"authority_mode must be one of {sorted(MODES)}")
        w = _obj(d["wallet"], (*_WALLET, "active_hold_count"), "wallet")
        wallet = {
            k: _amount(w[k], f"wallet.{k}", signed=k in ("available", "held", "gross"))
            for k in _WALLET
        }
        wallet["active_hold_count"] = _int(w["active_hold_count"], "wallet.active_hold_count")
        if dec(wallet["gross"]) != dec(wallet["available"]) + dec(wallet["held"]):
            raise ContractError("wallet.gross must equal available + held")
        base = d["baseline"]
        if base is not None:
            b = _obj(
                base, ("baseline_ref", "gross_at_cutover", "ledger_max_id", "status"), "baseline"
            )
            if not isinstance(b["baseline_ref"], str) or not _REF.match(b["baseline_ref"]):
                raise ContractError("baseline.baseline_ref must be a sha256 hex digest")
            if b["status"] not in BASELINE_STATUSES:
                raise ContractError("baseline.status is invalid")
            base = {"baseline_ref": b["baseline_ref"], "status": b["status"],
                    "gross_at_cutover": _amount(b["gross_at_cutover"], "baseline.gross_at_cutover"),
                    "ledger_max_id": _int(b["ledger_max_id"], "baseline.ledger_max_id")}  # fmt: skip
        f = _obj(d["flows"], _FLOWS, "flows")
        flows = {k: _amount(f[k], f"flows.{k}") for k in _FLOWS}
        i = _obj(d["integrity"], _INTEGRITY, "integrity")
        integrity = {
            k: _amount(i[k], f"integrity.{k}", signed=k.startswith("ledger_sum"))
            for k in _INTEGRITY
        }
        c = _obj(
            d["cursor"],
            ("epoch", "last_seq", "generation", "last_success_at", "has_error"),
            "cursor",
        )
        if not isinstance(c["has_error"], bool):
            raise ContractError("cursor.has_error must be a boolean")
        cursor = {
            "epoch": None if c["epoch"] is None else _uuid(c["epoch"], "cursor.epoch"),
            "last_seq": _int(c["last_seq"], "cursor.last_seq"),
            "generation": None if c["generation"] is None else _int(c["generation"], "cursor.generation", 1),
            "last_success_at": None if c["last_success_at"] is None else _ts(c["last_success_at"], "cursor.last_success_at"),
            "has_error": c["has_error"],
        }  # fmt: skip
        grants_in = d["grants"]
        if not isinstance(grants_in, list) or len(grants_in) > MAX_GRANTS:
            raise ContractError(f"grants must be an array of at most {MAX_GRANTS}")
        grants, seen = [], set()
        for n, g in enumerate(grants_in):
            g = _obj(g, _GRANT_KEYS, f"grants[{n}]")
            gid = _uuid(g["grant_id"], f"grants[{n}].grant_id")
            if gid in seen:
                raise ContractError(f"duplicate grant_id {gid}")
            seen.add(gid)
            if g["status"] not in GRANT_STATUSES or g["purpose"] not in PURPOSES:
                raise ContractError(f"grants[{n}] has an invalid status/purpose")
            if not isinstance(g["currency"], str) or not _CUR.match(g["currency"]):
                raise ContractError(f"grants[{n}].currency is invalid")
            ref = g["baseline_ref"]
            if ref is not None and (not isinstance(ref, str) or not _REF.match(ref)):
                raise ContractError(f"grants[{n}].baseline_ref is invalid")
            det = g["detail"]
            if det is not None and (not isinstance(det, str) or len(det) > MAX_DETAIL):
                raise ContractError(f"grants[{n}].detail must be a string of at most {MAX_DETAIL}")
            grants.append({
                "grant_id": gid, "status": g["status"], "amount": _amount(g["amount"], f"grants[{n}].amount"),
                "currency": g["currency"], "product_id": _uuid(g["product_id"], f"grants[{n}].product_id"),
                "purpose": g["purpose"], "baseline_ref": ref,
                "issued_seq": _int(g["issued_seq"], f"grants[{n}].issued_seq", 1),
                "reversed_seq": None if g["reversed_seq"] is None else _int(g["reversed_seq"], f"grants[{n}].reversed_seq", 1),
                "updated_at": _ts(g["updated_at"], f"grants[{n}].updated_at"), "detail": det,
            })  # fmt: skip
        grants.sort(key=lambda x: x["grant_id"])  # rend kanonik
        doc = {
            "schema": SCHEMA, "report_id": _uuid(d["report_id"], "report_id"),
            "report_seq": _int(d["report_seq"], "report_seq", 1),
            "enterprise_id": _uuid(d["enterprise_id"], "enterprise_id"),
            "product_id": _uuid(d["product_id"], "product_id"), "currency": cur,
            "generated_at": _ts(d["generated_at"], "generated_at"), "authority_mode": d["authority_mode"],
            "ledger_max_id": _int(d["ledger_max_id"], "ledger_max_id"), "wallet": wallet, "baseline": base,
            "flows": flows, "integrity": integrity, "cursor": cursor, "grants": grants,
        }  # fmt: skip
        return cls(doc)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "UsageReportV1":
        try:
            return cls.parse(json.loads(raw))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ContractError("report is not valid JSON") from e

    def conservation_gap(self) -> Decimal:
        """gross − (ekuacioni i ruajtjes). 0 = e vlefshme."""
        f, w = self.doc["flows"], self.doc["wallet"]
        expected = (dec(f["baseline_gross"]) + dec(f["grants_applied"]) - dec(f["grant_reversals"])
                    - dec(f["captured"]) - dec(f["negative_adjustments"]) - dec(f["invoice_debits"])
                    - dec(f["other_debits"]) + dec(f["positive_local_credit"]))  # fmt: skip
        return dec(w["gross"]) - expected
