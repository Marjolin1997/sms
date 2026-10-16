"""M10-S1: politika e shtetit për sender-a, regjistri global dhe vendimet (autoriteti Central). Pa transport, pa Enterprise, pa rrjet.

Parimet:
- Politika është APPEND-ONLY dhe hyn në fuqi në çastin e krijimit; pa rresht ⇒ parazgjedhja `allowed=true, requires_approval=true` (burim `default`, pa revizion).
- Çdo ndryshim i gjendjes = projeksioni i regjistrit + rreshti i vendimit + audit, në transaksionin e thirrësit (pa commit këtu). Vendimi ruan politikën e saktë të përdorur.
- Renditja e kyçeve (kundër deadlock): (1) kyç advisory i fushës (shtet, lloj) — SHARED për vendime, EXCLUSIVE për ndryshim politike; (2) rreshtat e regjistrit `FOR UPDATE`.
  Kështu një miratim nuk kalon kurrë me politikë të vjetruar: ose merr revizionin e ri, ose politika e re pret dhe pastaj revokon miratimin.
- `allowed=true→false`: ndryshimi i politikës revokon ATOMIKISHT (vendim `revoked`, aktor `system:sender-policy`, kategori `policy_revoked`) çdo sender të miratuar në fushë."""

import functools
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.central.core.errors import Conflict, Invalid, NotFound
from apps.central.core.timeutil import utcnow
from apps.central.models.enterprise import Enterprise
from apps.central.models.sender import CountrySenderPolicy, SenderDecision, SenderRegistry
from apps.central.models.user import CentralUser
from apps.central.services import audit, money_common, sender_sync
from apps.central.services import sender_identity as ident

SYSTEM_POLICY = "system:sender-policy"
_REF = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
EVIDENCE_MAX = 128


def _utc(dt: datetime | None) -> datetime:
    from apps.central.services.billing import utc

    return utc(dt or utcnow())


def _publishing(fn):
    """Publikon (outbox `cp.sender.v1`) gjendjet e ndryshuara NË FUND të mutacionit, në të njëjtin transaksion; gabim ⇒ asgjë s'mbetet e mbledhur."""

    @functools.wraps(fn)
    def wrapper(db, *a, **kw):
        sender_sync.discard(db)
        try:
            out = fn(db, *a, **kw)
            sender_sync.publish(db)
        except BaseException:
            sender_sync.discard(db)
            raise
        return out

    return wrapper


@dataclass(frozen=True, slots=True)
class PolicyView:
    country: str
    sender_kind: str
    source: str  # explicit | default
    allowed: bool
    requires_approval: bool
    policy_id: uuid.UUID | None = None
    revision: int | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None


@dataclass(frozen=True, slots=True)
class PolicyChange:
    policy: CountrySenderPolicy
    created: bool
    revoked: int


@dataclass(frozen=True, slots=True)
class RequestResult:
    sender: SenderRegistry
    created: bool
    auto: str  # not_applicable | approved | blocked | denied


# --- kyçet ------------------------------------------------------------------------------------------------------------------------


def _scope_lock(db: Session, country: str, kind: str, *, exclusive: bool) -> None:
    if db.get_bind().dialect.name != "postgresql":
        return
    key = int.from_bytes(
        hashlib.sha256(f"sender-policy:{country}:{kind}".encode()).digest()[:8], "big", signed=True
    )
    fn = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    db.execute(text(f"SELECT {fn}(:k)"), {"k": key})


# --- politika ---------------------------------------------------------------------------------------------------------------------


def _view(p: CountrySenderPolicy, nxt: datetime | None) -> PolicyView:
    return PolicyView(
        p.country,
        p.sender_kind,
        "explicit",
        p.allowed,
        p.requires_approval,
        p.id,
        p.revision,
        _utc(p.effective_from),
        None if nxt is None else _utc(nxt),
    )


