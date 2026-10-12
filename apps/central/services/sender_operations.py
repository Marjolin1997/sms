"""M11-D: pamje operacionale e qeverisjes së sender-ave për konsolën Central.

VETËM LEXIM: asnjë shkrim, asnjë auditim për lexim, asnjë rrjet.

Parimet:
- Asnjë logjikë e dytë gatishmërie: kontrollet e Central vijnë nga
  `sender_central_readiness.checks` (i njëjti kod si CLI); këtu vetëm mapohen në skemë të
  qëndrueshme.
- Vetëm çfarë Central ka si provë: feed-i (sekuenca/outbox), kërkesat e pranuara nga Enterprise
  (`sender_request_operations`) dhe ekzekutimet bootstrap. Gjendja runtime e Enterprise (kursori,
  lag-u, outbox i kërkesave, shadow/drift, cutover, alarmet) nuk është e dukshme nga Central,
  ndaj del si kontroll `unknown` i shpjeguar, kurrë `pass`.
- Pa vlera sender, pa sekrete: vetëm numërues, kode, kohë dhe prefikse hash-esh.
- I kufizuar: pyetje agregate/PK; kontrollet e plota të konsistencës ruhen në cache në proces për
  `READINESS_TTL_SECONDS`; asnjë thirrje nga shtigjet SMS.
Statuset: pass | warn | fail | unknown. Gjithsej/seksion: fail > warn > unknown > pass."""

import re
import threading
from datetime import UTC, datetime, timedelta
from time import monotonic

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.central.core.timeutil import utcnow
from apps.central.models.sender import (
    CountrySenderPolicy,
    SenderBootstrapRun,
    SenderRegistry,
    SenderRequestOperation,
    SenderSyncOutbox,
    SenderSyncSequence,
)
from apps.central.services import sender_central_readiness as readiness

PASS, WARN, FAIL, UNKNOWN = "pass", "warn", "fail", "unknown"
READINESS_TTL_SECONDS = 30.0
_SAFE_REV = re.compile(r"^[0-9A-Za-z._-]{1,64}$")
_RANK = {PASS: 0, UNKNOWN: 1, WARN: 2, FAIL: 3}

# Prova që ekziston VETËM te Enterprise: shpjegohet, kurrë e shpikur.
# `tool` = komanda read-only që e prodhon në host-in Enterprise.
ENTERPRISE_ONLY = {
    "enterprise_sync_runtime": "scripts.sender_sync_readiness",
    "enterprise_request_outbox": "scripts.sender_request_readiness",
    "bootstrap_issue_categories": "scripts.sender_bootstrap",
    "shadow_evidence": "scripts.sender_authority_readiness",
    "cutover_evidence": "scripts.sender_policy_readiness",
    "enterprise_alerts": "scripts.sender_alerts",
}

_cache: dict = {"at": None, "checks": None, "iso": None}
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _cache.update(at=None, checks=None, iso=None)


def _iso(d: datetime | None) -> str | None:
    if d is None:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).isoformat()


def _check(code: str, status: str, summary: str, **details) -> dict:
    return {"code": code, "status": status, "summary": summary, "details": details}


def _unknown(code: str) -> dict:
    return _check(
        code,
        UNKNOWN,
        "Not available to Central: this evidence lives on the Enterprise side.",
        source="enterprise",
        tool=ENTERPRISE_ONLY[code],
    )


def worst(statuses) -> str:
    return max(statuses, key=_RANK.__getitem__, default=UNKNOWN)


def _central_checks(db: Session, now_mono: float) -> tuple[list[dict], bool, str | None]:
    """→ (checks, from_cache, computed_at_iso). Kontrollet e plota lexojnë regjistrin:
    cache TTL që të mos përsëriten në çdo rifreskim/polling."""
    with _lock:
        if _cache["at"] is not None and now_mono - _cache["at"] < READINESS_TTL_SECONDS:
            return _cache["checks"], True, _cache["iso"]
    items = readiness.checks(db)
    checks = [_check(c.name, c.level.lower(), c.reason) for c in items]
    iso = _iso(utcnow())
    with _lock:
        _cache.update(at=now_mono, checks=checks, iso=iso)
    return checks, False, iso


