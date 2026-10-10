"""M10-S4: gatishmëria për autoritetin e sender-ave (`sender_authority_readiness`) — VETËM LEXIM, pa rrjet, pa PII (asnjë vlerë sender në rezultat/metrika). NUK e ndryshon konfigurimin as DB-në.

Provë nga DB (projeksioni, krahasimet shadow, bootstrap, outbox, mesazhet) dhe kodi — jo deklaratë operatori. Kombinon S2 (sinkronizimi) dhe S3 (transporti) dhe shton: provën shadow (mostra, drift kritik),
bootstrap-in e rakordimit, mbulimin e senderave lokalë të miratuar, ngrirjen e rishikimit lokal dhe ACK-un e prodhimit. Kritere të pavarura nga kohëzgjatja; dritarja e provës është parametër (S5 e fikson protokollin)."""

from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import and_, exists, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import as_utc, utcnow
from app.models.messaging import ApprovalStatus, SenderId
from app.models.sender_authority import (
    CRITICAL,
    MATCHES,
    SenderAuthorityComparison,
    SenderBootstrapState,
)
from app.models.sender_sync import SyncedSenderAuthorization
from app.models.sending import Message
from app.services import sender_authority as auth
from app.services import sender_request_readiness as rr
from app.services import sender_sync_readiness as sr

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
Check = sr.Check
# kontrollet S2 që janë të detyrueshme për autoritet (lag/gabim i fundit mbeten paralajmërime)
SYNC_REQUIRED = ("cursor_valid", "generation_known", "snapshot_known", "sync_fresh", "projection_consistent", "revisions_sane", "policy_registry_coherent")  # fmt: skip
TRANSPORT_REQUIRED = (
    "endpoint_configured",
    "https_required",
    "scope_granted",
    "no_permanent_failures",
)


def bootstrap_state(db: Session) -> SenderBootstrapState | None:
    return db.get(SenderBootstrapState, 1)


def local_approved_without_central(db: Session) -> int:
    """Senderë lokalë `approved` pa autorizim `approved` aktiv të sinkronizuar (ose pa identitet enterprise): drift i padukshëm për krahasimet e mostruara."""
    a = SyncedSenderAuthorization
    covered = exists().where(
        and_(
            a.enterprise_id == SenderId.enterprise_id, a.country == SenderId.country,
            a.norm_value == SenderId.norm_value, a.status == "approved", a.projection_state == "active",
        )
    )  # fmt: skip
    return (
        db.scalar(
            select(func.count())
            .select_from(SenderId)
            .where(SenderId.status == ApprovalStatus.APPROVED, ~covered)
        )
        or 0
    )


def comparison_counts(db: Session, since: datetime | None = None) -> Counter:
    q = select(SenderAuthorityComparison.category, func.count()).group_by(
        SenderAuthorityComparison.category
    )
    if since is not None:
        q = q.where(SenderAuthorityComparison.created_at >= since)
    return Counter({k: v for k, v in db.execute(q).all()})


def drift_summary(db: Session, since: datetime | None = None) -> dict:
    """Agregate të sigurta (pa label me kardinalitet të lartë)."""
    c = comparison_counts(db, since)
    total = sum(c.values())
    match = sum(c[k] for k in MATCHES)
    critical = sum(c[k] for k in CRITICAL)
    return {
        "comparisons_total": total, "match_total": match, "match_rate": None if total == 0 else round(match / total, 6),
        "critical_total": critical, "by_category": dict(sorted(c.items())),
        "local_allow_central_deny": sum(c[k] for k in ("local_allow_central_deny", "central_missing", "central_pending", "central_rejected", "central_revoked", "policy_mismatch", "sender_identity_mismatch")),
        "local_deny_central_allow": c["local_deny_central_allow"], "central_missing": c["central_missing"],
        "projection_stale": c["projection_stale"], "policy_mismatch": c["policy_mismatch"],
    }  # fmt: skip


def divergence(db: Session) -> dict:
    """Divergjenca lokal↔projeksion mbi çelësin kanonik: status të ndryshëm, ose i miratuar në projeksion pa të miratuar lokalisht."""
    a = SyncedSenderAuthorization
    local = {(r.enterprise_id, r.country, r.norm_value): r.status.value for r in db.scalars(select(SenderId)) if r.enterprise_id}  # fmt: skip
    proj = {(r.enterprise_id, r.country, r.norm_value): r.status for r in db.scalars(select(a).where(a.projection_state == "active"))}  # fmt: skip
    differ = sum(1 for k, v in local.items() if k in proj and proj[k] != v)
    central_only_approved = sum(
        1 for k, v in proj.items() if v == "approved" and local.get(k) != "approved"
    )
    return {"status_differs": differ, "central_approved_not_local": central_only_approved}