def effective_policy(
    db: Session, country: str, kind: str, at: datetime | None = None
) -> PolicyView:
    """Politika e vlefshme për (shtet, lloj) në çastin `at` (parazgjedhje: tani). Asnjë thirrës s'duhet ta riprodhojë këtë precedencë."""
    country, kind = ident.country_code(country), ident.kind_of(kind)
    at = _utc(at)
    row = db.scalar(
        select(CountrySenderPolicy)
        .where(
            CountrySenderPolicy.country == country,
            CountrySenderPolicy.sender_kind == kind,
            CountrySenderPolicy.effective_from <= at,
        )
        .order_by(CountrySenderPolicy.effective_from.desc())
        .limit(1)
    )
    if row is None:
        return PolicyView(country, kind, "default", True, True)
    nxt = db.scalar(
        select(CountrySenderPolicy.effective_from)
        .where(
            CountrySenderPolicy.country == country,
            CountrySenderPolicy.sender_kind == kind,
            CountrySenderPolicy.effective_from > row.effective_from,
        )
        .order_by(CountrySenderPolicy.effective_from)
        .limit(1)
    )
    return _view(row, nxt)


def list_policies(
    db: Session, *, country=None, kind=None, latest_only=False, limit=50, offset=0
) -> list[CountrySenderPolicy]:
    q = select(CountrySenderPolicy)
    if country:
        q = q.where(CountrySenderPolicy.country == ident.country_code(country))
    if kind:
        q = q.where(CountrySenderPolicy.sender_kind == ident.kind_of(kind))
    if latest_only:
        latest = (
            select(
                CountrySenderPolicy.country,
                CountrySenderPolicy.sender_kind,
                func.max(CountrySenderPolicy.revision).label("r"),
            )
            .group_by(CountrySenderPolicy.country, CountrySenderPolicy.sender_kind)
            .subquery()
        )
        q = q.join(
            latest,
            (latest.c.country == CountrySenderPolicy.country)
            & (latest.c.sender_kind == CountrySenderPolicy.sender_kind)
            & (latest.c.r == CountrySenderPolicy.revision),
        )
    return list(
        db.scalars(
            q.order_by(
                CountrySenderPolicy.country,
                CountrySenderPolicy.sender_kind,
                CountrySenderPolicy.revision,
            )
            .limit(limit + 1)
            .offset(offset)
        )
    )


def get_policy(db: Session, policy_id) -> CountrySenderPolicy:
    row = db.get(CountrySenderPolicy, money_common.uid(policy_id, "policy id"))
    if row is None:
        raise NotFound("sender policy not found")
    return row


@_publishing
def set_policy(
    db: Session,
    actor,
    country,
    kind,
    allowed,
    requires_approval,
    reason,
    *,
    now: datetime | None = None,
) -> PolicyChange:
    """Revizion i ri (hyn në fuqi tani). I njëjti përmbajtje si revizioni aktual ⇒ asnjë revizion i ri. `allowed=false` revokon atomikisht miratimet e fushës."""
    actor = money_common.admin(actor)
    why = money_common.reason(reason)
    country, kind = ident.country_code(country), ident.kind_of(kind)
    if not isinstance(allowed, bool) or not isinstance(requires_approval, bool):
        raise Invalid("allowed and requires_approval must be booleans")
    if not allowed and not requires_approval:
        raise Invalid(
            "a country/kind that is not allowed must keep requires_approval=true (no silent coercion)"
        )
    _scope_lock(db, country, kind, exclusive=True)
    now = _utc(
        now
    )  # pas kyçit: një revizion paralel i commit-uar para nesh ka `effective_from` më të hershëm se ora jonë
    latest = db.scalar(
        select(CountrySenderPolicy)
        .where(CountrySenderPolicy.country == country, CountrySenderPolicy.sender_kind == kind)
        .order_by(CountrySenderPolicy.revision.desc())
        .limit(1)
    )
    if latest is not None and (latest.allowed, latest.requires_approval) == (
        allowed,
        requires_approval,
    ):
        return PolicyChange(latest, False, 0)
    if latest is not None and now <= _utc(latest.effective_from):
        raise Conflict("a newer policy revision already took effect at this instant; retry")
    rev = 1 if latest is None else latest.revision + 1
    content = json.dumps(
        {
            "country": country,
            "kind": kind,
            "revision": rev,
            "allowed": allowed,
            "requires_approval": requires_approval,
        },
        sort_keys=True,
    )
    row = CountrySenderPolicy(
        id=uuid.uuid4(), country=country, sender_kind=kind, revision=rev, allowed=allowed, requires_approval=requires_approval, effective_from=now,
        reason=why, content_hash=hashlib.sha256(content.encode()).hexdigest(), created_by_id=actor.id, created_at=now,
    )  # fmt: skip
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as e:
        raise Conflict(
            "a concurrent policy revision was created for this country and sender kind"
        ) from e
    sender_sync.note_policy(db, row)
    audit.record(
        db,
        actor,
        "sender_policy.create",
        "sender_policy",
        row.id,
        {
            "country": country,
            "sender_kind": kind,
            "revision": rev,
            "allowed": allowed,
            "requires_approval": requires_approval,
        },
        now=now,
    )
    revoked = 0
    if not allowed:
        view = _view(row, None)
        for s in db.scalars(
            select(SenderRegistry)
            .where(
                SenderRegistry.country == country,
                SenderRegistry.sender_kind == kind,
                SenderRegistry.current_status == "approved",
            )
            .order_by(SenderRegistry.id)
            .with_for_update()
        ):
            _append(db, s, "revoked", "revoked", SYSTEM_POLICY, category="policy_revoked", pol=view, now=now,
                    reason=f"policy revision {rev} no longer allows {kind} senders in {country}", evidence_ref=None)  # fmt: skip
            revoked += 1
    return PolicyChange(row, True, revoked)