def _sync(db: Session) -> tuple[dict, list[dict]]:
    row = db.execute(
        select(
            SenderSyncSequence.epoch, SenderSyncSequence.floor_seq, SenderSyncSequence.last_seq
        ).where(SenderSyncSequence.id == 1)
    ).one_or_none()
    if row is None:
        return {}, [
            _check(
                "sync_sequence_consistent",
                UNKNOWN,
                "The sender sync sequence has not been initialized.",
            ),
            _unknown("enterprise_sync_runtime"),
        ]
    last, floor = int(row.last_seq), int(row.floor_seq)
    tail = (
        db.scalar(select(SenderSyncOutbox.created_at).where(SenderSyncOutbox.seq == last))
        if last
        else None
    )
    head = (
        db.scalar(select(SenderSyncOutbox.created_at).where(SenderSyncOutbox.seq == floor + 1))
        if last > floor
        else None
    )
    ok = last == 0 or tail is not None
    metrics = {
        "epoch": str(row.epoch)[:8],
        "last_seq": last,
        "floor_seq": floor,
        "retained_events": last - floor,
        "last_event_at": _iso(tail),
        "oldest_retained_at": _iso(head),
    }
    checks = [
        _check(
            "sync_sequence_consistent",
            PASS if ok else FAIL,
            "Feed tail present in the outbox."
            if ok
            else "The latest feed sequence has no outbox event (gap).",
            last_seq=last,
            floor_seq=floor,
        ),
        _unknown("enterprise_sync_runtime"),
    ]
    return metrics, checks


def _requests(db: Session, since: datetime) -> tuple[dict, list[dict]]:
    by_outcome = dict(
        db.execute(
            select(SenderRequestOperation.outcome, func.count()).group_by(
                SenderRequestOperation.outcome
            )
        ).all()
    )
    by_auto = dict(
        db.execute(
            select(SenderRequestOperation.auto, func.count()).group_by(SenderRequestOperation.auto)
        ).all()
    )
    total = sum(by_outcome.values())
    last = db.scalar(select(func.max(SenderRequestOperation.received_at)))
    day = (
        db.scalar(
            select(func.count())
            .select_from(SenderRequestOperation)
            .where(SenderRequestOperation.received_at >= since)
        )
        or 0
    )
    metrics = {
        "total": total,
        "by_outcome": by_outcome,
        "by_auto": by_auto,
        "last_received_at": _iso(last),
        "received_24h": day,
    }
    ingest = (
        _check(
            "request_ingest",
            UNKNOWN,
            "No sender request from Enterprise has been received yet (no evidence).",
        )
        if total == 0
        else _check(
            "request_ingest",
            PASS,
            "Sender requests from Enterprise are being received.",
            total=total,
        )
    )
    return metrics, [ingest, _unknown("enterprise_request_outbox")]


def _bootstrap(db: Session) -> tuple[dict, list[dict]]:
    by_status = dict(
        db.execute(
            select(SenderBootstrapRun.status, func.count()).group_by(SenderBootstrapRun.status)
        ).all()
    )
    run = db.scalars(
        select(SenderBootstrapRun).order_by(SenderBootstrapRun.started_at.desc()).limit(1)
    ).first()
    if run is None:
        return {"runs": by_status, "latest": None}, [
            _check("bootstrap_run", UNKNOWN, "No bootstrap run is recorded in Central."),
            _unknown("bootstrap_issue_categories"),
        ]
    rev = (
        run.source_revision
        if run.source_revision and _SAFE_REV.match(run.source_revision)
        else None
    )
    latest = {
        "status": run.status,
        "started_at": _iso(run.started_at),
        "completed_at": _iso(run.completed_at),
        "tenant_count": run.tenant_count,
        "sender_count": run.sender_count,
        "imported_count": run.imported_count,
        "unresolved_count": run.unresolved_count,
        "artifact_hash": run.artifact_hash[:12],
        "report_hash": (run.report_hash or "")[:12] or None,
        "source_revision": rev,
    }
    state = {
        "completed": (PASS, "The latest bootstrap run completed."),
        "running": (WARN, "The latest bootstrap run is still marked as running."),
        "failed": (FAIL, "The latest bootstrap run failed."),
    }[run.status]
    checks = [_check("bootstrap_run", state[0], state[1], run_status=run.status)]
    if run.status == "completed":
        n = run.unresolved_count
        checks.append(
            _check(
                "bootstrap_unresolved",
                WARN if n else PASS,
                f"The latest run reported {n} unresolved item(s); they block cutover readiness."
                if n
                else "The latest run reported no unresolved items.",
                unresolved_count=n,
                blocks_cutover=bool(n),
            )
        )
    else:
        checks.append(
            _check("bootstrap_unresolved", UNKNOWN, "No completed bootstrap run to evaluate.")
        )
    checks.append(_unknown("bootstrap_issue_categories"))
    return {"runs": by_status, "latest": latest}, checks


