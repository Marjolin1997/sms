"""M11-D2: snapshot operacional i sender-ave (`sender.ops.v1`) Enterprise → Central. Latest-state, asinkron, VETËM LEXIM lokal.

- Ndërtimi përdor shërbimet ekzistuese (sync readiness `status`, outbox `stats`, `drift_summary`, `evaluate`) me pyetje agregate: numri i pyetjeve nuk varet nga volumi
  i sender-ave. Çdo seksion lexohet i pavarur; seksion që dështon del `null` (= e padisponueshme, JO zero e shëndetshme).
- `report_once` NDËRTON brenda një seancë të shkurtër lexuese që mbyllet para rrjetit; thirrja HTTP bëhet jashtë çdo transaksioni. Dështimi (rrjet, auth, refuzim)
  regjistrohet në numëruesit e këtij moduli dhe NUK prek raportimin e kërkesave, sync-un, submit-in apo dispatch-in (asnjë prej tyre s'e importon këtë modul).
- Asnjë vlerë sender, PII, sekret ose payload kërkese: vetëm numërues, statuse, mosha, kategori të kufizuara dhe prefikse hash-esh."""

import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import as_utc, utcnow
from app.models.sender_authority import SenderBootstrapIssue, SenderCutoverEvidence
from app.models.sender_sync import SenderSyncCursor
from app.services import sender_alerts as alerts
from app.services import sender_authority as sau
from app.services import sender_authority_readiness as ar
from app.services import sender_request_outbox as ob
from app.services import sender_sync_readiness as sr
from app.services.control_plane_client import (
    ControlPlaneClient,
    CpError,
    CpReportRejected,
)
from packages.contracts.control_plane.sender import ops_v1 as v1

log = logging.getLogger("sms.sender.ops")
ALERTS_TTL_S = 300.0  # `evaluate` përfshin kontrolle të plota (rakordim): rilexohet jo më shpesh se kaq; mosha shfaqet te `alerts.evaluated_age_seconds`
BACKOFF_CAP_S = 600
_REV = re.compile(r"^[0-9A-Za-z._-]{1,64}$")
_alerts_cache: dict = {"mono": None, "items": None}


def _cats(d: dict | None) -> dict:
    """Kategori të kufizuara: çelësa [a-z0-9_]{1,40}, max `MAX_CATEGORIES` (tepricat → `other`)."""
    out: dict[str, int] = {}
    for k, n in sorted((d or {}).items(), key=lambda kv: str(kv[0])):
        key = re.sub(r"[^a-z0-9_]", "_", str(k or "unknown").lower())[:40] or "unknown"
        if key not in out and len(out) >= v1.MAX_CATEGORIES - 1:
            key = "other"
        out[key] = out.get(key, 0) + int(n)
    return out


def _age(now: datetime, t) -> int | None:
    return None if t is None else max(0, int((now - as_utc(t)).total_seconds()))


def _section(name: str, fn: Callable[[], dict | None]):
    try:
        return fn()
    except Exception:  # noqa: BLE001  (seksioni i prishur ⇒ e padisponueshme, pa rrëzuar të tjerët)
        log.warning("sender ops snapshot: section %s unavailable", name, exc_info=True)
        return None


def _sync(db: Session, now: datetime) -> dict:
    cur = db.get(SenderSyncCursor, 1)
    if cur is None:  # `status()` do të krijonte rreshtin e kursorit: snapshot-i është vetëm-lexim
        return {
            "enabled": settings.sender_sync_enabled, "initialized": False, "cursor_seq": None, "latest_central_seq": None, "lag": None, "last_success_age_seconds": None,
            "snapshot_age_seconds": None, "failure_count": 0, "gap_recoveries": 0, "drift_repairs": 0, **_projection_counts(db), "projection_stale": True, "last_error_age_seconds": None,
        }  # fmt: skip
    st = sr.status(db, now)
    return {
        "enabled": bool(st["enabled"]), "initialized": bool(st["initialized"]), "cursor_seq": st["cursor_seq"], "latest_central_seq": st["latest_central_seq"], "lag": st["lag"],
        "last_success_age_seconds": st["last_success_age_seconds"], "snapshot_age_seconds": st["snapshot_age_seconds"], "failure_count": int(st["failure_count"] or 0),
        "gap_recoveries": int(st["gap_recoveries"] or 0), "drift_repairs": int(st["drift_repairs"] or 0), "policy_count": st["policy_count"],
        "authorization_count": st["authorization_count"], "withdrawn_count": st["withdrawn_count"], "projection_stale": sau.projection_stale(db, now),
        "last_error_age_seconds": _age(now, cur.last_error_at),
    }  # fmt: skip


