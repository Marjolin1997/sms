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
AUTHORITY_VERSION = 1
Check = sr.Check
# kontrollet S2 që janë të detyrueshme për autoritet (lag/gabim i fundit mbeten paralajmërime)
SYNC_REQUIRED = ("cursor_valid", "generation_known", "snapshot_known", "sync_fresh", "projection_consistent", "revisions_sane", "policy_registry_coherent")  # fmt: skip
TRANSPORT_REQUIRED = (
    "endpoint_configured",
    "https_required",
    "scope_granted",
    "no_permanent_failures",
    "oldest_pending_age",
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
    from app.models.sender_authority import SenderBootstrapIssue as Iss

    explained = exists().where(
        and_(Iss.sender_id == SenderId.id, Iss.resolution == "accepted_not_migrated")
    )
    return (
        db.scalar(
            select(func.count())
            .select_from(SenderId)
            .where(SenderId.status == ApprovalStatus.APPROVED, ~covered, ~explained)
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
    risk = sum(
        1 for k, v in local.items() if v == "approved" and k in proj and proj[k] != "approved"
    )
    return {
        "status_differs": differ,
        "central_approved_not_local": central_only_approved,
        "reauthorize_risk": risk,
    }


def evidence_since(db: Session, now: datetime, window_hours: int | None) -> datetime | None:
    """Dritarja e provës shadow: që nga përfundimi i bootstrap-it (drifti i vjetër i korrigjuar s'bllokon përgjithmonë), e kufizuar opsionalisht nga `window_hours`."""
    st = bootstrap_state(db)
    start = as_utc(st.completed_at) if (st is not None and st.completed_at is not None) else None
    if window_hours is not None:
        w = now - timedelta(hours=window_hours)
        start = w if start is None else max(start, w)
    return start


def ack_status(db: Session) -> tuple[bool, str]:
    """ACK i lidhur me provën: `SMS_SENDER_AUTHORITY_ACK` = `evidence_hash` i një prove `pre_cutover` që ekziston, me versionin e autoritetit dhe mjedisin e njëjtë, lidhur me hash-in AKTUAL të bootstrap-it
    dhe pa FAIL në gatishmërinë e saj. Ndryshimi i provës së bootstrap-it e bën ACK-un e vjetër të pavlefshëm."""
    from app.models.sender_authority import SenderCutoverEvidence

    h = settings.sender_authority_ack or ""
    if not h:
        return False, "no ACK configured"
    ev = db.scalar(
        select(SenderCutoverEvidence).where(
            SenderCutoverEvidence.evidence_hash == h, SenderCutoverEvidence.kind == "pre_cutover"
        )
    )
    if ev is None:
        return False, "ACK does not match any recorded pre_cutover evidence"
    if ev.authority_version != AUTHORITY_VERSION:
        return (
            False,
            f"ACK was issued for authority version {ev.authority_version}, current is {AUTHORITY_VERSION}",
        )
    if ev.environment != settings.env:
        return False, "ACK was issued for a different environment"
    st = bootstrap_state(db)
    if st is None or st.report_hash != ev.bootstrap_report_hash:
        return False, "ACK is stale: the bootstrap evidence changed after it was issued"
    if ev.readiness_status == "FAIL":
        return False, "ACK evidence recorded a failing readiness"
    return True, f"ACK bound to evidence {h[:12]}"


def checks(
    db: Session,
    *,
    now: datetime | None = None,
    min_samples: int | None = None,
    window_hours: int | None = None,
) -> list[Check]:
    now = as_utc(now or utcnow())
    min_samples = settings.sender_evidence_min_samples if min_samples is None else min_samples
    window_hours = settings.sender_evidence_window_hours if window_hours is None else window_hours
    since = evidence_since(db, now, window_hours)
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
    if d["by_category"].get("local_deny_central_allow"):
        out.append(
            Check(
                "expected_drift",
                WARN,
                f"{d['by_category']['local_deny_central_allow']} local-deny/central-allow comparison(s): understand before cutover (they become allowed)",
            )
        )
    if mode == "central" and settings.env == "production":
        ok, why = ack_status(db)
        out.append(Check("production_ack", PASS if ok else FAIL, why))
    else:
        out.append(Check("production_ack", PASS, "not required before central/production"))
    return out


def overall(items: list[Check]) -> str:
    return (
        FAIL
        if any(c.level == FAIL for c in items)
        else (WARN if any(c.level == WARN for c in items) else PASS)
    )


def metrics(db: Session, now: datetime | None = None, window_hours: int | None = None) -> dict:
    now = as_utc(now or utcnow())
    window_hours = settings.sender_evidence_window_hours if window_hours is None else window_hours
    since = evidence_since(db, now, window_hours)
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
    risk, other = div["reauthorize_risk"], div["status_differs"] + div["central_approved_not_local"]
    if target == "shadow":
        out.append(
            Check(
                "divergence",
                PASS if not other else WARN,
                f"local authority resumes; drift will be recorded: {div}",
            )
        )
    else:
        # rikthimi në local NUK duhet të ri-autorizojë heshtur sender të revokuar/refuzuar nga Central
        out.append(
            Check(
                "reauthorize_risk",
                FAIL if (risk and not accept_divergence) else (WARN if risk else PASS),
                f"{risk} locally approved sender(s) are denied by Central and would be re-authorised by local authority"
                if risk
                else "no locally approved sender is denied by Central",
            )
        )
        out.append(
            Check(
                "divergence",
                WARN if other else PASS,
                f"local and Central statuses differ: {div}" if other else "local and Central agree",
            )
        )
    return out