# --- regjistri --------------------------------------------------------------------------------------------------------------------


def _who(actor) -> tuple[uuid.UUID | None, str | None]:
    if isinstance(actor, CentralUser):
        return money_common.admin(actor).id, None
    return None, money_common.system_label(actor)


def _audit(db: Session, actor, action: str, row: SenderRegistry, d: SenderDecision) -> None:
    detail = {
        "enterprise_id": str(row.enterprise_id),
        "country": row.country,
        "decision_id": str(d.id),
        "policy_revision": d.policy_revision,
        "policy_source": d.policy_source,
        "category": d.category,
    }
    if isinstance(actor, CentralUser):
        audit.record(db, actor, action, "sender", row.id, detail, now=d.decided_at)
    else:
        audit.record_system(
            db,
            label=actor,
            action=action,
            resource_type="sender",
            resource_id=row.id,
            detail=detail,
            now=d.decided_at,
        )


_ACTION = {
    "requested": "sender.request",
    "approved": "sender.approve",
    "rejected": "sender.reject",
    "revoked": "sender.revoke",
    "resubmitted": "sender.resubmit",
}


def _append(
    db,
    row: SenderRegistry,
    decision: str,
    to_status: str,
    actor,
    *,
    category: str,
    reason,
    evidence_ref,
    pol: PolicyView,
    now: datetime,
    new: bool = False,
) -> SenderDecision:
    by_id, label = _who(actor)
    seq = (
        1
        if new
        else (
            db.scalar(
                select(func.max(SenderDecision.seq)).where(SenderDecision.registry_id == row.id)
            )
            or 0
        )
        + 1
    )
    d = SenderDecision(
        id=row.current_decision_id if new else uuid.uuid4(), registry_id=row.id, seq=seq, decision=decision, from_status=None if new else row.current_status,
        to_status=to_status, category=category, decided_at=now, decided_by_id=by_id, actor_label=label, reason=reason, evidence_ref=evidence_ref,
        policy_source=pol.source, policy_id=pol.policy_id, policy_revision=pol.revision, source="admin" if by_id else row.source, created_at=now,
    )  # fmt: skip
    if not new:
        row.current_status, row.current_decision_id, row.updated_at = to_status, d.id, now
        row.approved_key = (
            ident.canonical_key(row.country, row.norm_value) if to_status == "approved" else None
        )
    db.add(d)
    db.flush()
    sender_sync.note_registry(db, row, d)
    _audit(
        db,
        actor,
        _ACTION[decision]
        if category not in ("policy_revoked", "policy_auto_approved")
        else ("sender.policy_revoke" if category == "policy_revoked" else "sender.auto_approve"),
        row,
        d,
    )
    return d


def _evidence(value) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not (1 <= len(value.strip()) <= EVIDENCE_MAX)
        or re.search(r"[\x00-\x1f\x7f]", value)
    ):
        raise Invalid(f"evidence_ref must be 1..{EVIDENCE_MAX} printable characters")
    return value.strip()


def _load(db: Session, sender_id, *, lock_scope=True) -> SenderRegistry:
    """Kyç (1) fusha shared, pastaj (2) rreshti FOR UPDATE — gjithmonë kjo renditje."""
    sid = money_common.uid(sender_id, "sender id")
    row = db.get(SenderRegistry, sid)
    if row is None:
        raise NotFound("sender not found")
    if lock_scope:
        _scope_lock(db, row.country, row.sender_kind, exclusive=False)
    row = db.get(SenderRegistry, sid, with_for_update=True, populate_existing=True)
    return row


