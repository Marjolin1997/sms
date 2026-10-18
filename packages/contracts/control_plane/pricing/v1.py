"""`cp.pricing.v1` — SNAPSHOT i plotë, i versionuar dhe i verifikueshëm i çmimeve të klientit (Central → Enterprise).
Stdlib-only; vetëm formë (pa transport/auth). NUK është `cp.v1` (gjendje entitetesh) as `cp.money.v1` (fakte parash
additive): çmimi është shpërndarje versionesh të pandryshueshme dhe një mesazh duhet të shohë NJË version koherent.

Snapshot = gjithçka që i duhet një Enterprise për të çmuar lokalisht, në NJË dokument atomik:
  {"schema":"cp.pricing.v1","epoch","revision","authorization_generation","snapshot_hash",
   "enterprises":[{"enterprise_id","assignments":[{"assignment_id","product_id","price_book_id","effective_from"}]}],
   "books":[{"book_id","code","currency","versions":[{"version_id","version","status","effective_from",
             "content_hash","rules":[{"rule_id","channel","prefix","operator","unit_price"}]}]}]}
- `revision`: numërues monoton i Central (rritet në çdo aktivizim/tërheqje/caktim); `epoch` ndryshon vetëm me restore.
- `status` i versionit: `active` (e zgjedhshme sipas `effective_from`) | `retired` (e tërhequr: s'zgjidhet kurrë më; historia mbetet).
  Drafte KURRË nuk dërgohen. Versioni është i pandryshueshëm pasi aktivizohet; korrigjim = version i ri.
- `content_hash` i versionit = SHA-256 i rregullave kanonike (renditur); `snapshot_hash` = SHA-256 i (enterprises, books)
  kanonike. Konsumatori i rillogarit të dyja: snapshot i paplotë/i prishur NUK aktivizohet.
- Rregulla: `channel` sms (prefix shifra pa "+", `operator` MCCMNC opsional '') ose email (prefix '' dhe operator '': çmimi
  i një email-i mbi kuotën e përfshirë). `unit_price` = string me SAKTËSISHT 6 shifra, ≥ 0 (kurrë float).
Precedenca e kërkimit (e njëjtë me sjelljen ekzistuese të Enterprise): versioni = ai me `effective_from` më të vonë ≤ t
(nëse është `retired` ⇒ asnjë çmim, fail-closed); rregulla = prefiksi më i gjatë, brenda tij operatori specifik mbi të përgjithshmin.
Serializim kanonik: `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True)`."""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

SCHEMA = "cp.pricing.v1"
CHANNELS = frozenset({"sms", "email"})
VERSION_STATUSES = frozenset({"active", "retired"})
INT64_MAX = 2**63 - 1
MAX_RULES_PER_VERSION = 20000

_AMOUNT = re.compile(r"^\d{1,14}\.\d{6}$")
_CUR = re.compile(r"^[A-Z]{3}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_PREFIX = re.compile(r"^[1-9]\d{0,15}$")
_OPERATOR = re.compile(r"^\d{0,8}$")
_CODE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}\+00:00$")
_TOP = frozenset(
    {
        "schema",
        "epoch",
        "revision",
        "authorization_generation",
        "snapshot_hash",
        "enterprises",
        "books",
    }
)


class ContractError(ValueError):
    pass


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


def _keys(d: Any, keys, f: str) -> dict:
    if not isinstance(d, dict) or set(d) != set(keys):
        raise ContractError(f"{f} must be an object with exactly {sorted(keys)}")
    return d


def format_ts(dt: datetime) -> str:
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.isoformat(timespec="microseconds")


def format_price(d: Decimal) -> str:
    if isinstance(d, float) or not isinstance(d, Decimal):
        raise ContractError("price must be a Decimal")
    q = Decimal("0.000001")
    if not d.is_finite() or d < 0 or d != d.quantize(q):
        raise ContractError("price must be a non-negative decimal with at most 6 decimals")
    return format(d.quantize(q), "f")


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=True).encode("utf-8")


def rules_hash(rules: list[dict]) -> str:
    """Hash kanonik i rregullave të një versioni (pavarësisht nga rendi)."""
    ordered = sorted(rules, key=lambda r: (r["channel"], r["prefix"], r["operator"], r["rule_id"]))
    return hashlib.sha256(_canon(ordered)).hexdigest()


