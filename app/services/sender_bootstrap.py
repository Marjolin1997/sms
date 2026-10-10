"""M10-S4 (Enterprise): bootstrap-i i senderave ekzistues — eksport i artefaktit për Central dhe RAKORDIM lokal-vs-projeksion (VETËM LEXIM, përveç shënimit opsional `record`).

Rrjedha: `export` (artefakt `sender-bootstrap.v1`) → Central `apps.central.tools.sender_import` (dry-run, pastaj apply) → S2 sinkronizon regjistrin → `reconcile --record` krahason çdo `SenderId` me projeksionin dhe, vetëm nëse
asgjë nuk mbetet e pazgjidhur, shënon `sms_sender_bootstrap_state` (versioni, kohët, numrat, hash i raportit). Asnjë `SenderId` nuk ndryshohet."""

import hashlib
import json
from collections import Counter
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.timeutil import utcnow
from app.models.messaging import ApprovalStatus, SenderId
from app.models.sender_authority import SenderBootstrapIssue, SenderBootstrapState
from app.models.sender_sync import SyncedSenderAuthorization
from app.services import audit
from app.services import sender_authorization as sa
from packages.contracts.control_plane.sender import request_v1 as rv

SCHEMA = "sender-bootstrap.v1"
REPORT_SCHEMA = "sender-bootstrap-report.v1"
VERSION = 1
RECONCILE_LOCK_KEY = 0x534D5342  # "SMSB": kyç advisory për `reconcile --record`
UNRESOLVED = frozenset({
    "identity_conflict", "invalid_legacy_identity", "missing_enterprise_mapping", "missing_in_central", "policy_denied", "global_key_conflict",
    "local_approved_central_pending", "local_approved_central_rejected", "local_approved_central_revoked",
})  # fmt: skip


def export(db: Session, source_revision: str = "unknown") -> dict:
    rows = db.scalars(select(SenderId).order_by(SenderId.id)).all()
    return {
        "schema": SCHEMA, "source_revision": source_revision, "generated_at": utcnow().isoformat(timespec="microseconds"),
        "senders": [
            {"sender_id": r.id, "external_ref": rv.external_ref_for(r.id), "enterprise_id": None if r.enterprise_id is None else str(r.enterprise_id),
             "country": r.country, "display_value": r.value, "kind": r.kind.value, "status": r.status.value}
            for r in rows
        ],
    }  # fmt: skip


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


class ResolutionError(ValueError):
    """Zgjidhje e pavlefshme e një çështjeje bootstrap."""


OPERATOR_RESOLUTIONS = ("accepted_not_migrated", "sender_deactivated")
ACTIVE = (ApprovalStatus.APPROVED, ApprovalStatus.PENDING)


def _identity_hash(country: str, value: str) -> str:
    return hashlib.sha256(sa.canonical_key(country, sa.norm_of(value)).encode()).hexdigest()[:16]


def _accepted(issue: SenderBootstrapIssue, sender: SenderId | None) -> bool:
    if issue.resolution == "accepted_not_migrated":
        return True
    if (
        issue.resolution == "sender_deactivated"
    ):  # vlen vetëm ndërsa sender-i s'është i miratuar lokalisht
        return sender is not None and sender.status != ApprovalStatus.APPROVED
    return False


