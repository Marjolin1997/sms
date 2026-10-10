"""M10-S5: alerte operacionale të sender policy — vlerësim vetëm-lexim; emra të kufizuar, ASNJË label me vlerë sender (vetëm numra/kategori).

Çdo alertë: `name`, `level` (ok|warning|critical), `value`, `threshold`. Integrimi (Prometheus/cron) lexon `python -m scripts.sender_alerts --json` (kod 1 kur ka critical)."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import as_utc, utcnow
from app.models.sender_authority import SenderBootstrapState
from app.models.sender_sync import SenderSyncCursor
from app.models.sending import Message
from app.services import sender_authority as sau
from app.services import sender_authority_readiness as ar
from app.services import sender_policy_readiness as pr
from app.services import sender_request_outbox as ob
from app.services import sender_sync as ss

LAG_WARN = 1000
DENY_RATIO, DENY_MIN = 0.2, 20


@dataclass(frozen=True, slots=True)
class Alert:
    name: str
    level: str
    value: float | int | None
    threshold: float | int | None


def _lvl(v, warn, crit=None):
    if v is None:
        return "ok"
    if crit is not None and v >= crit:
        return "critical"
    return "warning" if v >= warn else "ok"


def evaluate(db: Session, now: datetime | None = None) -> list[Alert]:
    now = as_utc(now or utcnow())
    cur = db.get(SenderSyncCursor, 1)
    out: list[Alert] = []
    lag = (
        None
        if (cur is None or cur.latest_central_seq is None)
        else max(0, cur.latest_central_seq - cur.last_seq)
    )
    out.append(Alert("sender_sync_lag", _lvl(lag, LAG_WARN), lag, LAG_WARN))
    age = None if cur is None else ss.sync_age_seconds(cur, now)
    out.append(
        Alert(
            "sender_sync_age_seconds",
            "critical"
            if (cur is not None and age is None)
            else _lvl(age, ss.ALERT_AGE_S, ss.SLO_AGE_S),
            None if age is None else int(age),
            ss.ALERT_AGE_S,
        )
    )
    gaps = 0 if cur is None else (cur.gap_recoveries or 0)
    out.append(Alert("sender_sync_gap_recoveries_total", "warning" if gaps else "ok", gaps, 1))
    st = ob.stats(db, now)
    out.append(
        Alert(
            "sender_request_oldest_age_seconds",
            _lvl(
                st["oldest_open_age_seconds"],
                settings.sender_request_alert_age_seconds,
                settings.sender_request_alert_age_seconds * 6,
            ),
            st["oldest_open_age_seconds"],
            settings.sender_request_alert_age_seconds,
        )
    )
    out.append(
        Alert(
            "sender_request_permanent_failures",
            "critical" if st["failed"] else "ok",
            st["failed"],
            1,
        )
    )
    b = db.get(SenderBootstrapState, 1)
    unresolved = (b.unresolved_count if b else 0) + pr.open_blocking_issues(db)
    out.append(
        Alert(
            "sender_bootstrap_unresolved",
            ("critical" if settings.sender_authority == "central" else "warning")
            if unresolved
            else "ok",
            unresolved,
            1,
        )
    )
    d = ar.drift_summary(db, ar.evidence_since(db, now, settings.sender_evidence_window_hours))
    out.append(
        Alert(
            "sender_shadow_critical_drift",
            "critical" if d["critical_total"] else "ok",
            d["critical_total"],
            1,
        )
    )
    s = sau.stats_snapshot()
    denied = sum(v for k, v in s.items() if k.startswith("central_denied_"))
    total = denied + s.get("central_allowed", 0)
    ratio = None if total < DENY_MIN else round(denied / total, 4)
    out.append(
        Alert(
            "sender_central_deny_ratio",
            "warning" if (ratio is not None and ratio >= DENY_RATIO) else "ok",
            ratio,
            DENY_RATIO,
        )
    )
    blocked = (
        db.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.error_code.in_(list(sau.BLOCK_CODES.values())),
                Message.updated_at >= now - timedelta(hours=1),
            )
        )
        or 0
    )
    out.append(
        Alert("sender_dispatch_recheck_blocked_1h", "warning" if blocked else "ok", blocked, 1)
    )
    stale = sau.projection_stale(db, now)
    out.append(Alert("sender_projection_stale", "warning" if stale else "ok", int(stale), 1))
    r = pr.overall(pr.checks(db, now=now))
    out.append(
        Alert(
            "sender_policy_readiness_fail", "critical" if r == "FAIL" else "ok", int(r == "FAIL"), 1
        )
    )
    return out
