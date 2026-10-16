"""M9-e: importi i tarifave ekzistuese të Enterprise në çmime Central (propozim → klasifikim → zbatim me miratim).

Hyrja është propozimi `pricing-bootstrap.v1` i eksportuar nga Enterprise (`scripts.pricing_bootstrap export`): libra (rate cards +
planet email), versione të publikuara me `effective_from` origjinal, rregulla, dhe caktime (owner_ref→enterprise_id→libër).
Klasifikimi (pa shkrim në dry-run): `exact` (i zbatueshëm ose tashmë i barabartë në Central) · `conflict` (kodi ekziston në
Central me përmbajtje/version tjetër) · `invalid` (kod/monedhë/prefiks/çmim i pavlefshëm) · `unmapped` (enterprise pa identitet
Central ose libër i papërdorshëm). Zbatohen VETËM `exact`; asgjë s'fshihet/zhvendoset në Enterprise; versionet historike importohen
`imported=True` (lejohet `effective_from` në të kaluarën, rendi mbetet i rreptë). Aktivizimi në Enterprise bëhet më vonë me `shadow`."""

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.core.errors import CentralError
from apps.central.models.enterprise import Enterprise
from apps.central.models.pricing import PriceBook, PriceVersion
from apps.central.models.product import Product
from apps.central.services import pricing
from packages.contracts.control_plane.pricing import v1 as pv

SCHEMA = "pricing-bootstrap.v1"
EXACT, CONFLICT, INVALID, UNMAPPED = "exact", "conflict", "invalid", "unmapped"


@dataclass(slots=True)
class Item:
    kind: str  # book | version | assignment
    ref: str
    classification: str
    reason: str = ""
    applied: bool = False


@dataclass(slots=True)
class Report:
    proposal_hash: str
    items: list[Item] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        c = dict.fromkeys((EXACT, CONFLICT, INVALID, UNMAPPED), 0)
        for i in self.items:
            c[i.classification] += 1
        return c

    def to_dict(self) -> dict:
        return {"proposal_hash": self.proposal_hash, "counts": self.counts(),
                "items": [vars(i) | {} for i in self.items]}  # fmt: skip


