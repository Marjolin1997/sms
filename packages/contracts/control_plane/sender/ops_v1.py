"""`sender.ops.v1` — snapshot operacional i sender-ave, Enterprise → Central (M11-D2). Vetëm formë (stdlib-only; pa transport/auth).

Kjo NUK është kontratë ngjarjesh: është gjendja e FUNDIT (latest-state), e vogël dhe e kufizuar; Central e mbishkruan idempotentisht. Pa histori,
pa vlera sender, pa PII, pa sekrete: vetëm numërues, statuse, mosha në sekonda, kode kategorish të kufizuara dhe prefikse hash-esh.

Zarfi (strikt; fushat e panjohura ose që mungojnë → refuzim):
  schema           `sender.ops.v1`
  snapshot_id      UUID kanonik (idempotencë: i njëjti id + përmbajtje identike ⇒ sukses; i njëjti id + përmbajtje tjetër ⇒ konflikt)
  scope            `deployment` (një snapshot për deployment-in Enterprise; identiteti i raportuesit vjen nga autentikimi, kurrë nga trupi)
  generated_at     UTC `YYYY-MM-DDTHH:MM:SSZ`
  source_revision  null ose ID rishikimi `[0-9A-Za-z._-]{1,64}`
  data             {sync, requests, shadow, bootstrap, cutover, alerts}

Semantika e mungesës (kritike):
- një SEKSION `null` = e padisponueshme (burimi s'u lexua) — Central e shfaq `unknown`, kurrë "zero e shëndetshme";
- `0` / `false` = vlerë e matur; një fushë `null` brenda një seksioni ka kuptimin e dokumentuar te fusha (p.sh. `oldest_open_age_seconds: null` = s'ka kërkesa të hapura).
Serializim kanonik: `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True)`."""

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SCHEMA = "sender.ops.v1"
SCOPE = "deployment"
MAX_BODY_BYTES = 16_384
MAX_INT = 2**53
MAX_ALERTS = 32
MAX_CATEGORIES = 24
MODES = ("local", "shadow", "central")
LEVELS = ("ok", "warning", "critical")
READINESS = ("PASS", "WARN", "FAIL")
_KEY = re.compile(r"^[a-z0-9_]{1,40}$")
_REV = re.compile(r"^[0-9A-Za-z._-]{1,64}$")
_HASH = re.compile(r"^[0-9a-f]{12}$")
_NAME = re.compile(r"^[a-z0-9_]{1,48}$")
_TOP = frozenset({"schema", "snapshot_id", "scope", "generated_at", "source_revision", "data"})
_SECTIONS = ("sync", "requests", "shadow", "bootstrap", "cutover", "alerts")


class ContractError(ValueError):
    """Snapshot i pavlefshëm sipas `sender.ops.v1`."""


class UnsupportedSchemaError(ContractError):
    pass


def format_ts(d: datetime) -> str:
    return d.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(v: Any, field: str) -> datetime:
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", v):
        raise ContractError(f"{field} must be a UTC timestamp YYYY-MM-DDTHH:MM:SSZ")
    try:
        return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise ContractError(f"{field} is not a valid timestamp") from None


def _int(v: Any, f: str, *, null: bool = False) -> int | None:
    if v is None and null:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= MAX_INT:
        raise ContractError(f"{f} must be an integer 0..2^53" + (" or null" if null else ""))
    return v


def _bool(v: Any, f: str, *, null: bool = False) -> bool | None:
    if v is None and null:
        return None
    if not isinstance(v, bool):
        raise ContractError(f"{f} must be a boolean" + (" or null" if null else ""))
    return v


def _num(v: Any, f: str, *, null: bool = False) -> float | int | None:
    if v is None and null:
        return None
    if (
        isinstance(v, bool)
        or not isinstance(v, (int, float))
        or not math.isfinite(v)
        or not 0 <= v <= 1e12
    ):
        raise ContractError(f"{f} must be a finite number 0..1e12" + (" or null" if null else ""))
    return v


def _enum(values):
    def check(v: Any, f: str, *, null: bool = False):
        if v is None and null:
            return None
        if v not in values:
            raise ContractError(f"{f} must be one of {values}")
        return v

    return check


def _hash(v: Any, f: str, *, null: bool = False) -> str | None:
    if v is None and null:
        return None
    if not isinstance(v, str) or not _HASH.match(v):
        raise ContractError(
            f"{f} must be 12 lowercase hex characters" + (" or null" if null else "")
        )
    return v


