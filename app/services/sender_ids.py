"""Ciklin e jetës së sender-ave (kërkesë → miratim/refuzim/revokim → ridërgim) mbi `SenderId` + historinë append-only `SenderDecision` (M10-S0).

Autorizimi/leximi është te `sender_authorization` (kanonik). Çdo tranzicion shkruan, në të njëjtin transaksion të thirrësit: gjendjen aktuale,
rreshtin e vendimit dhe (nga API) auditin. Gjendjet nuk ndryshojnë: pending → approved|rejected · approved → revoked · rejected|revoked → pending."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import Conflict, NotFound
from app.core.scope import Owner, owned, ref
from app.models.messaging import ApprovalStatus, SenderDecision, SenderId
from app.services import approvals, sender_authority, sender_request_outbox
from app.services import sender_authorization as sa
from app.services.sender_authorization import (  # noqa: F401  (ri-eksport për përputhshmëri)
    ALNUM,
    COUNTRY,
    NUMERIC,
    InvalidSender,
    SenderNotAllowed,
    classify,
)


def _key(country: str, value: str) -> str:
    return sa.canonical_key(country, sa.norm_of(value))


def _record(
    db: Session,
    s: SenderId,
    decision: str,
    actor: str,
    reason: str | None,
    from_status: str | None,
    evidence_ref: str | None = None,
) -> SenderDecision:
    d = SenderDecision(
        sender_id=s.id, decision=decision, from_status=from_status, to_status=s.status.value,
        decided_at=s.reviewed_at or datetime.now(UTC), decided_by=actor, reason=reason,
        policy_revision=None, evidence_ref=evidence_ref, source="local",
    )  # fmt: skip
    db.add(d)
    db.flush()
    s.current_decision_id = d.id
    db.flush()
    return d


def request(
    db: Session, owner: Owner, country: str, value: str, actor: str | None = None
) -> SenderId:
    """Kërkesë e re ose idempotente (rreshti ekzistues kthehet; i refuzuar/revokuar → ridërgim). Kërkesë paralele identike: humbësi merr `Conflict`."""
    if not COUNTRY.match(country):
        raise InvalidSender("country must be ISO alpha-2")
    country = country.upper()
    n = sa.normalize(value)
    who = actor or ref(owner)
    existing = sa.pick(
        db.scalars(
            select(SenderId).where(
                owned(SenderId, owner), SenderId.country == country, SenderId.norm_value == n.norm
            )
        ).all(),
        n.display,
    )
    if existing:
        if existing.status in (ApprovalStatus.REJECTED, ApprovalStatus.REVOKED):
            before = existing.status.value
            approvals.transition(existing, "resubmit", who)
            db.flush()
            _record(db, existing, "resubmitted", who, None, before)
            sender_request_outbox.enqueue_resubmission(db, existing)
        return existing
    s = SenderId(
        owner_ref=ref(owner), country=country, value=n.display, kind=n.kind, norm_value=n.norm
    )
    try:
        with db.begin_nested():
            db.add(s)
            db.flush()
    except IntegrityError as e:
        raise Conflict("a request for this sender id and country is already in progress") from e
    _record(db, s, "requested", who, None, None)
    sender_request_outbox.enqueue(db, s, "requested")
    return s


def _get(db: Session, sender_id: int) -> SenderId:
    s = db.get(SenderId, sender_id, with_for_update=True)
    if s is None:
        raise NotFound("sender id not found")
    return s


def approve(db: Session, sender_id: int, actor: str, evidence_ref: str | None = None) -> SenderId:
    sender_authority.require_local_review("approve")
    s = _get(db, sender_id)
    before = s.status.value
    approvals.transition(s, "approve", actor)
    s.approved_key = sa.canonical_key(s.country, s.norm_value or sa.norm_of(s.value))
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise Conflict("sender id already approved for another account") from e
    _record(db, s, "approved", actor, None, before, evidence_ref)
    return s


def reject(
    db: Session, sender_id: int, actor: str, reason: str, evidence_ref: str | None = None
) -> SenderId:
    sender_authority.require_local_review("reject")
    s = _get(db, sender_id)
    before = s.status.value
    approvals.transition(s, "reject", actor, reason)
    db.flush()
    _record(db, s, "rejected", actor, reason, before, evidence_ref)
    return s


def revoke(
    db: Session, sender_id: int, actor: str, reason: str, evidence_ref: str | None = None
) -> SenderId:
    sender_authority.require_local_review("revoke")
    s = _get(db, sender_id)
    before = s.status.value
    approvals.transition(s, "revoke", actor, reason)
    s.approved_key = None
    db.flush()
    _record(db, s, "revoked", actor, reason, before, evidence_ref)
    return s


def assert_usable(db: Session, owner: Owner, country: str, value: str) -> SenderId:
    """Përputhshmëri: ruan nënshkrimin e vjetër. Rruga e re është `sender_authorization.assert_outbound` (rezultat i strukturuar)."""
    auth = sa.assert_outbound(db, owner, country, value)
    return db.get(SenderId, auth.sender_ref)


def owners_of_number(db: Session, number: str) -> list:
    return sa.owners_of_numeric(db, number)
