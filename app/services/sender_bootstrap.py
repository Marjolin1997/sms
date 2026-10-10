"""M10-S4 (Enterprise): bootstrap-i i senderave ekzistues — eksport i artefaktit për Central dhe RAKORDIM lokal-vs-projeksion (VETËM LEXIM, përveç shënimit opsional `record`).

Rrjedha: `export` (artefakt `sender-bootstrap.v1`) → Central `apps.central.tools.sender_import` (dry-run, pastaj apply) → S2 sinkronizon regjistrin → `reconcile --record` krahason çdo `SenderId` me projeksionin dhe, vetëm nëse
asgjë nuk mbetet e pazgjidhur, shënon `sms_sender_bootstrap_state` (versioni, kohët, numrat, hash i raportit). Asnjë `SenderId` nuk ndryshohet."""

import hashlib
import json
from collections import Counter
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import utcnow
from app.models.messaging import ApprovalStatus, SenderId
from app.models.sender_authority import SenderBootstrapState
from app.models.sender_sync import SyncedSenderAuthorization
from app.services import sender_authorization as sa
from packages.contracts.control_plane.sender import request_v1 as rv

SCHEMA = "sender-bootstrap.v1"
REPORT_SCHEMA = "sender-bootstrap-report.v1"
VERSION = 1
UNRESOLVED = frozenset({
    "identity_conflict", "invalid_legacy_identity", "missing_enterprise_mapping", "missing_in_central",
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


def reconcile(
    db: Session,
    *,
    record: bool = False,
    source_revision: str | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or utcnow()
    a = SyncedSenderAuthorization
    proj = {
        (r.enterprise_id, r.country, r.norm_value): r
        for r in db.scalars(select(a).where(a.projection_state == "active"))
    }
    items = []
    for s in db.scalars(select(SenderId).order_by(SenderId.id)):
        if s.enterprise_id is None:
            cat = "missing_enterprise_mapping"
        else:
            try:
                n = sa.normalize(s.value)
                bad = n.kind != s.kind
            except sa.InvalidSender:
                bad, n = True, None
            if bad:
                cat = "invalid_legacy_identity"
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
                    cat = "missing_in_central"
                elif s.status == ApprovalStatus.PENDING:
                    cat = "local_pending"
                else:
                    cat = "local_inactive"
        items.append({"sender_id": s.id, "external_ref": rv.external_ref_for(s.id), "enterprise_id": None if s.enterprise_id is None else str(s.enterprise_id), "category": cat})  # fmt: skip
    summary = Counter(i["category"] for i in items)
    unresolved = sum(v for k, v in summary.items() if k in UNRESOLVED)
    report = {"schema": REPORT_SCHEMA, "bootstrap_version": VERSION, "summary": dict(sorted(summary.items())), "unresolved": unresolved,
              "senders": len(items), "tenants": len({i["enterprise_id"] for i in items}), "items": items}  # fmt: skip
    report["report_hash"] = hashlib.sha256(
        _canonical({k: report[k] for k in ("summary", "items")})
    ).hexdigest()
    if record:
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