def _registry(db: Session) -> dict:
    by_status = dict(
        db.execute(
            select(SenderRegistry.current_status, func.count()).group_by(
                SenderRegistry.current_status
            )
        ).all()
    )
    scopes = (
        db.scalar(
            select(func.count()).select_from(
                select(CountrySenderPolicy.country, CountrySenderPolicy.sender_kind)
                .group_by(CountrySenderPolicy.country, CountrySenderPolicy.sender_kind)
                .subquery()
            )
        )
        or 0
    )
    return {
        "senders_by_status": by_status,
        "senders_total": sum(by_status.values()),
        "policy_scopes": scopes,
    }


_ALERT_CODE = {
    "sync_sequence_consistent": "sync_gap",
    "bootstrap_unresolved": "bootstrap_unresolved",
    "bootstrap_run": "bootstrap_failed",
}


def _alerts(sections: list[dict], observed_at: str) -> list[dict]:
    out = []
    for s in sections:
        for c in s["checks"]:
            if c["status"] in (WARN, FAIL) and not (
                c["code"] == "bootstrap_run" and c["status"] == WARN
            ):  # `running` është gjendje normale gjatë ekzekutimit
                code = _ALERT_CODE.get(
                    c["code"], "readiness_fail" if s["code"] == "central_readiness" else c["code"]
                )
                out.append(
                    {
                        "code": code,
                        "severity": "critical" if c["status"] == FAIL else "warning",
                        "check": c["code"],
                        "section": s["code"],
                        "summary": c["summary"],
                        "observed_at": observed_at,
                    }
                )
    return out


def snapshot(db: Session, *, now: datetime | None = None, mono: float | None = None) -> dict:
    now = now or utcnow()
    n = _iso(now)
    central, cached, computed_at = _central_checks(db, monotonic() if mono is None else mono)
    sync_m, sync_c = _sync(db)
    req_m, req_c = _requests(
        db, (now if now.tzinfo else now.replace(tzinfo=UTC)) - timedelta(hours=24)
    )
    boot_m, boot_c = _bootstrap(db)
    sections = [
        {"code": "central_readiness", "checks": central},
        {"code": "sync", "checks": sync_c},
        {"code": "requests", "checks": req_c},
        {"code": "bootstrap", "checks": boot_c},
        {
            "code": "enterprise_evidence",
            "checks": [
                _unknown("shadow_evidence"),
                _unknown("cutover_evidence"),
                _unknown("enterprise_alerts"),
            ],
        },
    ]
    for s in sections:
        s["status"] = worst(c["status"] for c in s["checks"])
    return {
        "status": worst(s["status"] for s in sections),
        "generated_at": n,
        "readiness_computed_at": computed_at,
        "readiness_cached": cached,
        "readiness_ttl_seconds": int(READINESS_TTL_SECONDS),
        "sections": sections,
        "metrics": {
            "registry": _registry(db),
            "sync": sync_m,
            "requests": req_m,
            "bootstrap": boot_m,
        },
        "alerts": _alerts(sections, n),
    }