def body_hash(enterprises: list[dict], books: list[dict]) -> str:
    return hashlib.sha256(_canon({"enterprises": enterprises, "books": books})).hexdigest()


def _rule(r: Any, f: str) -> dict:
    r = _keys(r, ("rule_id", "channel", "prefix", "operator", "unit_price"), f)
    if r["channel"] not in CHANNELS:
        raise ContractError(f"{f}.channel must be one of {sorted(CHANNELS)}")
    prefix, op = r["prefix"], r["operator"]
    if not isinstance(prefix, str) or not isinstance(op, str) or not _OPERATOR.match(op):
        raise ContractError(f"{f}.prefix/operator are invalid")
    if r["channel"] == "sms":
        if not _PREFIX.match(prefix):
            raise ContractError(f"{f}.prefix must be digits without '+' or leading zero")
    elif prefix != "" or op != "":
        raise ContractError(f"{f}: email rules have empty prefix and operator")
    price = r["unit_price"]
    if not isinstance(price, str) or not _AMOUNT.match(price):
        raise ContractError(
            f"{f}.unit_price must be a non-negative decimal string with exactly 6 decimals"
        )
    return {"rule_id": _uuid(r["rule_id"], f"{f}.rule_id"), "channel": r["channel"], "prefix": prefix,
            "operator": op, "unit_price": price}  # fmt: skip


def _version(v: Any, f: str) -> dict:
    v = _keys(v, ("version_id", "version", "status", "effective_from", "content_hash", "rules"), f)
    if v["status"] not in VERSION_STATUSES:
        raise ContractError(f"{f}.status must be one of {sorted(VERSION_STATUSES)}")
    rules_in = v["rules"]
    if not isinstance(rules_in, list) or not rules_in or len(rules_in) > MAX_RULES_PER_VERSION:
        raise ContractError(
            f"{f}.rules must be a non-empty array of at most {MAX_RULES_PER_VERSION}"
        )
    rules, ids, scope = [], set(), set()
    for n, r in enumerate(rules_in):
        rr = _rule(r, f"{f}.rules[{n}]")
        key = (rr["channel"], rr["prefix"], rr["operator"])
        if rr["rule_id"] in ids or key in scope:
            raise ContractError(
                f"{f}.rules has a duplicate rule id or scope {key}"
            )  # asnjë "rresht i parë" i paqartë
        ids.add(rr["rule_id"])
        scope.add(key)
        rules.append(rr)
    rules.sort(key=lambda x: (x["channel"], x["prefix"], x["operator"]))
    ch = v["content_hash"]
    if not isinstance(ch, str) or not _HASH.match(ch) or ch != rules_hash(rules):
        raise ContractError(
            f"{f}.content_hash does not match its rules (incomplete or tampered version)"
        )
    return {"version_id": _uuid(v["version_id"], f"{f}.version_id"), "version": _int(v["version"], f"{f}.version", 1),
            "status": v["status"], "effective_from": _ts(v["effective_from"], f"{f}.effective_from"),
            "content_hash": ch, "rules": rules}  # fmt: skip