def _projection_counts(db: Session) -> dict:
    from app.models.sender_sync import SyncedSenderAuthorization as A
    from app.models.sender_sync import SyncedSenderPolicy as P

    return {
        "policy_count": db.scalar(select(func.count()).select_from(P).where(P.projection_state == "active")) or 0,
        "authorization_count": db.scalar(select(func.count()).select_from(A).where(A.projection_state == "active")) or 0,
        "withdrawn_count": db.scalar(select(func.count()).select_from(A).where(A.projection_state == "withdrawn")) or 0,
    }  # fmt: skip


def _requests(db: Session, now: datetime) -> dict:
    s = ob.stats(db, now)
    return {
        "enabled": settings.sender_request_reporting, "pending": s["pending"], "retry": s["retry"], "sending": s["sending"], "failed": s["failed"], "sent": s["sent"],
        "stuck_sending": s["stuck_sending"], "oldest_open_age_seconds": s["oldest_open_age_seconds"], "last_sent_age_seconds": s["last_sent_age_seconds"],
        "attempts_max": s["attempts_max"], "failed_by_category": _cats(s["failed_by_category"]),
    }  # fmt: skip


def _shadow(db: Session, now: datetime) -> dict:
    d = ar.drift_summary(db, ar.evidence_since(db, now, settings.sender_evidence_window_hours))
    cat = d["by_category"]
    return {
        "mode": settings.sender_authority, "window_hours": settings.sender_evidence_window_hours, "min_samples": settings.sender_evidence_min_samples,
        "comparisons_total": d["comparisons_total"], "match_total": d["match_total"], "match_rate": d["match_rate"], "critical_total": d["critical_total"],
        "local_allow_central_deny": d["local_allow_central_deny"], "local_deny_central_allow": d["local_deny_central_allow"], "central_missing": d["central_missing"],
        "policy_mismatch": d["policy_mismatch"], "identity_mismatch": int(cat.get("sender_identity_mismatch", 0)), "projection_stale": d["projection_stale"],
        "by_category": _cats(cat),
    }  # fmt: skip


def _bootstrap(db: Session, now: datetime) -> dict:
    st = ar.bootstrap_state(db)
    open_by = dict(
        db.execute(
            select(SenderBootstrapIssue.category, func.count())
            .where(SenderBootstrapIssue.resolved_at.is_(None))
            .group_by(SenderBootstrapIssue.category)
        ).all()
    )
    resolved = (
        db.scalar(
            select(func.count())
            .select_from(SenderBootstrapIssue)
            .where(SenderBootstrapIssue.resolved_at.is_not(None))
        )
        or 0
    )
    state = None
    if st is not None:
        state = {
            "completed": st.completed_at is not None, "version": st.bootstrap_version,
            "completed_at": None if st.completed_at is None else v1.format_ts(as_utc(st.completed_at)),
            "tenant_count": st.tenant_count, "sender_count": st.sender_count, "unresolved_count": st.unresolved_count,
            "report_hash": (st.report_hash or "")[:12] or None,
        }  # fmt: skip
    return {
        "state": state,
        "issues": {
            "open": sum(open_by.values()),
            "resolved": int(resolved),
            "open_by_category": _cats(open_by),
        },
    }


def _cutover(db: Session, now: datetime) -> dict:
    configured = bool(settings.sender_authority_ack)
    last = db.scalars(
        select(SenderCutoverEvidence)
        .order_by(SenderCutoverEvidence.created_at.desc(), SenderCutoverEvidence.id.desc())
        .limit(1)
    ).first()
    return {
        "authority_mode": settings.sender_authority, "dispatch_recheck": settings.sender_dispatch_recheck, "ack_configured": configured,
        "ack_valid": bool(ar.ack_status(db)[0]) if configured else None,
        "evidence_count": db.scalar(select(func.count()).select_from(SenderCutoverEvidence)) or 0,
        "latest_evidence": None if last is None else {
            "kind": last.kind, "hash": last.evidence_hash[:12], "readiness_status": last.readiness_status,
            "created_at": v1.format_ts(as_utc(last.created_at)), "authority_version": last.authority_version,
        },
    }  # fmt: skip


