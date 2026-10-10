"""Bootstrap/rakordim manual i assignment-eve Enterprise↔Product nga prova legacy (M7-f).

    ENTERPRISE_DATABASE_URL=... CENTRAL_DATABASE_URL=... \\
        python -m apps.central.tools.bootstrap_enterprise_products \\
            [--apply] [--review-file approved.json] [--format text|json] [--output raport.json]

DRY-RUN është parazgjedhja (zero shkrime); `--apply` shkruan. Enterprise DB lexohet vetëm-lexim
(transaksion READ ONLY / `query_only`), SQL minimal, pa ORM të Enterprise; shkruhet vetëm Central.
Asnjë shkrim te AccountPlan/owner_ref/Enterprise, asnjë fshirje, asnjë mbishkrim, asnjë Product
ose Enterprise i krijuar tërthorazi (M4-c krijon Enterprise; Product-et duhet të ekzistojnë me kod
`sms` dhe `email`). Asnjë URL/sekret në dalje.

Rregullat (miratuara):
  SMS   confirmed = AccountPlan + (SenderId i miratuar OSE ≥1 mesazh SMS); active/suspended nga
        AccountPlan.enabled. AccountPlan pa evidencë SMS ⇒ review (objekt legacy i përbashkët).
        Pa AccountPlan ⇒ no_evidence.
  Email confirmed = ≥1 EmailDomain verified + AccountPlan ekziston DHE enabled ⇒ active. Domen i
        verifikuar por AccountPlan mungon/disabled ⇒ review (kill-switch i përbashkët: s'nxirret
        gjendje suspended). Pa domen të verifikuar: Subscription aktive me included_emails>0 OSE
        histori email ⇒ review. Përndryshe no_evidence.
  Vetëm `confirmed` aplikohet vetë. `review` aplikohet vetëm me `--review-file` (enterprise_id,
  product_code, approved_status, evidence_hash); hash-i rillogaritet gjatë apply: evidencë e
  ndryshuar ⇒ approval i vjetruar ⇒ asgjë s'shkruhet.

Veprimet: create · noop · conflict · review · invalid · central_only (+ none = pa evidencë).
Plani është i plotë para çdo shkrimi; invalid/conflict/stale ⇒ ZERO shkrime (kodi 1). Apply = një
transaksion Central (all-or-nothing) me shërbimin normal (revision + outbox) dhe audit `system`
(`system:enterprise_product_bootstrap`) në të njëjtin transaksion; një assignment `suspended`
krijohet si assign (rev 1 active) + suspend (rev 2) në po atë commit. noop/conflict/review/invalid
nuk shkruajnë asgjë (as outbox, as audit). Kodet: 0 ok, 1 konflikte/invalid/stale, 2 konfigurim.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.models.enterprise import Enterprise, EnterpriseStatus
from apps.central.models.enterprise_product import EnterpriseProduct
from apps.central.models.product import Product, ProductStatus
from apps.central.services import audit
from apps.central.services import enterprise_products as asg

ACTOR_LABEL = "system:enterprise_product_bootstrap"
CODES = ("sms", "email")  # kodet e qëndrueshme të Product (jo UUID të ngurtësuara)
ACTIVE, SUSPENDED = "active", "suspended"
CONFIRMED, REVIEW, NO_EVIDENCE = "confirmed", "review", "no_evidence"
CREATE, NOOP, CONFLICT, INVALID, CENTRAL_ONLY, NONE = (
    "create", "noop", "conflict", "invalid", "central_only", "none",
)  # fmt: skip
_HASH = re.compile(r"^[0-9a-f]{64}$")


class ReviewFileError(ValueError):
    pass


# --- burimi (vetëm-lexim) ------------------------------------------------------------------------

_MAIN = text(
    """
    select e.id, e.owner_ref, e.status,
           p.id is not null as has_plan, p.enabled as plan_enabled, p.enterprise_id as plan_eid,
           exists(select 1 from sms_sender_ids s
                  where s.owner_ref = e.owner_ref and lower(s.status) = 'approved') as sender_ok,
           exists(select 1 from sms_messages m where m.owner_ref = e.owner_ref) as sms_history,
           exists(select 1 from sms_emails x where x.owner_ref = e.owner_ref) as email_history,
           exists(select 1 from sms_subscriptions s join sms_plans sp on sp.id = s.plan_id
                  where s.owner_ref = e.owner_ref and lower(s.status) = 'active'
                    and sp.included_emails > 0) as sub_email
    from sms_enterprises e left join sms_account_plans p on p.owner_ref = e.owner_ref
    order by e.created_at, e.id
    """  # noqa: S608
)
_DOMAINS = text(
    "select owner_ref, domain, enterprise_id from sms_email_domains where lower(status) = 'verified'"
)
_ENTITLEMENTS = text("select enterprise_id, channel, status from sms_entitlements")
_CP_REV = text("select id from sms_enterprises where cp_revision = 0")


@dataclass
class SourceRow:
    id: uuid.UUID | None
    owner_ref: str | None
    status: str | None
    has_plan: bool
    plan_enabled: bool
    plan_eid: uuid.UUID | None
    sender_ok: bool
    sms_history: bool
    email_history: bool
    sub_email: bool
    verified_domains: tuple[str, ...] = ()
    domain_identity_mismatch: bool = False


@dataclass
class Source:
    rows: list[SourceRow]
    entitlements: list[tuple[uuid.UUID, str, str]] = field(default_factory=list)
    never_synced: int | None = None  # None = kolona cp_revision s'ekziston (para M7-d)


def _uuid(v) -> uuid.UUID | None:
    if v is None or isinstance(v, uuid.UUID):
        return v
    try:
        return uuid.UUID(str(v))
    except ValueError:
        return None


def read_source(enterprise_url: str) -> Source:
    """Lexim vetëm-lexim: READ ONLY në PostgreSQL, `query_only` në SQLite."""
    engine = create_engine(enterprise_url)
    try:
        with engine.connect() as conn:
            if engine.dialect.name == "postgresql":
                conn.execute(text("SET TRANSACTION READ ONLY"))
            else:
                conn.execute(text("PRAGMA query_only = ON"))
            tables = set(inspect(conn).get_table_names())
            need = {
                "sms_enterprises", "sms_account_plans", "sms_sender_ids", "sms_messages",
                "sms_emails", "sms_subscriptions", "sms_plans", "sms_email_domains",
            }  # fmt: skip
            if need - tables:
                raise RuntimeError(
                    f"source is not an Enterprise DB (missing {sorted(need - tables)})"
                )
            rows = []
            for r in conn.execute(_MAIN):
                rows.append(SourceRow(
                    _uuid(r[0]), r[1], r[2], bool(r[3]), bool(r[4]), _uuid(r[5]), bool(r[6]),
                    bool(r[7]), bool(r[8]), bool(r[9]),
                ))  # fmt: skip
            by_owner = {r.owner_ref: r for r in rows if isinstance(r.owner_ref, str)}
            doms: dict[str, list[str]] = {}
            for owner, domain, eid in conn.execute(_DOMAINS):
                row = by_owner.get(owner)
                if row is None:
                    continue
                doms.setdefault(owner, []).append(str(domain).lower())
                if eid is not None and _uuid(eid) != row.id:
                    row.domain_identity_mismatch = True
            for owner, ds in doms.items():
                by_owner[owner].verified_domains = tuple(sorted(set(ds)))
            ents, never = [], None
            if "sms_entitlements" in tables:
                ents = [(_uuid(a), b, c) for a, b, c in conn.execute(_ENTITLEMENTS)]
                never = len(conn.execute(_CP_REV).all())
            return Source(rows, ents, never)
    finally:
        engine.dispose()


# --- klasifikimi (i pastër) -----------------------------------------------------------------------


@dataclass
class Item:
    enterprise_id: str | None
    owner_ref: str | None
    product_code: str
    classification: str  # confirmed | review | no_evidence | invalid
    recommended_status: str | None
    reason: str
    facts: dict
    evidence_hash: str | None = None
    central_status: str | None = None
    action: str = NONE
    approved_status: str | None = None  # nga review-file
    detail: str = ""

    @property
    def target_status(self) -> str | None:
        return self.approved_status or (
            self.recommended_status if self.classification == CONFIRMED else None
        )

    def to_dict(self) -> dict:
        return {
            "enterprise_id": self.enterprise_id, "owner_ref": self.owner_ref,
            "product_code": self.product_code, "classification": self.classification,
            "recommended_status": self.recommended_status, "reason": self.reason,
            "evidence": self.facts, "evidence_hash": self.evidence_hash,
            "central_status": self.central_status, "action": self.action,
            "approved_status": self.approved_status, "detail": self.detail,
        }  # fmt: skip


def _hash(eid: str, code: str, cls: str, rec: str | None, facts: dict) -> str:
    blob = json.dumps(
        {"enterprise_id": eid, "product_code": code, "classification": cls,
         "recommended_status": rec, "facts": facts},
        sort_keys=True, separators=(",", ":"),
    )  # fmt: skip
    return hashlib.sha256(blob.encode()).hexdigest()


def classify_sms(r: SourceRow) -> tuple[str, str | None, str, dict]:
    facts = {"has_account_plan": r.has_plan, "plan_enabled": r.plan_enabled,
             "approved_sender_id": r.sender_ok, "sms_history": r.sms_history}  # fmt: skip
    if not r.has_plan:
        return NO_EVIDENCE, None, "no AccountPlan", facts
    status = ACTIVE if r.plan_enabled else SUSPENDED
    if r.sender_ok or r.sms_history:
        return CONFIRMED, status, "AccountPlan + SMS-specific evidence", facts
    return REVIEW, status, "AccountPlan without SMS-specific evidence (shared legacy object)", facts


def classify_email(r: SourceRow) -> tuple[str, str | None, str, dict]:
    facts = {"verified_domains": list(r.verified_domains), "has_account_plan": r.has_plan,
             "plan_enabled": r.plan_enabled, "active_subscription_with_included_emails": r.sub_email,
             "email_history": r.email_history}  # fmt: skip
    if r.verified_domains:
        if r.has_plan and r.plan_enabled:
            return CONFIRMED, ACTIVE, "verified EmailDomain + enabled AccountPlan", facts
        why = "AccountPlan missing" if not r.has_plan else "AccountPlan.enabled=false"
        return (REVIEW, None,
                f"verified EmailDomain but {why}: shared kill-switch gives no clean email lifecycle",
                facts)  # fmt: skip
    if r.sub_email or r.email_history:
        return (REVIEW, None, "email evidence without a verified EmailDomain", facts)
    return NO_EVIDENCE, None, "no email evidence", facts


# --- plani ---------------------------------------------------------------------------------------


@dataclass
class Approval:
    enterprise_id: str
    product_code: str
    approved_status: str
    evidence_hash: str


def load_review_file(path: str) -> list[Approval]:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        raise ReviewFileError(f"cannot read review file: {type(e).__name__}") from None
    if not isinstance(doc, dict) or doc.get("version") != 1 or set(doc) != {"version", "approvals"}:
        raise ReviewFileError('review file must be {"version": 1, "approvals": [...]}')
    out, seen = [], set()
    for a in doc["approvals"] if isinstance(doc["approvals"], list) else []:
        if not isinstance(a, dict) or set(a) != {
            "enterprise_id",
            "product_code",
            "approved_status",
            "evidence_hash",
        }:
            raise ReviewFileError("each approval needs exactly enterprise_id, product_code, "
                                  "approved_status, evidence_hash")  # fmt: skip
        eid = _uuid(a["enterprise_id"])
        if eid is None or a["product_code"] not in CODES:
            raise ReviewFileError("approval has an invalid enterprise_id or product_code")
        if a["approved_status"] not in (ACTIVE, SUSPENDED):
            raise ReviewFileError("approved_status must be 'active' or 'suspended'")
        if not isinstance(a["evidence_hash"], str) or not _HASH.match(a["evidence_hash"]):
            raise ReviewFileError("evidence_hash must be a sha256 hex digest")
        key = (str(eid), a["product_code"])
        if key in seen:
            raise ReviewFileError("duplicate approval for the same enterprise/product")
        seen.add(key)
        out.append(Approval(str(eid), a["product_code"], a["approved_status"], a["evidence_hash"]))
    if not isinstance(doc["approvals"], list):
        raise ReviewFileError("approvals must be an array")
    return out


@dataclass
class Report:
    items: list[Item] = field(default_factory=list)
    scanned: int = 0
    central_only: list[dict] = field(default_factory=list)
    stale_reviews: list[dict] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    readiness: dict = field(default_factory=dict)
    apply: bool = False
    written: int = 0
    apply_error: str | None = None

    def count(self, action: str) -> int:
        return sum(1 for i in self.items if i.action == action)

    @property
    def ok(self) -> bool:
        return not (self.count(CONFLICT) or self.count(INVALID) or self.stale_reviews
                    or self.apply_error)  # fmt: skip

    @property
    def to_create(self) -> list[Item]:
        return sorted((i for i in self.items if i.action == CREATE),
                      key=lambda i: (i.enterprise_id, i.product_code))  # fmt: skip

    def counts(self) -> dict:
        c = Counter((i.product_code, i.classification) for i in self.items
                    if i.classification in (CONFIRMED, REVIEW, NO_EVIDENCE))  # fmt: skip
        return {
            "scanned_enterprises": self.scanned,
            "sms_confirmed": c[("sms", CONFIRMED)], "sms_review": c[("sms", REVIEW)],
            "email_confirmed": c[("email", CONFIRMED)], "email_review": c[("email", REVIEW)],
            "create": self.count(CREATE), "matching": self.count(NOOP),
            "conflicts": self.count(CONFLICT), "invalid": self.count(INVALID),
            "review": self.count(REVIEW), "central_only": len(self.central_only),
            "no_evidence": sum(v for (_, k), v in c.items() if k == NO_EVIDENCE),
            "stale_reviews": len(self.stale_reviews),
        }  # fmt: skip

    def to_dict(self) -> dict:
        return {
            "mode": "apply" if self.apply else "dry-run", "written": self.written,
            "ok": self.ok, "counts": self.counts(), "preconditions": self.preconditions,
            "items": [i.to_dict() for i in self.items if i.action != NONE],
            "central_only": self.central_only, "stale_reviews": self.stale_reviews,
            "readiness": self.readiness, "apply_error": self.apply_error,
        }  # fmt: skip

    def render(self) -> str:
        c = self.counts()
        out = [
            f"Scanned enterprises: {c['scanned_enterprises']}",
            f"SMS confirmed: {c['sms_confirmed']} (review: {c['sms_review']})",
            f"Email confirmed: {c['email_confirmed']} (review: {c['email_review']})",
            f"Create: {c['create']}", f"Matching: {c['matching']}", f"Conflicts: {c['conflicts']}",
            f"Invalid: {c['invalid']}", f"Review: {c['review']}",
            f"Central-only (untouched): {c['central_only']}", f"No-evidence: {c['no_evidence']}",
            f"Stale review approvals: {c['stale_reviews']}",
            f"Mode: {'apply' if self.apply else 'dry-run (0 writes)'}; written: {self.written}",
        ]  # fmt: skip
        out += [f"PRECONDITION {p}" for p in self.preconditions]
        for i in sorted((x for x in self.items if x.action != NONE),
                        key=lambda x: (x.action, x.enterprise_id or "", x.product_code)):  # fmt: skip
            out.append(
                f"{i.action.upper():12} enterprise_id={i.enterprise_id} owner_ref={i.owner_ref!r} "
                f"product={i.product_code} class={i.classification} recommended={i.recommended_status}"
                f" central={i.central_status} evidence_hash={(i.evidence_hash or '-')[:16]} "
                f"reason={i.reason}{' | ' + i.detail if i.detail else ''}"
            )
        for o in self.central_only:
            out.append(f"CENTRAL_ONLY enterprise_id={o['enterprise_id']} product={o['product_code']} "
                       f"status={o['status']} reason={o['reason']}")  # fmt: skip
        for s in self.stale_reviews:
            out.append(f"STALE_REVIEW enterprise_id={s['enterprise_id']} product={s['product_code']} "
                       f"reason={s['reason']}")  # fmt: skip
        if self.apply_error:
            out.append(f"APPLY_ERROR {self.apply_error}")
        r = self.readiness
        if r:
            out.append(
                "Shadow readiness ("
                + ("projected after apply" if r.get("projected") else "current")
                + "):"
            )
            for ch in ("sms", "email"):
                v = r["channels"][ch]
                out.append(f"  {ch}: " + " ".join(f"{k}={n}" for k, n in v["counts"].items())
                           + f" unexplained={v['unexplained']}")  # fmt: skip
            out.append(f"  unknown_local_enterprise={r['unknown_local_enterprise']} "
                       f"withdrawn_local={r['withdrawn_local']} never_synced_local={r['never_synced_local']}")  # fmt: skip
        return "\n".join(out)


def build_plan(src: Source, central: Session, approvals: list[Approval] | None = None) -> Report:
    rep = Report(scanned=len(src.rows))
    approvals = approvals or []
    products = {p.code: p for p in central.scalars(select(Product).where(Product.code.in_(CODES)))}
    bad_product: dict[str, str] = {}
    for code in CODES:
        p = products.get(code)
        if p is None:
            bad_product[code] = (
                f"Product code {code!r} does not exist in Central (not created implicitly)"
            )
        elif p.status != ProductStatus.ACTIVE.value:
            bad_product[code] = f"Product {code!r} is not active in Central"
        elif p.channel != code:
            bad_product[code] = f"Product {code!r} has unexpected channel {p.channel!r}"
    rep.preconditions = sorted(bad_product.values())
    c_ents = {e.id: e for e in central.scalars(select(Enterprise))}
    c_asg: dict[tuple[uuid.UUID, str], tuple[EnterpriseProduct, Product]] = {}
    code_of: dict[uuid.UUID, str] = {}
    for ep, p in central.execute(
        select(EnterpriseProduct, Product).join(Product, Product.id == EnterpriseProduct.product_id)
    ):
        c_asg[(ep.enterprise_id, p.code)] = (ep, p)
        code_of[p.id] = p.code
    ids = Counter(r.id for r in src.rows if r.id is not None)
    owners = Counter(r.owner_ref.strip().lower() for r in src.rows if isinstance(r.owner_ref, str))
    explained: set[tuple[uuid.UUID, str]] = set()
    by_key = {(a.enterprise_id, a.product_code): a for a in approvals}
    used_approvals: set[tuple[str, str]] = set()

    def invalid(r, reason, code="*"):
        rep.items.append(Item(str(r.id) if r.id else None, r.owner_ref, code, "invalid", None,
                              reason, {}, action=INVALID))  # fmt: skip

    for r in src.rows:
        if r.id is None:
            invalid(r, "missing/invalid enterprise id")
            continue
        if ids[r.id] > 1:
            invalid(r, "duplicate enterprise id in source")
            continue
        if not isinstance(r.owner_ref, str) or not r.owner_ref.strip():
            invalid(r, "missing owner_ref")
            continue
        if owners[r.owner_ref.strip().lower()] > 1:
            invalid(r, "duplicate owner_ref in source (case/space-insensitive)")
            continue
        if (r.plan_eid is not None and r.plan_eid != r.id) or r.domain_identity_mismatch:
            invalid(r, "enterprise_id mismatch between enterprise and its AccountPlan/EmailDomain")
            continue
        eid = str(r.id)
        for code, fn in (("sms", classify_sms), ("email", classify_email)):
            cls, rec, reason, facts = fn(r)
            it = Item(eid, r.owner_ref, code, cls, rec, reason, facts,
                      evidence_hash=_hash(eid, code, cls, rec, facts))  # fmt: skip
            rep.items.append(it)
            ce = c_asg.get((r.id, code))
            it.central_status = ce[0].status if ce else None
            if cls == NO_EVIDENCE:
                it.action = NONE
                continue
            explained.add((r.id, code))
            appr = by_key.get((eid, code))
            if appr is not None:
                used_approvals.add((eid, code))
                if cls != REVIEW or appr.evidence_hash != it.evidence_hash:
                    rep.stale_reviews.append({
                        "enterprise_id": eid, "product_code": code,
                        "reason": "evidence changed since the approval was prepared "
                                  f"(now {cls}, hash {it.evidence_hash[:12]})",
                    })  # fmt: skip
                    it.action, it.detail = REVIEW, "approval rejected as stale"
                    continue
                it.approved_status = appr.approved_status
            ent = c_ents.get(r.id)
            if ent is None:
                it.action, it.detail = (
                    INVALID,
                    "enterprise does not exist in Central (M4-c creates it)",
                )
                continue
            if code in bad_product:
                it.action, it.detail = INVALID, bad_product[code]
                continue
            target = it.target_status
            if target is None:  # review pa approval
                it.action = REVIEW
                if ce:
                    it.detail = f"Central already has status {ce[0].status}; not modified"
                continue
            if ce is None:
                if ent.status != EnterpriseStatus.ACTIVE.value:
                    it.action = CONFLICT
                    it.detail = "Central enterprise is suspended: assignment creation not allowed"
                else:
                    it.action = CREATE
            elif ce[0].status == target:
                it.action = NOOP
            else:
                it.action = CONFLICT
                it.detail = f"Central status {ce[0].status} differs from {target}; not overwritten"
    for key, a in by_key.items():
        if key not in used_approvals:
            rep.stale_reviews.append({"enterprise_id": a.enterprise_id, "product_code": a.product_code,
                                      "reason": "approval matches no source enterprise/product"})  # fmt: skip
    src_ids = {r.id for r in src.rows if r.id is not None}
    for (eid, code), (ep, p) in sorted(c_asg.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        if (eid, code) in explained:
            continue
        why = (
            "enterprise not in legacy source"
            if eid not in src_ids
            else "legacy source has no evidence for this product"
        )
        rep.central_only.append({"enterprise_id": str(eid), "product_code": p.code,
                                 "status": ep.status, "reason": why})  # fmt: skip
    rep.items.sort(key=lambda i: (i.enterprise_id or "", i.product_code))
    rep.readiness = _readiness(src, rep, c_ents, c_asg)
    return rep


def _readiness(src: Source, rep: Report, c_ents, c_asg) -> dict:
    """Projeksion i shadow për M7-g: vendimi legacy (AccountPlan.enabled) kundrejt CP (Enterprise
    Central + assignment, përfshirë ato që do krijohen). Mospërputhja pa shpjegim duhet të jetë 0."""
    planned = {(i.enterprise_id, i.product_code): i.target_status for i in rep.items
               if i.action == CREATE}  # fmt: skip
    by_item = {(i.enterprise_id, i.product_code): i for i in rep.items}
    channels = {}
    for ch in CODES:
        counts = Counter()
        unexplained = []
        for r in src.rows:
            if r.id is None or not r.has_plan:
                continue
            ent = c_ents.get(r.id)
            ce = c_asg.get((r.id, ch))
            status = ce[0].status if ce else planned.get((str(r.id), ch))
            if ent is None or status is None:
                cp = "missing"
            else:
                cp = "allow" if (ent.status == ACTIVE and status == ACTIVE) else "deny"
            legacy = "allow" if r.plan_enabled else "deny"
            key = "cp_missing" if cp == "missing" else f"legacy_{legacy}_cp_{cp}"
            counts[key] += 1
            if key in ("legacy_allow_cp_allow", "legacy_deny_cp_deny"):
                continue
            it = by_item.get((str(r.id), ch))
            # cp_missing shpjegohet nga mungesa e evidencës ose nga një item i raportuar; një
            # assignment Central pa evidencë legacy (central_only) me mospërputhje NUK shpjegohet.
            explained = it is not None and (
                it.action in (REVIEW, CONFLICT, INVALID)
                or (key == "cp_missing" and it.classification == NO_EVIDENCE)
            )
            if not explained:
                unexplained.append({"enterprise_id": str(r.id), "class": key})
        names = ("legacy_allow_cp_allow", "legacy_allow_cp_deny", "legacy_deny_cp_allow",
                 "legacy_deny_cp_deny", "cp_missing")  # fmt: skip
        channels[ch] = {"counts": {k: counts[k] for k in names}, "unexplained": len(unexplained),
                        "unexplained_items": unexplained}  # fmt: skip
    src_ids = {r.id for r in src.rows if r.id is not None}
    return {
        "projected": not rep.apply and bool(planned), "channels": channels,
        "unknown_local_enterprise": len(set(c_ents) - src_ids),
        "withdrawn_local": sum(1 for _, _, st in src.entitlements if st == "withdrawn"),
        "never_synced_local": src.never_synced,
    }  # fmt: skip


# --- apply ---------------------------------------------------------------------------------------


def _apply(db: Session, rep: Report, now: datetime) -> None:
    products = {p.code: p for p in db.scalars(select(Product).where(Product.code.in_(CODES)))}
    for it in rep.to_create:
        row, _ = asg.assign_product(db, it.enterprise_id, products[it.product_code].id, now=now)
        if it.target_status == SUSPENDED:
            asg.suspend_assignment(db, it.enterprise_id, row.id, now=now)
        audit.record_system(
            db, label=ACTOR_LABEL, action="enterprise_product.bootstrap",
            resource_type="enterprise_product", resource_id=row.id,
            detail={"enterprise_id": it.enterprise_id, "product_code": it.product_code,
                    "status": it.target_status, "classification": it.classification,
                    "approved": it.approved_status is not None, "evidence_hash": it.evidence_hash},
            now=now,
        )  # fmt: skip


def run(
    enterprise_url: str,
    central_url: str,
    *,
    apply: bool = False,
    approvals: list[Approval] | None = None,
    central_engine: Engine | None = None,
) -> Report:
    if enterprise_url == central_url:
        raise ValueError("ENTERPRISE_DATABASE_URL and CENTRAL_DATABASE_URL must differ")
    src = read_source(enterprise_url)
    engine = central_engine or make_engine(central_url)
    try:
        with Session(engine, expire_on_commit=False) as db:
            rep = build_plan(src, db, approvals)
            rep.apply = apply
            if not apply or not rep.ok or not rep.to_create:
                db.rollback()
                return rep
            try:
                _apply(db, rep, datetime.now(UTC))
                db.commit()  # një transaksion: all-or-nothing
                rep.written = len(rep.to_create)
                rep.readiness = _readiness(src, rep, *_central_state(db))
            except Exception as e:  # noqa: BLE001  (garë/konflikt gjatë apply ⇒ asgjë e shkruar)
                db.rollback()
                rep.apply_error = f"{type(e).__name__}: {str(e)[:200]}"
            return rep
    finally:
        if central_engine is None:
            engine.dispose()


def _central_state(db: Session):
    c_ents = {e.id: e for e in db.scalars(select(Enterprise))}
    c_asg = {(ep.enterprise_id, p.code): (ep, p) for ep, p in db.execute(
        select(EnterpriseProduct, Product).join(Product, Product.id == EnterpriseProduct.product_id))}  # fmt: skip
    return c_ents, c_asg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Bootstrap/reconcile Enterprise products into Central."
    )
    ap.add_argument(
        "--apply", action="store_true", help="shkruaj (parazgjedhja: dry-run, 0 shkrime)"
    )
    ap.add_argument("--dry-run", action="store_true", help="e njëjtë me parazgjedhjen")
    ap.add_argument("--review-file", help="JSON me approval-et e rishikuara nga njeriu")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--output", help="shkruaj raportin në skedar")
    args = ap.parse_args(argv)
    if args.apply and args.dry_run:
        print("--apply and --dry-run are mutually exclusive", file=sys.stderr)  # noqa: T201
        return 2
    ent = os.environ.get("ENTERPRISE_DATABASE_URL")
    if not ent:
        print("ENTERPRISE_DATABASE_URL is required", file=sys.stderr)  # noqa: T201
        return 2
    try:
        approvals = load_review_file(args.review_file) if args.review_file else None
        rep = run(ent, settings.database_url, apply=args.apply, approvals=approvals)
    except Exception as e:  # raport pa URL/sekrete
        print(f"bootstrap failed: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)  # noqa: T201
        return 2
    body = (
        json.dumps(rep.to_dict(), indent=1, sort_keys=True)
        if args.format == "json"
        else rep.render()
    )
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(body + "\n")
    print(body)  # noqa: T201
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