@dataclass(frozen=True, slots=True)
class PricingSnapshotV1:
    doc: dict

    @property
    def epoch(self) -> str:
        return self.doc["epoch"]

    @property
    def revision(self) -> int:
        return self.doc["revision"]

    @property
    def authorization_generation(self) -> int:
        return self.doc["authorization_generation"]

    @property
    def snapshot_hash(self) -> str:
        return self.doc["snapshot_hash"]

    def to_dict(self) -> dict:
        return json.loads(self.to_bytes())

    def to_bytes(self) -> bytes:
        return _canon(self.doc)

    @classmethod
    def build(
        cls,
        *,
        epoch: str,
        revision: int,
        generation: int,
        enterprises: list[dict],
        books: list[dict],
    ):
        """Central: hash-i llogaritet nga trupi, pastaj dokumenti kalon të njëjtin validim si te konsumatori."""
        norm = cls.parse({"schema": SCHEMA, "epoch": epoch, "revision": revision, "authorization_generation": generation,
                          "snapshot_hash": "0" * 64, "enterprises": enterprises, "books": books}, verify_hash=False)  # fmt: skip
        d = dict(norm.doc)
        d["snapshot_hash"] = body_hash(d["enterprises"], d["books"])
        return cls(d)

    @classmethod
    def parse(cls, d: Any, *, verify_hash: bool = True) -> "PricingSnapshotV1":
        if not isinstance(d, dict):
            raise ContractError("snapshot must be an object")
        if d.get("schema") != SCHEMA:
            raise UnsupportedSchemaError(f"unsupported schema {d.get('schema')!r}")
        extra, missing = set(d) - _TOP, _TOP - set(d)
        if extra or missing:
            raise ContractError(
                f"snapshot fields: unexpected {sorted(extra)}, missing {sorted(missing)}"
            )
        if not isinstance(d["books"], list) or not isinstance(d["enterprises"], list):
            raise ContractError("books and enterprises must be arrays")
        books, book_ids, codes, version_ids = [], {}, set(), set()
        for bn, b in enumerate(d["books"]):
            b = _keys(b, ("book_id", "code", "currency", "versions"), f"books[{bn}]")
            bid = _uuid(b["book_id"], f"books[{bn}].book_id")
            if (
                not isinstance(b["code"], str)
                or not _CODE.match(b["code"])
                or b["code"] in codes
                or bid in book_ids
            ):
                raise ContractError(f"books[{bn}] has an invalid or duplicate code/id")
            if not isinstance(b["currency"], str) or not _CUR.match(b["currency"]):
                raise ContractError(f"books[{bn}].currency must be 3 uppercase letters")
            if not isinstance(b["versions"], list):
                raise ContractError(f"books[{bn}].versions must be an array")
            versions, effs, nums = [], set(), set()
            for vn, v in enumerate(b["versions"]):
                vv = _version(v, f"books[{bn}].versions[{vn}]")
                if (
                    vv["version_id"] in version_ids
                    or vv["effective_from"] in effs
                    or vv["version"] in nums
                ):
                    raise ContractError(
                        f"books[{bn}].versions has a duplicate id, number or effective_from"
                    )
                version_ids.add(vv["version_id"])
                effs.add(vv["effective_from"])
                nums.add(vv["version"])
                versions.append(vv)
            versions.sort(key=lambda x: x["effective_from"])
            codes.add(b["code"])
            book_ids[bid] = b["currency"]
            books.append(
                {"book_id": bid, "code": b["code"], "currency": b["currency"], "versions": versions}
            )
        books.sort(key=lambda x: x["book_id"])
        ents, seen_e = [], set()
        for en, e in enumerate(d["enterprises"]):
            e = _keys(e, ("enterprise_id", "assignments"), f"enterprises[{en}]")
            eid = _uuid(e["enterprise_id"], f"enterprises[{en}].enterprise_id")
            if eid in seen_e or not isinstance(e["assignments"], list):
                raise ContractError(f"enterprises[{en}] is duplicated or malformed")
            seen_e.add(eid)
            asg, aids, scope = [], set(), set()
            for an, a in enumerate(e["assignments"]):
                a = _keys(
                    a,
                    ("assignment_id", "product_id", "price_book_id", "effective_from"),
                    f"enterprises[{en}].assignments[{an}]",
                )
                aid, pid, bk = (_uuid(a["assignment_id"], "assignment_id"), _uuid(a["product_id"], "product_id"),
                                _uuid(a["price_book_id"], "price_book_id"))  # fmt: skip
                eff = _ts(a["effective_from"], "assignment.effective_from")
                if bk not in book_ids:
                    raise ContractError(
                        f"assignment {aid} references a book that is not in the snapshot"
                    )
                if aid in aids or (pid, eff) in scope:
                    raise ContractError(
                        f"assignment {aid}: duplicate id or (product, effective_from)"
                    )
                aids.add(aid)
                scope.add((pid, eff))
                asg.append(
                    {
                        "assignment_id": aid,
                        "product_id": pid,
                        "price_book_id": bk,
                        "effective_from": eff,
                    }
                )
            asg.sort(key=lambda x: (x["product_id"], x["effective_from"]))
            ents.append({"enterprise_id": eid, "assignments": asg})
        ents.sort(key=lambda x: x["enterprise_id"])
        h = d["snapshot_hash"]
        if not isinstance(h, str) or not _HASH.match(h):
            raise ContractError("snapshot_hash must be a sha256 hex digest")
        if verify_hash and h != body_hash(ents, books):
            raise ContractError(
                "snapshot_hash does not match the content (incomplete or tampered snapshot)"
            )
        doc = {"schema": SCHEMA, "epoch": _uuid(d["epoch"], "epoch"), "revision": _int(d["revision"], "revision"),
               "authorization_generation": _int(d["authorization_generation"], "authorization_generation", 1),
               "snapshot_hash": h, "enterprises": ents, "books": books}  # fmt: skip
        return cls(doc)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "PricingSnapshotV1":
        try:
            return cls.parse(json.loads(raw))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ContractError("snapshot is not valid JSON") from e