def _alerts(db: Session, now: datetime) -> dict:
    mono = time.monotonic()
    c = _alerts_cache
    if c["mono"] is None or mono - c["mono"] >= ALERTS_TTL_S:
        c["items"] = [
            {"name": a.name, "level": a.level, "value": a.value, "threshold": a.threshold} for a in alerts.evaluate(db, now)
        ]  # fmt: skip
        c["mono"] = mono
    return {"items": list(c["items"]), "evaluated_age_seconds": int(mono - c["mono"])}


def clear_cache() -> None:
    _alerts_cache.update(mono=None, items=None)


def source_revision() -> str | None:
    rev = os.environ.get("SMS_CODE_REVISION", "")
    return rev if _REV.match(rev) else None


def build(db: Session, now: datetime | None = None) -> dict:
    """→ dokument `sender.ops.v1` i vlefshëm (validuar nga kontrata). Vetëm lexim; pa N+1."""
    now = as_utc(now or utcnow())
    data = {
        "sync": _section("sync", lambda: _sync(db, now)),
        "requests": _section("requests", lambda: _requests(db, now)),
        "shadow": _section("shadow", lambda: _shadow(db, now)),
        "bootstrap": _section("bootstrap", lambda: _bootstrap(db, now)),
        "cutover": _section("cutover", lambda: _cutover(db, now)),
        "alerts": _section("alerts", lambda: _alerts(db, now)),
    }
    doc = {
        "schema": v1.SCHEMA, "snapshot_id": str(uuid.uuid4()), "scope": v1.SCOPE, "generated_at": v1.format_ts(now),
        "source_revision": source_revision(), "data": data,
    }  # fmt: skip
    return v1.SenderOpsV1.parse(doc).to_dict()


# --- raportimi ---------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class OpsOutcome:
    kind: str = "ok"  # ok | not_due | rejected | transport_error | auth_error | forbidden_error | protocol_error | build_error
    detail: str = ""
    status: str = ""  # përgjigja e Central: accepted | duplicate | ignored_stale

    @property
    def ok(self) -> bool:
        return self.kind in ("ok", "not_due")


class _State:
    """Numëruesit e vetë-raportimit (në proces; të ndarë nga outbox-i i kërkesave)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.next_due = 0.0
        self.consecutive_failures = 0
        self.sent = 0
        self.failed = 0
        self.last_error = ""


STATE = _State()


def reset_state() -> None:
    STATE.reset()
    clear_cache()


def report_once(
    factory: Callable[[], Session],
    client: ControlPlaneClient,
    *,
    now: datetime | None = None,
    force: bool = False,
    mono: float | None = None,
) -> OpsOutcome:
    t = time.monotonic() if mono is None else mono
    with STATE.lock:
        if not force and t < STATE.next_due:
            return OpsOutcome("not_due")
    try:
        with factory() as db:  # seancë e shkurtër lexuese; mbyllet para rrjetit
            payload = build(db, now)
            db.rollback()
    except Exception as e:  # noqa: BLE001
        return _fail(t, "build_error", type(e).__name__)
    try:
        res = client.post_sender_ops(payload)  # HTTP jashtë çdo transaksioni
    except CpReportRejected as e:
        return _fail(t, "rejected", (e.code or f"http_{e.status}")[:32])
    except CpError as e:
        kind = {
            "CpTransportError": "transport_error",
            "CpAuthError": "auth_error",
            "CpForbidden": "forbidden_error",
        }.get(type(e).__name__, "protocol_error")
        return _fail(t, kind, kind)
    with STATE.lock:
        STATE.consecutive_failures, STATE.last_error = 0, ""
        STATE.sent += 1
        STATE.next_due = t + settings.sender_ops_interval_seconds
    return OpsOutcome("ok", status=res["status"])


def _fail(t: float, kind: str, detail: str) -> OpsOutcome:
    with STATE.lock:
        STATE.consecutive_failures += 1
        STATE.failed += 1
        STATE.last_error = detail
        delay = min(
            settings.sender_ops_interval_seconds * 2 ** min(STATE.consecutive_failures, 4),
            BACKOFF_CAP_S,
        )
        STATE.next_due = t + delay
        n = STATE.consecutive_failures
    if n in (1, 5) or n % 20 == 0:
        log.error("ALERT sender ops snapshot report failing: %d consecutive (last=%s)", n, detail)
    return OpsOutcome(kind, detail)