def get_sender(db: Session, sender_id) -> SenderRegistry:
    row = db.get(SenderRegistry, money_common.uid(sender_id, "sender id"))
    if row is None:
        raise NotFound("sender not found")
    return row


def list_senders(
    db: Session, *, enterprise_id=None, country=None, status=None, kind=None, limit=50, offset=0
) -> list[SenderRegistry]:
    q = select(SenderRegistry)
    if enterprise_id:
        q = q.where(
            SenderRegistry.enterprise_id == money_common.uid(enterprise_id, "enterprise id")
        )
    if country:
        q = q.where(SenderRegistry.country == ident.country_code(country))
    if status:
        q = q.where(SenderRegistry.current_status == status)
    if kind:
        q = q.where(SenderRegistry.sender_kind == ident.kind_of(kind))
    return list(
        db.scalars(
            q.order_by(SenderRegistry.created_at.desc(), SenderRegistry.id)
            .limit(limit + 1)
            .offset(offset)
        )
    )


def history(db: Session, sender_id) -> list[SenderDecision]:
    sid = get_sender(db, sender_id).id
    return list(
        db.scalars(
            select(SenderDecision)
            .where(SenderDecision.registry_id == sid)
            .order_by(SenderDecision.seq)
        )
    )


def _after_pending(db: Session, row: SenderRegistry, pol: PolicyView, now: datetime) -> str:
    """Pas kërkese/ridërgimi: `allowed=false` ⇒ refuzim sistemi me kategori të qartë; `requires_approval=false` ⇒ miratim sistemi; përndryshe pret admin."""
    if not pol.allowed:
        _append(db, row, "rejected", "rejected", SYSTEM_POLICY, category="policy_denied", pol=pol, now=now, evidence_ref=None,
                reason=f"policy_denied: {row.sender_kind} senders are not allowed in {row.country}" + (f" (revision {pol.revision})" if pol.revision else ""))  # fmt: skip
        return "denied"
    if not pol.requires_approval:
        try:
            with db.begin_nested():
                _append(
                    db,
                    row,
                    "approved",
                    "approved",
                    SYSTEM_POLICY,
                    category="policy_auto_approved",
                    pol=pol,
                    now=now,
                    reason=None,
                    evidence_ref=None,
                )
        except IntegrityError:
            db.refresh(row)
            return "blocked"
        return "approved"
    return "not_applicable"