# --- rregulla të përbashkëta të çmimit (një implementim: Central dhe Enterprise e përdorin të njëjtin) ----------------------

import decimal  # noqa: E402

E164 = re.compile(r"^\+?[1-9]\d{6,14}$")
PRICE_QUANT = Decimal("0.000001")
_CTX = decimal.Context(
    prec=60, rounding=decimal.ROUND_HALF_UP, traps=[decimal.InvalidOperation, decimal.Overflow]
)


def line_total(unit_price: Decimal, quantity: int) -> Decimal:
    """Totali i një linje = çmim_njësie × sasi (e plotë), i kuantizuar në 6 shifra me ROUND_HALF_UP në kontekst
    EKSPLICIT (jo konteksti implicit i Python). Me çmim ≤ 6 shifra dhe sasi të plotë prodhimi është i saktë (s'ka rrumbullakim
    real); funksioni e bën rregullin të shprehur dhe të testueshëm. Rezervimi dhe capture përdorin PAK këtë vlerë të ngrirë."""
    if (
        isinstance(unit_price, float)
        or isinstance(quantity, bool)
        or not isinstance(quantity, int)
        or quantity < 0
    ):
        raise ContractError("unit price must be a Decimal and quantity a non-negative integer")
    return _CTX.multiply(Decimal(unit_price), Decimal(quantity)).quantize(
        PRICE_QUANT, rounding=decimal.ROUND_HALF_UP, context=_CTX
    )


def candidate_prefixes(number: str) -> list[str]:
    if not isinstance(number, str) or not E164.match(number):
        raise ContractError("number must be E.164")
    digits = number.lstrip("+")
    return [digits[:i] for i in range(1, len(digits) + 1)]


def pick_rule(candidates: list[dict], operator: str = "") -> dict | None:
    """Precedenca: prefiksi më i gjatë fiton; brenda tij, rregulla e operatorit specifik mbi të përgjithshmen. Kandidatët
    kanë `prefix` dhe `operator` të ndryshëm (UNIQUE per version) ⇒ rezultati është deterministik, kurrë "rreshti i parë"."""
    ops = {""} | ({operator} if operator else set())
    eligible = [c for c in candidates if c["operator"] in ops]
    if not eligible:
        return None
    return max(eligible, key=lambda r: (len(r["prefix"]), r["operator"] != ""))


def select_version(versions: list[dict], at: datetime) -> tuple[dict | None, str]:
    """Versioni efektiv në `at`: ai me `effective_from` më të vonë ≤ at. → (version|None, arsyeja kur None).
    `retired` ⇒ (None, "retired"): s'zgjidhet kurrë, dhe NUK bie kthim në një version më të vjetër (fail-closed)."""
    at = at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)
    best = None
    for v in versions:
        eff = datetime.fromisoformat(v["effective_from"])
        if eff <= at and (best is None or eff > datetime.fromisoformat(best["effective_from"])):
            best = v
    if best is None:
        return None, "no_version"
    if best["status"] != "active":
        return None, "retired"
    return best, ""


def select_assignment(assignments: list[dict], product_id: str, at: datetime) -> dict | None:
    at = at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)
    best = None
    for a in assignments:
        if a["product_id"] != product_id:
            continue
        eff = datetime.fromisoformat(a["effective_from"])
        if eff <= at and (best is None or eff > datetime.fromisoformat(best["effective_from"])):
            best = a
    return best
