"""M10-S2: gatishmëria e sinkronizimit të sender-ave (`sender_sync_readiness`) — VETËM LEXIM, pa PII (asnjë vlerë sender në rezultat), pa rrjet. Jo gatishmëria finale e autoritetit (S4/S5).

PASS/WARN/FAIL: kursori i vlefshëm · snapshot i njohur · mosha e suksesit të fundit (vetëm raportim: s'skadon kurrë miratime) · lag · gabimi i fundit · konsistenca e projeksionit · revizione ·
marrëdhënia politikë↔regjistër (asnjë i miratuar nën politikë `allowed=false`)."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import as_utc, utcnow
from app.models.sender_sync import SyncedSenderAuthorization, SyncedSenderPolicy
from app.services import sender_sync as ss

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
LAG_WARN = 1000


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: str
    reason: str


def _c(name, bad, ok, bad_text, level=FAIL) -> Check:
    return Check(name, level if bad else PASS, bad_text if bad else ok)


def status(db: Session, now: datetime | None = None) -> dict:
    """Metrika të sigurta (numra/mosha; pa vlera sender)."""
    now = as_utc(now or utcnow())
    cur = ss.get_cursor(db)
    snap_age = (
        None
        if cur.last_snapshot_at is None
        else max(0, int((now - as_utc(cur.last_snapshot_at)).total_seconds()))
    )
    age = ss.sync_age_seconds(cur, now)
    return {
        "enabled": settings.sender_sync_enabled, "initialized": cur.epoch is not None, "cursor_seq": cur.last_seq, "latest_central_seq": cur.latest_central_seq,
        "lag": None if cur.latest_central_seq is None else max(0, cur.latest_central_seq - cur.last_seq), "last_success_age_seconds": None if age is None else int(age),
        "snapshot_age_seconds": snap_age, "policy_count": db.scalar(select(func.count()).select_from(SyncedSenderPolicy).where(SyncedSenderPolicy.projection_state == "active")) or 0,
        "authorization_count": db.scalar(select(func.count()).select_from(SyncedSenderAuthorization).where(SyncedSenderAuthorization.projection_state == "active")) or 0,
        "withdrawn_count": db.scalar(select(func.count()).select_from(SyncedSenderAuthorization).where(SyncedSenderAuthorization.projection_state == "withdrawn")) or 0,
        "failure_count": cur.failure_count, "gap_recoveries": cur.gap_recoveries, "drift_repairs": cur.drift_repairs, "last_error_at": None if cur.last_error_at is None else as_utc(cur.last_error_at).isoformat(),
    }  # fmt: skip


def checks(db: Session, now: datetime | None = None) -> list[Check]:
    now = as_utc(now or utcnow())
    cur = ss.get_cursor(db)
    out: list[Check] = []
    out.append(
        Check(
            "sync_enabled",
            PASS if settings.sender_sync_enabled else WARN,
            "enabled"
            if settings.sender_sync_enabled
            else "SMS_SENDER_SYNC_ENABLED is false (S2 informational: projection is not used by SMS)",
        )
    )
    bad_cursor = (
        cur.last_seq < 0
        or (cur.epoch is None) != (cur.authorization_generation is None)
        or (cur.snapshot_seq is not None and cur.snapshot_seq > cur.last_seq and cur.last_seq != 0)
    )
    out.append(
        _c("cursor_valid", bad_cursor, "cursor is coherent", "cursor fields are inconsistent")
    )
    out.append(
        Check(
            "generation_known",
            PASS if cur.authorization_generation is not None else FAIL,
            "authorization generation known"
            if cur.authorization_generation is not None
            else "no snapshot yet: generation unknown",
        )
    )
    out.append(
        Check(
            "snapshot_known",
            PASS if cur.last_snapshot_at is not None else FAIL,
            "snapshot applied"
            if cur.last_snapshot_at is not None
            else "no snapshot has been applied",
        )
    )
    age = ss.sync_age_seconds(cur, now)
    lvl = FAIL if age is None or age > ss.SLO_AGE_S else (WARN if age > ss.ALERT_AGE_S else PASS)
    out.append(
        Check(
            "sync_fresh", lvl, "never succeeded" if age is None else f"last success {int(age)}s ago"
        )
    )
    lag = None if cur.latest_central_seq is None else max(0, cur.latest_central_seq - cur.last_seq)
    out.append(
        Check(
            "sync_lag",
            PASS if not lag else WARN,
            "no lag" if not lag else f"{lag} events behind the last known Central seq",
        )
    )
    errored = cur.last_error_at is not None and (
        cur.last_success_at is None or as_utc(cur.last_error_at) > as_utc(cur.last_success_at)
    )
    out.append(
        Check(
            "no_unresolved_error",
            WARN if errored else PASS,
            "last sync error is newer than the last success" if errored else "none",
        )
    )
    auth = list(db.scalars(select(SyncedSenderAuthorization)))
    bad_rows = [
        a.registry_id for a in auth
        if (a.approved_key is not None) != (a.status == "approved" and a.projection_state == "active")
        or (a.approved_key is not None and a.approved_key != f"{a.country}:{a.norm_value}")
        or a.norm_value != a.display_value.lower()
    ]  # fmt: skip
    out.append(
        _c(
            "projection_consistent",
            bad_rows,
            f"{len(auth)} authorization row(s) consistent",
            f"{len(bad_rows)} projected row(s) are inconsistent",
        )
    )
    bad_rev = [a.registry_id for a in auth if a.cp_revision < 1 or a.cp_seq > cur.last_seq]
    pol = list(db.scalars(select(SyncedSenderPolicy)))
    bad_rev += [p.policy_id for p in pol if p.policy_revision < 1 or p.cp_seq > cur.last_seq]
    out.append(
        _c(
            "revisions_sane",
            bad_rev,
            "revisions and seq within the cursor",
            f"{len(bad_rev)} row(s) ahead of the cursor or with invalid revisions",
        )
    )
    denied = {
        (p.country, p.sender_kind) for p in pol if p.projection_state == "active" and not p.allowed
    }
    mixed = [
        a.registry_id
        for a in auth
        if a.projection_state == "active"
        and a.status == "approved"
        and (a.country, a.sender_kind) in denied
    ]
    out.append(
        _c(
            "policy_registry_coherent",
            mixed,
            "no approved sender under a disallowing policy",
            f"{len(mixed)} approved sender(s) under an allowed=false policy (mixed state)",
        )
    )
    return out


def overall(items: list[Check]) -> str:
    return (
        FAIL
        if any(c.level == FAIL for c in items)
        else (WARN if any(c.level == WARN for c in items) else PASS)
    )