def reconcile(
    db: Session,
    *,
    record: bool = False,
    source_revision: str | None = None,
    central_report: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """Rakordim lokal-vs-projeksion (+ opsionalisht raporti Central i importit për kategoritë që Enterprise s'i sheh: politikë, çelës global). `record` shkruan çështjet + gjendjen."""
    now = now or utcnow()
    if record and db.get_bind().dialect.name == "postgresql":
        # dy `reconcile --record` paralele s'duhet të shkruajnë të njëjtat çështje/gjendje: serializim për transaksion
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": RECONCILE_LOCK_KEY})
    a = SyncedSenderAuthorization
    proj = {
        (r.enterprise_id, r.country, r.norm_value): r
        for r in db.scalars(select(a).where(a.projection_state == "active"))
    }
    central = {}
    if central_report is not None:
        if central_report.get("schema") != "sender-bootstrap-report.v1":
            raise ResolutionError("central report must have schema sender-bootstrap-report.v1")
        central = {i["sender_id"]: i["category"] for i in central_report.get("items", [])}
    senders = {s.id: s for s in db.scalars(select(SenderId).order_by(SenderId.id))}
    items = []
    for s in senders.values():
        active = s.status in ACTIVE
        if s.enterprise_id is None:
            cat = "missing_enterprise_mapping" if active else "local_inactive"
        else:
            try:
                n = sa.normalize(s.value)
                bad = n.kind != s.kind
            except sa.InvalidSender:
                bad, n = True, None
            if bad:
                cat = "invalid_legacy_identity" if active else "local_inactive"
            else:
                p = proj.get((s.enterprise_id, s.country, n.norm))
                approved = s.status == ApprovalStatus.APPROVED
                if p is not None:
                    if p.external_ref != rv.external_ref_for(s.id):
                        cat = "identity_conflict"
                    elif approved and p.status != "approved":
                        cat = f"local_approved_central_{p.status}"
                    else:
                        cat = "exact_match"
                elif approved:
                    cat = (
                        central.get(s.id)
                        if central.get(s.id) in UNRESOLVED
                        else "missing_in_central"
                    )
                elif s.status == ApprovalStatus.PENDING:
                    cat = "local_pending"
                else:
                    cat = "local_inactive"
        items.append({"sender_id": s.id, "external_ref": rv.external_ref_for(s.id), "enterprise_id": None if s.enterprise_id is None else str(s.enterprise_id), "category": cat})  # fmt: skip
    # çështjet e mëparshme (historia s'fshihet): të zgjidhurat e pranuara nuk numërohen si të pazgjidhura
    issues = list(db.scalars(select(SenderBootstrapIssue)))
    accepted_keys = {
        (i.sender_id, i.category)
        for i in issues
        if i.resolved_at is not None and _accepted(i, senders.get(i.sender_id))
    }
    for it in items:
        it["accepted"] = (it["sender_id"], it["category"]) in accepted_keys
    blocking = [it for it in items if it["category"] in UNRESOLVED]
    unresolved = sum(1 for it in blocking if not it["accepted"])
    summary = Counter(i["category"] for i in items)
    report = {"schema": REPORT_SCHEMA, "bootstrap_version": VERSION, "summary": dict(sorted(summary.items())), "unresolved": unresolved,
              "accepted": len(blocking) - unresolved, "senders": len(items), "tenants": len({i["enterprise_id"] for i in items}), "items": items}  # fmt: skip
    report["report_hash"] = hashlib.sha256(
        _canonical({k: report[k] for k in ("summary", "items")})
    ).hexdigest()
    if record:
        open_by_key = {(i.sender_id, i.category): i for i in issues if i.resolved_at is None}
        now_blocking = {(it["sender_id"], it["category"]) for it in blocking}
        for it in blocking:
            k = (it["sender_id"], it["category"])
            if not it["accepted"] and k not in open_by_key:
                s = senders[it["sender_id"]]
                db.add(SenderBootstrapIssue(enterprise_id=s.enterprise_id, sender_id=s.id, category=it["category"], identity_hash=_identity_hash(s.country, s.value), detected_at=now, report_hash=report["report_hash"]))  # fmt: skip
        for k, i in open_by_key.items():
            if (
                k not in now_blocking
            ):  # u korrigjua (ose sender-i u çaktivizua): zgjidhje automatike, e shënuar si e tillë
                i.resolved_at, i.resolution, i.resolved_by, i.reason = (
                    now,
                    "corrected",
                    "system:reconcile",
                    "no longer detected by reconcile",
                )
        st = db.get(SenderBootstrapState, 1)
        if st is None:
            st = SenderBootstrapState(id=1)
            db.add(st)
        st.bootstrap_version = VERSION
        st.started_at = st.started_at or now
        st.completed_at = now if unresolved == 0 else None
        st.source_revision = source_revision or st.source_revision
        st.tenant_count, st.sender_count, st.unresolved_count = (
            report["tenants"],
            report["senders"],
            unresolved,
        )
        st.report_hash = report["report_hash"]
        db.flush()
    return report


def resolve(
    db: Session, *, sender_id: int, category: str, resolution: str, actor: str, reason: str,
    evidence_ref: str | None = None, now: datetime | None = None,
) -> SenderBootstrapIssue:  # fmt: skip
    """Zgjidhje e shprehur nga operatori e një çështjeje të hapur: "e kuptuar dhe e pranuar" — NUK e anashkalon politikën Central dhe NUK e miraton sender-in.
    `missing_in_central` s'zgjidhet kurrë me deklaratë (duhet importuar). `sender_deactivated` kërkon që sender-i të mos jetë i miratuar lokalisht."""
    if resolution not in OPERATOR_RESOLUTIONS:
        raise ResolutionError(f"resolution must be one of {OPERATOR_RESOLUTIONS}")
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 64:
        raise ResolutionError("actor is required (max 64 chars)")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 255:
        raise ResolutionError("reason is required (max 255 chars)")
    if evidence_ref is not None and not (1 <= len(evidence_ref) <= 128):
        raise ResolutionError("evidence_ref must be 1..128 chars")
    if category == "missing_in_central":
        raise ResolutionError(
            "missing_in_central cannot be resolved by declaration: import the sender"
        )
    issue = db.scalar(select(SenderBootstrapIssue).where(SenderBootstrapIssue.sender_id == sender_id, SenderBootstrapIssue.category == category, SenderBootstrapIssue.resolved_at.is_(None)))  # fmt: skip
    if issue is None:
        raise ResolutionError("no open issue for this sender and category")
    s = db.get(SenderId, sender_id)
    if resolution == "sender_deactivated" and s is not None and s.status == ApprovalStatus.APPROVED:
        raise ResolutionError(
            "sender_deactivated requires the sender not to be approved locally (revoke it first)"
        )
    issue.resolved_at, issue.resolution, issue.resolved_by, issue.reason, issue.evidence_ref = (now or utcnow()), resolution, actor.strip(), reason.strip(), evidence_ref  # fmt: skip
    audit.system_event(db, actor.strip(), "sender.bootstrap_resolve", "sender_id", sender_id, {"category": category, "resolution": resolution, "evidence_ref": evidence_ref})  # fmt: skip
    db.flush()
    return issue