def checks(
    db: Session,
    *,
    now: datetime | None = None,
    min_samples: int = 20,
    window_hours: int | None = 168,
) -> list[Check]:
    now = as_utc(now or utcnow())
    since = None if window_hours is None else now - timedelta(hours=window_hours)
    out: list[Check] = []
    mode = settings.sender_authority
    out.append(
        Check(
            "authority_mode",
            FAIL if mode == "local" else PASS,
            "SMS_SENDER_AUTHORITY=local: set shadow first (evidence is collected in shadow)"
            if mode == "local"
            else f"mode={mode}",
        )
    )
    sync = {c.name: c for c in sr.checks(db, now)}
    bad = [n for n in SYNC_REQUIRED if sync[n].level == FAIL]
    out.append(
        Check(
            "sync_healthy",
            FAIL if bad else PASS,
            f"S2 sync checks failing: {', '.join(bad)}" if bad else "S2 sync healthy",
        )
    )
    tr = {c.name: c for c in rr.checks(db, now)}
    badt = [n for n in TRANSPORT_REQUIRED if tr[n].level == FAIL]
    out.append(
        Check(
            "request_transport",
            FAIL if badt else PASS,
            f"S3 transport checks failing: {', '.join(badt)}" if badt else "S3 transport healthy",
        )
    )
    out.append(
        Check(
            "reporting_enabled",
            PASS if settings.sender_request_reporting else FAIL,
            "reporter enabled"
            if settings.sender_request_reporting
            else "SMS_SENDER_REQUEST_REPORTING is false: new requests never reach Central",
        )
    )
    st = bootstrap_state(db)
    done = st is not None and st.completed_at is not None
    out.append(
        Check(
            "bootstrap_complete",
            PASS if done else FAIL,
            "bootstrap reconciled"
            if done
            else "no completed bootstrap record (run scripts.sender_bootstrap reconcile --record)",
        )
    )
    unresolved = 0 if st is None else st.unresolved_count
    out.append(
        Check(
            "bootstrap_unresolved",
            FAIL if unresolved else PASS,
            f"{unresolved} unresolved reconciliation item(s)" if unresolved else "none",
        )
    )
    gap = local_approved_without_central(db)
    out.append(
        Check(
            "local_approved_covered",
            FAIL if gap else PASS,
            f"{gap} locally approved sender(s) have no approved projection"
            if gap
            else "every locally approved sender has an approved projection",
        )
    )
    d = drift_summary(db, since)
    out.append(
        Check(
            "shadow_samples",
            FAIL if d["comparisons_total"] < min_samples else PASS,
            f"{d['comparisons_total']} comparison(s) (minimum {min_samples})",
        )
    )
    out.append(
        Check(
            "critical_drift",
            FAIL if d["critical_total"] else PASS,
            f"{d['critical_total']} critical comparison(s) in the window {d['by_category']}"
            if d["critical_total"]
            else "no critical drift in the window",
        )
    )
    div = divergence(db)
    out.append(
        Check(
            "projection_divergence",
            WARN if (div["status_differs"] or div["central_approved_not_local"]) else PASS,
            f"{div}"
            if (div["status_differs"] or div["central_approved_not_local"])
            else "local and projected statuses agree",
        )
    )
    out.append(
        Check(
            "review_freeze",
            PASS if (mode != "central" or auth.frozen()) else FAIL,
            "local review is frozen under central"
            if mode == "central"
            else "not applicable before central",
        )
    )
    prod = settings.env == "production"
    ack_bad = mode == "central" and prod and not settings.sender_authority_ack
    out.append(
        Check(
            "production_ack",
            FAIL if ack_bad else PASS,
            "SMS_SENDER_AUTHORITY_ACK is required in production for central" if ack_bad else "ok",
        )
    )
    return out


def overall(items: list[Check]) -> str:
    return (
        FAIL
        if any(c.level == FAIL for c in items)
        else (WARN if any(c.level == WARN for c in items) else PASS)
    )


def metrics(db: Session, now: datetime | None = None, window_hours: int | None = 168) -> dict:
    now = as_utc(now or utcnow())
    since = None if window_hours is None else now - timedelta(hours=window_hours)
    st = bootstrap_state(db)
    central_msgs = (
        db.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.sender_authority_source == "central")
        )
        or 0
    )
    return {
        "mode": settings.sender_authority, "drift": drift_summary(db, since), "divergence": divergence(db),
        "local_approved_without_central": local_approved_without_central(db),
        "bootstrap": None if st is None else {"version": st.bootstrap_version, "completed": st.completed_at is not None, "unresolved": st.unresolved_count, "senders": st.sender_count, "tenants": st.tenant_count, "report_hash": st.report_hash},
        "central_sourced_messages": int(central_msgs), "projection_stale": auth.projection_stale(db, now),
    }  # fmt: skip


def rollback_checks(db: Session, target: str, *, accept_divergence: bool = False) -> list[Check]:
    """Rikthimi i kontrolluar. `central→shadow`: gjithmonë i mundur (lokali kthehet autoritet; Central s'preket, projeksioni s'prek). `central→local`: kërkon rakordim — pas vendimeve Central gjendja lokale mund të jetë e vjetruar."""
    out: list[Check] = []
    if target not in ("shadow", "local"):
        return [Check("target", FAIL, "target must be shadow or local")]
    div = divergence(db)
    cm = (
        db.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.sender_authority_source == "central")
        )
        or 0
    )
    out.append(
        Check("data_preserved", PASS, "rollback never deletes Central data or the projection")
    )
    out.append(
        Check(
            "central_messages",
            PASS,
            f"{cm} message(s) were authorised by Central (provenance preserved)",
        )
    )
    if target == "shadow":
        out.append(
            Check(
                "divergence",
                PASS if not (div["status_differs"] or div["central_approved_not_local"]) else WARN,
                f"local authority resumes; drift will be recorded: {div}",
            )
        )
    else:
        bad = bool(div["status_differs"] or div["central_approved_not_local"])
        out.append(
            Check(
                "divergence",
                FAIL if (bad and not accept_divergence) else (WARN if bad else PASS),
                f"local state differs from Central decisions: {div}"
                if bad
                else "local and Central agree",
            )
        )
    return out