def _counts(v: Any, f: str, *, null: bool = False) -> dict | None:
    if v is None and null:
        return None
    if not isinstance(v, dict) or len(v) > MAX_CATEGORIES:
        raise ContractError(f"{f} must be an object with at most {MAX_CATEGORIES} categories")
    for k, n in v.items():
        if not isinstance(k, str) or not _KEY.match(k):
            raise ContractError(f"{f}: category names must match [a-z0-9_]{{1,40}}")
        _int(n, f"{f}.{k}")
    return dict(v)


def _ts(v: Any, f: str, *, null: bool = False) -> str | None:
    if v is None and null:
        return None
    parse_ts(v, f)
    return v


# fusha → (validator, nullable)
_SYNC = {
    "enabled": (_bool, False), "initialized": (_bool, False), "cursor_seq": (_int, True), "latest_central_seq": (_int, True), "lag": (_int, True),
    "last_success_age_seconds": (_int, True), "snapshot_age_seconds": (_int, True), "failure_count": (_int, False), "gap_recoveries": (_int, False),
    "drift_repairs": (_int, False), "policy_count": (_int, False), "authorization_count": (_int, False), "withdrawn_count": (_int, False),
    "projection_stale": (_bool, True), "last_error_age_seconds": (_int, True),
}  # fmt: skip
_REQUESTS = {
    "enabled": (_bool, False), "pending": (_int, False), "retry": (_int, False), "sending": (_int, False), "failed": (_int, False), "sent": (_int, False),
    "stuck_sending": (_int, False), "oldest_open_age_seconds": (_int, True), "last_sent_age_seconds": (_int, True), "attempts_max": (_int, False),
    "failed_by_category": (_counts, False),
}  # fmt: skip
_SHADOW = {
    "mode": (_enum(MODES), False), "window_hours": (_int, True), "min_samples": (_int, False), "comparisons_total": (_int, False), "match_total": (_int, False),
    "match_rate": (_num, True), "critical_total": (_int, False), "local_allow_central_deny": (_int, False), "local_deny_central_allow": (_int, False),
    "central_missing": (_int, False), "policy_mismatch": (_int, False), "identity_mismatch": (_int, False), "projection_stale": (_int, False),
    "by_category": (_counts, False),
}  # fmt: skip
_BOOT_STATE = {
    "completed": (_bool, False), "version": (_int, False), "completed_at": (_ts, True), "tenant_count": (_int, False), "sender_count": (_int, False),
    "unresolved_count": (_int, False), "report_hash": (_hash, True),
}  # fmt: skip
_BOOT_ISSUES = {
    "open": (_int, False),
    "resolved": (_int, False),
    "open_by_category": (_counts, False),
}
_CUTOVER = {
    "authority_mode": (_enum(MODES), False), "dispatch_recheck": (_bool, False), "ack_configured": (_bool, False), "ack_valid": (_bool, True),
    "evidence_count": (_int, False),
}  # fmt: skip
_EVIDENCE = {
    "kind": (_enum(("pre_cutover", "post_cutover", "rollback_ack")), False), "hash": (_hash, False), "readiness_status": (_enum(READINESS), False),
    "created_at": (_ts, False), "authority_version": (_int, False),
}  # fmt: skip
_ALERT = {
    "name": None,
    "level": (_enum(LEVELS), False),
    "value": (_num, True),
    "threshold": (_num, True),
}


def _obj(raw: Any, spec: dict, f: str) -> dict:
    if not isinstance(raw, dict):
        raise ContractError(f"{f} must be an object")
    extra, missing = set(raw) - set(spec), set(spec) - set(raw)
    if extra or missing:
        raise ContractError(f"{f}: unexpected {sorted(extra)}, missing {sorted(missing)}")
    return {k: spec[k][0](raw[k], f"{f}.{k}", null=spec[k][1]) for k in spec}


def _alerts(raw: Any) -> dict:
    if not isinstance(raw, dict) or set(raw) != {"items", "evaluated_age_seconds"}:
        raise ContractError("data.alerts must have exactly items and evaluated_age_seconds")
    items = raw["items"]
    if not isinstance(items, list) or len(items) > MAX_ALERTS:
        raise ContractError(f"data.alerts.items must be a list of at most {MAX_ALERTS}")
    out = []
    for i, a in enumerate(items):
        if not isinstance(a, dict) or set(a) != set(_ALERT):
            raise ContractError(f"data.alerts.items[{i}] must have exactly {sorted(_ALERT)}")
        if not isinstance(a["name"], str) or not _NAME.match(a["name"]):
            raise ContractError("alert names must match [a-z0-9_]{1,48}")
        out.append(
            {
                "name": a["name"],
                **{
                    k: _ALERT[k][0](a[k], f"alert.{k}", null=_ALERT[k][1])
                    for k in ("level", "value", "threshold")
                },
            }
        )
    return {
        "items": out,
        "evaluated_age_seconds": _int(
            raw["evaluated_age_seconds"], "data.alerts.evaluated_age_seconds", null=True
        ),
    }


