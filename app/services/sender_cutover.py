"""M10-S5: prova e cutover-it, rikthimi i kontrolluar dhe canary. Asnjë veprim i pakthyeshëm nuk automatizohet: ndërrimi i `SMS_SENDER_AUTHORITY` mbetet komandë e operatorit.

- `record_evidence`: regjistron provën e pandryshueshme (hash UNIQUE ⇒ idempotent dhe pa garë shkrimi); `pre_cutover` refuzohet nëse gatishmëria ka FAIL.
- ACK = `evidence_hash` i provës `pre_cutover` (lidhet me versionin, mjedisin dhe hash-in e bootstrap-it; shih `sender_authority_readiness.ack_status`).
- `reconcile_local_for_rollback`: rikthim i sigurt central→(shadow)→local — revokon LOKALISHT senderat që Central i ka mohuar (që autoriteti lokal të mos i ri-autorizojë); kërkon modë ≠ central."""

import hashlib
import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import utcnow
from app.models.messaging import ApprovalStatus, SenderId
from app.models.sender_authority import SenderBootstrapState, SenderCutoverEvidence
from app.models.sender_sync import SenderSyncCursor, SyncedSenderAuthorization
from app.models.sending import Message
from app.services import sender_authority as sau
from app.services import sender_authority_readiness as ar
from app.services import sender_ids
from app.services import sender_policy_readiness as pr

AUTHORITY_VERSION = ar.AUTHORITY_VERSION


class EvidenceRefused(ValueError):
    pass


def _canon(obj) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode()


def build_evidence(
    db: Session, *, kind: str, actor: str, code_revision: str, now: datetime | None = None,
    central_readiness: dict | None = None, canary_ref: str | None = None, ref_hash: str | None = None,
    min_samples: int | None = None, window_hours: int | None = None,
) -> dict:  # fmt: skip
    now = now or utcnow()
    items = pr.checks(
        db,
        now=now,
        target="central" if kind == "pre_cutover" else None,
        central_readiness=central_readiness,
        min_samples=min_samples,
        window_hours=window_hours,
    )
    st = db.get(SenderBootstrapState, 1)
    cur = db.get(SenderSyncCursor, 1)
    since = ar.evidence_since(
        db, now, window_hours if window_hours is not None else settings.sender_evidence_window_hours
    )
    drift = ar.drift_summary(db, since)
    return {
        "kind": kind, "authority_version": AUTHORITY_VERSION, "environment": settings.env, "code_revision": code_revision,
        "generated_at": now.isoformat(timespec="microseconds"), "actor": actor, "authority_mode": settings.sender_authority,
        "bootstrap": None if st is None else {"report_hash": st.report_hash, "completed": st.completed_at is not None, "tenants": st.tenant_count, "senders": st.sender_count, "unresolved": st.unresolved_count},
        "sync": None if cur is None else {"epoch": str(cur.epoch) if cur.epoch else None, "generation": cur.authorization_generation, "last_seq": cur.last_seq, "latest_central_seq": cur.latest_central_seq},
        "shadow": {"comparisons_total": drift["comparisons_total"], "critical_total": drift["critical_total"], "match_rate": drift["match_rate"], "min_samples": min_samples if min_samples is not None else settings.sender_evidence_min_samples, "since": since.isoformat() if since else None},
        "open_issues": pr.open_blocking_issues(db),
        "readiness": {"status": pr.overall(items), "hash": pr.readiness_hash(items), "checks": [{"name": c.name, "level": c.level} for c in sorted(items, key=lambda c: c.name)]},
        "canary_ref": canary_ref, "ref_hash": ref_hash,
    }  # fmt: skip


def record_evidence(db: Session, payload: dict) -> tuple[SenderCutoverEvidence, bool]:
    """Idempotent: i njëjti payload ⇒ i njëjti rresht (created=False). `pre_cutover` me FAIL refuzohet."""
    if payload["kind"] == "pre_cutover" and payload["readiness"]["status"] == "FAIL":
        raise EvidenceRefused(
            "readiness has FAIL checks: "
            + ", ".join(c["name"] for c in payload["readiness"]["checks"] if c["level"] == "FAIL")
        )
    h = hashlib.sha256(_canon(payload)).hexdigest()
    row = SenderCutoverEvidence(
        kind=payload["kind"], evidence_hash=h, authority_version=payload["authority_version"], environment=payload["environment"],
        code_revision=payload["code_revision"], actor=payload["actor"], bootstrap_report_hash=(payload["bootstrap"] or {}).get("report_hash"),
        readiness_status=payload["readiness"]["status"], readiness_hash=payload["readiness"]["hash"], canary_ref=payload["canary_ref"],
        ref_hash=payload["ref_hash"], payload=payload,
    )  # fmt: skip
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        return db.scalar(
            select(SenderCutoverEvidence).where(SenderCutoverEvidence.evidence_hash == h)
        ), False
    return row, True


def complete_cutover(
    db: Session,
    *,
    actor: str,
    ref_hash: str,
    canary_ref: str,
    code_revision: str,
    now: datetime | None = None,
):
    """Prova `post_cutover`: vetëm nën `central`, me ACK-un e vlefshëm dhe me canary-n (mesazh i vërtetë, i autorizuar nga Central, jo i dështuar)."""
    if settings.sender_authority != "central":
        raise EvidenceRefused("post-cutover evidence requires SMS_SENDER_AUTHORITY=central")
    pre = db.scalar(
        select(SenderCutoverEvidence).where(
            SenderCutoverEvidence.evidence_hash == ref_hash,
            SenderCutoverEvidence.kind == "pre_cutover",
        )
    )
    if pre is None:
        raise EvidenceRefused("ref_hash does not match a pre_cutover evidence record")
    m = db.scalar(select(Message).where(Message.public_id == canary_ref))
    if m is None or m.sender_authority_source != "central":
        raise EvidenceRefused("canary must be a message authorised by Central")
    if m.status.value == "failed":
        raise EvidenceRefused("canary message failed")
    payload = build_evidence(
        db,
        kind="post_cutover",
        actor=actor,
        code_revision=code_revision,
        now=now,
        canary_ref=canary_ref,
        ref_hash=ref_hash,
    )
    return record_evidence(db, payload)


def reconcile_local_for_rollback(db: Session, actor: str) -> int:
    """Revokon lokalisht senderat e miratuar lokalisht që Central i ka refuzuar/revokuar. Kërkon modë ≠ central (ngrirja). Central dhe projeksioni nuk preken."""
    if sau.frozen():
        raise EvidenceRefused("switch to shadow first: local review is frozen under central")
    a = SyncedSenderAuthorization
    proj = {
        (r.enterprise_id, r.country, r.norm_value): r.status
        for r in db.scalars(select(a).where(a.projection_state == "active"))
    }
    n = 0
    for s in db.scalars(select(SenderId).where(SenderId.status == ApprovalStatus.APPROVED)):
        st = proj.get((s.enterprise_id, s.country, s.norm_value))
        if st is not None and st != "approved":
            sender_ids.revoke(db, s.id, actor, f"rollback: Central status is {st}")
            n += 1
    return n