@_publishing
def request_sender(
    db: Session,
    actor,
    enterprise_id,
    external_ref,
    country,
    value,
    evidence_ref=None,
    *,
    source="admin",
    now: datetime | None = None,
) -> RequestResult:
    """Kërkesë e re ose idempotente: e njëjta (enterprise, external_ref) + e njëjta ngarkesë ⇒ i njëjti rresht; ngarkesë tjetër ⇒ Conflict."""
    _who(actor)
    eid = money_common.uid(enterprise_id, "enterprise id")
    if not isinstance(external_ref, str) or not _REF.match(external_ref):
        raise Invalid("external_ref must be 1..64 chars of [A-Za-z0-9._:-]")
    i = ident.identity(country, value)
    ev = _evidence(evidence_ref)
    if source not in ("admin", "enterprise", "import"):
        raise Invalid("invalid source")
    h = hashlib.sha256(
        json.dumps(
            {"country": i.country, "kind": i.kind, "display": i.display, "evidence": ev},
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if db.get(Enterprise, eid) is None:
        raise NotFound("enterprise not found")
    now = _utc(now)
    _scope_lock(db, i.country, i.kind, exclusive=False)

    def existing():
        return db.scalar(
            select(SenderRegistry).where(
                SenderRegistry.enterprise_id == eid, SenderRegistry.external_ref == external_ref
            )
        )

    def same(r):
        if r.request_hash != h:
            raise Conflict("external_ref was already used with a different sender request")
        return RequestResult(r, False, "not_applicable")

    prior = existing()
    if prior is not None:
        return same(prior)
    clash = db.scalar(
        select(SenderRegistry.id).where(
            SenderRegistry.enterprise_id == eid,
            SenderRegistry.country == i.country,
            SenderRegistry.norm_value == i.norm,
        )
    )
    if clash is not None:
        raise Conflict(
            "this sender is already registered for the enterprise and country under another reference"
        )
    row = SenderRegistry(
        id=uuid.uuid4(), enterprise_id=eid, external_ref=external_ref, country=i.country, sender_kind=i.kind, display_value=i.display, norm_value=i.norm,
        request_hash=h, current_status="pending", current_decision_id=uuid.uuid4(), source=source, created_at=now, updated_at=now,
    )  # fmt: skip
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as e:
        again = existing()
        if again is not None:
            return same(again)
        raise Conflict("a request for this sender is already in progress") from e
    pol = effective_policy(db, i.country, i.kind, now)
    _append(
        db,
        row,
        "requested",
        "pending",
        actor,
        category="request",
        pol=pol,
        now=now,
        reason=None,
        evidence_ref=ev,
        new=True,
    )
    return RequestResult(row, True, _after_pending(db, row, pol, now))


def _transition(
    db,
    actor,
    sender_id,
    decision,
    sources,
    target,
    *,
    category,
    reason=None,
    evidence_ref=None,
    now=None,
    need_reason=False,
):
    _who(actor)
    why = (
        money_common.reason(reason) if need_reason else money_common.optional_text(reason, "reason")
    )
    ev = _evidence(evidence_ref)
    row = _load(db, sender_id)
    if row.current_status not in sources:
        raise Conflict(f"cannot {_VERB[decision]} from status {row.current_status}")
    now = _utc(now)
    pol = effective_policy(db, row.country, row.sender_kind, now)
    return row, why, ev, pol, now


_VERB = {
    "approved": "approve",
    "rejected": "reject",
    "revoked": "revoke",
    "resubmitted": "resubmit",
}


@_publishing
def approve(
    db: Session, actor, sender_id, evidence_ref=None, *, now: datetime | None = None
) -> SenderRegistry:
    row, _, ev, pol, now = _transition(
        db,
        actor,
        sender_id,
        "approved",
        ("pending",),
        "approved",
        category="manual",
        evidence_ref=evidence_ref,
        now=now,
    )
    if not pol.allowed:
        raise Conflict(
            f"policy denies {row.sender_kind} senders in {row.country} (revision {pol.revision}): approval is not possible"
        )
    try:
        with db.begin_nested():
            _append(
                db,
                row,
                "approved",
                "approved",
                actor,
                category="manual",
                pol=pol,
                now=now,
                reason=None,
                evidence_ref=ev,
            )
    except IntegrityError as e:
        db.refresh(row)
        raise Conflict("sender id already approved for another account") from e
    return row


@_publishing
def reject(
    db: Session, actor, sender_id, reason, evidence_ref=None, *, now: datetime | None = None
) -> SenderRegistry:
    row, why, ev, pol, now = _transition(
        db,
        actor,
        sender_id,
        "rejected",
        ("pending",),
        "rejected",
        category="manual",
        reason=reason,
        evidence_ref=evidence_ref,
        now=now,
        need_reason=True,
    )
    _append(
        db,
        row,
        "rejected",
        "rejected",
        actor,
        category="manual",
        pol=pol,
        now=now,
        reason=why,
        evidence_ref=ev,
    )
    return row


@_publishing
def revoke(
    db: Session, actor, sender_id, reason, evidence_ref=None, *, now: datetime | None = None
) -> SenderRegistry:
    row, why, ev, pol, now = _transition(
        db,
        actor,
        sender_id,
        "revoked",
        ("approved",),
        "revoked",
        category="manual",
        reason=reason,
        evidence_ref=evidence_ref,
        now=now,
        need_reason=True,
    )
    _append(
        db,
        row,
        "revoked",
        "revoked",
        actor,
        category="manual",
        pol=pol,
        now=now,
        reason=why,
        evidence_ref=ev,
    )
    return row


@_publishing
def resubmit(
    db: Session, actor, sender_id, evidence_ref=None, *, now: datetime | None = None
) -> RequestResult:
    row, _, ev, pol, now = _transition(
        db,
        actor,
        sender_id,
        "resubmitted",
        ("rejected", "revoked"),
        "pending",
        category="resubmit",
        evidence_ref=evidence_ref,
        now=now,
    )
    _append(
        db,
        row,
        "resubmitted",
        "pending",
        actor,
        category="resubmit",
        pol=pol,
        now=now,
        reason=None,
        evidence_ref=ev,
    )
    return RequestResult(row, False, _after_pending(db, row, pol, now))