def _section(raw: dict, name: str, fn):
    v = raw[name]
    return None if v is None else fn(v)  # seksion null = e padisponueshme


def _data(raw: Any) -> dict:
    if not isinstance(raw, dict) or set(raw) != set(_SECTIONS):
        raise ContractError(f"data must have exactly {list(_SECTIONS)}")

    def boot(v):
        if not isinstance(v, dict) or set(v) != {"state", "issues"}:
            raise ContractError("data.bootstrap must have exactly state and issues")
        return {
            "state": None
            if v["state"] is None
            else _obj(v["state"], _BOOT_STATE, "data.bootstrap.state"),
            "issues": _obj(v["issues"], _BOOT_ISSUES, "data.bootstrap.issues"),
        }

    def cut(v):
        if not isinstance(v, dict) or set(v) != set(_CUTOVER) | {"latest_evidence"}:
            raise ContractError("data.cutover has unexpected or missing fields")
        base = _obj({k: v[k] for k in _CUTOVER}, _CUTOVER, "data.cutover")
        ev = v["latest_evidence"]
        return {
            **base,
            "latest_evidence": None
            if ev is None
            else _obj(ev, _EVIDENCE, "data.cutover.latest_evidence"),
        }

    return {
        "sync": _section(raw, "sync", lambda v: _obj(v, _SYNC, "data.sync")),
        "requests": _section(raw, "requests", lambda v: _obj(v, _REQUESTS, "data.requests")),
        "shadow": _section(raw, "shadow", lambda v: _obj(v, _SHADOW, "data.shadow")),
        "bootstrap": _section(raw, "bootstrap", boot),
        "cutover": _section(raw, "cutover", cut),
        "alerts": _section(raw, "alerts", _alerts),
    }


@dataclass(frozen=True, slots=True)
class SenderOpsV1:
    doc: dict

    @property
    def snapshot_id(self) -> str:
        return self.doc["snapshot_id"]

    @property
    def generated_at(self) -> datetime:
        return parse_ts(self.doc["generated_at"], "generated_at")

    @property
    def source_revision(self) -> str | None:
        return self.doc["source_revision"]

    @property
    def data(self) -> dict:
        return self.doc["data"]

    def to_dict(self) -> dict:
        return json.loads(self.to_bytes())

    def to_bytes(self) -> bytes:
        return json.dumps(
            self.doc, separators=(",", ":"), sort_keys=True, ensure_ascii=True
        ).encode("utf-8")

    def content_hash(self) -> str:
        """Hash i të gjithë dokumentit (përfshin `snapshot_id` dhe `generated_at`): i njëjti id me përmbajtje tjetër ⇒ konflikt."""
        return hashlib.sha256(self.to_bytes()).hexdigest()

    @classmethod
    def parse(cls, d: Any) -> "SenderOpsV1":
        if not isinstance(d, dict):
            raise ContractError("snapshot must be an object")
        if d.get("schema") != SCHEMA:
            raise UnsupportedSchemaError(f"unsupported schema {d.get('schema')!r}")
        extra, missing = set(d) - _TOP, _TOP - set(d)
        if extra or missing:
            raise ContractError(
                f"snapshot fields: unexpected {sorted(extra)}, missing {sorted(missing)}"
            )
        sid = d["snapshot_id"]
        try:
            ok = isinstance(sid, str) and str(uuid.UUID(sid)) == sid
        except ValueError:
            ok = False
        if not ok:
            raise ContractError("snapshot_id must be a canonical lowercase UUID")
        if d["scope"] != SCOPE:
            raise ContractError(f"scope must be {SCOPE!r}")
        parse_ts(d["generated_at"], "generated_at")
        rev = d["source_revision"]
        if rev is not None and (not isinstance(rev, str) or not _REV.match(rev)):
            raise ContractError("source_revision must be null or [0-9A-Za-z._-]{1,64}")
        return cls(
            {
                "schema": SCHEMA,
                "snapshot_id": sid,
                "scope": SCOPE,
                "generated_at": d["generated_at"],
                "source_revision": rev,
                "data": _data(d["data"]),
            }
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SenderOpsV1":
        try:
            return cls.parse(json.loads(raw))
        except ValueError as e:
            if isinstance(e, ContractError):
                raise
            raise ContractError(f"snapshot is not valid JSON: {e}") from e