def proposal_hash(proposal: dict) -> str:
    return hashlib.sha256(
        json.dumps(proposal, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _valid_rule(r: dict) -> str | None:
    try:
        ch, prefix, op = r["channel"], r.get("prefix", ""), r.get("operator", "")
        if ch not in pv.CHANNELS:
            return "bad channel"
        if ch == "sms" and not pv._PREFIX.match(prefix):
            return f"bad prefix {prefix!r}"
        if ch == "email" and (prefix or op):
            return "email rule must have empty prefix/operator"
        if not pv._OPERATOR.match(op):
            return f"bad operator {op!r}"
        pricing.price(r["unit_price"])
    except (KeyError, TypeError) as e:
        return f"malformed rule: {e!r}"
    except CentralError as e:
        return str(e)
    return None


def _parse_ts(v) -> datetime:
    return datetime.fromisoformat(v)


def _content_hash(rules: list[dict]) -> str:
    return pv.rules_hash([{"rule_id": "x", "channel": r["channel"], "prefix": r.get("prefix", ""),
                           "operator": r.get("operator", ""), "unit_price": pv.format_price(Decimal(str(r["unit_price"])))}
                          for r in sorted(rules, key=lambda r: (r["channel"], r.get("prefix", ""), r.get("operator", "")))])  # fmt: skip


def _norm_hash(rules: list[dict]) -> str:
    """Hash i krahasueshëm pa rule_id (id-të janë të Central): (channel, prefix, operator, price) të renditura."""
    rows = sorted((r["channel"], r.get("prefix", ""), r.get("operator", ""), pv.format_price(Decimal(str(r["unit_price"]))))
                  for r in rules)  # fmt: skip
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _central_norm(db: Session, version: PriceVersion) -> str:
    return _norm_hash([{"channel": r["channel"], "prefix": r["prefix"], "operator": r["operator"], "unit_price": r["unit_price"]}
                       for r in pricing.rules_of(db, version.id)])  # fmt: skip


def classify_and_apply(db: Session, proposal: dict, *, actor=None, apply: bool = False, sms_product_code: str = "sms",
                       email_product_code: str = "email", now: datetime | None = None) -> Report:  # fmt: skip
    if proposal.get("schema") != SCHEMA:
        raise ValueError(f"unsupported proposal schema {proposal.get('schema')!r}")
    rep = Report(proposal_hash(proposal))
    ok_books: dict[str, dict] = {}
    for b in proposal.get("books", []):
        code = b.get("code", "?")
        bad = None
        try:
            if not pv._CODE.match(code):
                bad = "bad book code"
            elif (
                not isinstance(b.get("currency"), str)
                or len(b["currency"]) != 3
                or b["currency"] != b["currency"].upper()
            ):
                bad = "bad currency"
            elif not b.get("versions"):
                bad = "no versions"
            else:
                last = None
                for v in b["versions"]:
                    eff = _parse_ts(v["effective_from"])
                    if last is not None and eff <= last:
                        bad = "versions are not strictly ordered by effective_from"
                    last = eff
                    for r in v.get("rules", []):
                        bad = bad or _valid_rule(r)
                    if not v.get("rules"):
                        bad = bad or "empty version"
        except (KeyError, TypeError, ValueError) as e:
            bad = f"malformed: {e!r}"
        if bad:
            rep.items.append(Item("book", code, INVALID, bad))
            continue
        existing = db.scalar(select(PriceBook).where(PriceBook.code == code))
        if existing is not None:
            if existing.currency != b["currency"]:
                rep.items.append(Item("book", code, CONFLICT, "book exists with another currency"))
                continue
            central = {pv.format_ts(v.effective_from): _central_norm(db, v)
                       for v in db.scalars(select(PriceVersion).where(PriceVersion.price_book_id == existing.id, PriceVersion.status != "draft"))}  # fmt: skip
            conflict = None
            for v in b["versions"]:
                key = pv.format_ts(_parse_ts(v["effective_from"]))
                if key in central and central[key] != _norm_hash(v["rules"]):
                    conflict = f"version at {key} differs from Central"
            only_extra = [
                k
                for k in central
                if k not in {pv.format_ts(_parse_ts(v["effective_from"])) for v in b["versions"]}
            ]
            if conflict:
                rep.items.append(Item("book", code, CONFLICT, conflict))
                continue
            if only_extra:
                rep.items.append(
                    Item("book", code, CONFLICT, "Central has versions that the proposal does not")
                )
                continue
            rep.items.append(Item("book", code, EXACT, "already present and identical"))
            ok_books[code] = {**b, "_exists": True}
            continue
        rep.items.append(Item("book", code, EXACT, f"new: {len(b['versions'])} version(s)"))
        ok_books[code] = {**b, "_exists": False}
    sms = db.scalar(select(Product).where(Product.code == sms_product_code))
    email = db.scalar(select(Product).where(Product.code == email_product_code))
    plan_assign: list[tuple[dict, dict, Product]] = []
    for a in proposal.get("assignments", []):
        ref = f"{a.get('owner_ref')}->{a.get('book_code')}"
        book = ok_books.get(a.get("book_code"))
        prod = email if a.get("channel") == "email" else sms
        if book is None:
            rep.items.append(
                Item(
                    "assignment",
                    ref,
                    UNMAPPED,
                    "book is not importable (missing, invalid or conflicting)",
                )
            )
        elif not a.get("enterprise_id"):
            rep.items.append(Item("assignment", ref, UNMAPPED, "owner has no Enterprise identity"))
        elif prod is None:
            rep.items.append(Item("assignment", ref, UNMAPPED, "product not found in Central"))
        else:
            try:
                eid = uuid.UUID(a["enterprise_id"])
            except ValueError:
                rep.items.append(Item("assignment", ref, INVALID, "bad enterprise_id"))
                continue
            if db.get(Enterprise, eid) is None:
                rep.items.append(
                    Item("assignment", ref, UNMAPPED, "enterprise is unknown to Central")
                )
            else:
                rep.items.append(Item("assignment", ref, EXACT, "ready"))
                plan_assign.append((a, book, prod))
    if not apply:
        return rep
    if actor is None:
        raise ValueError("an admin actor is required to apply")
    now = now or datetime.now().astimezone()
    for code, b in ok_books.items():
        if b["_exists"]:
            continue
        book = pricing.create_book(db, actor, code, b.get("name") or code, b["currency"], now=now)
        for v in b["versions"]:
            draft = pricing.new_draft(db, actor, book.id, now=now)
            for r in v["rules"]:
                pricing.set_rule(db, actor, draft.id, r["channel"], r["unit_price"], prefix=r.get("prefix", ""),
                                 operator=r.get("operator", ""), now=now)  # fmt: skip
            pricing.activate(
                db, actor, draft.id, _parse_ts(v["effective_from"]), now=now, imported=True
            )
        for i in rep.items:
            if i.kind == "book" and i.ref == code:
                i.applied = True
    for a, b, prod in plan_assign:
        first = min(_parse_ts(v["effective_from"]) for v in b["versions"])
        book = db.scalar(select(PriceBook).where(PriceBook.code == b["code"]))
        pricing.assign(
            db,
            actor,
            uuid.UUID(a["enterprise_id"]),
            prod.id,
            book.id,
            first,
            now=now,
            imported=True,
        )
        for i in rep.items:
            if i.kind == "assignment" and i.ref == f"{a.get('owner_ref')}->{a.get('book_code')}":
                i.applied = True
    return rep
