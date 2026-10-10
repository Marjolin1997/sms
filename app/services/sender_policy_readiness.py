"""M10-S5: `sender_policy_readiness` — agregatori FINAL i gatishmërisë së M10 (Enterprise). VETËM LEXIM, pa rrjet, pa vlera sender.

Përmbledh provat e S1 (Central: nga JSON-i i `apps.central.tools.sender_central_readiness --json`, i dhënë shprehimisht), S2 (sinkronizimi/projeksioni), S3 (transporti), S4 (shadow/bootstrap/ngrirja) dhe S5
(skema aktuale, rikontrolli para dispatch-it, çështjet e bootstrap-it, ACK i lidhur me provën, siguria e konfigurimit, rikthimi).

Dallimi kyç: **autorizimi është fail-static** (projeksion i vjetër NUK mohon dërgime) por **gatishmëria operacionale mund të dështojë** (sinkronizim i ndërprerë ⇒ FAIL për cutover/vazhdim operimi).
`target="central"` vlerëson gatishmërinë për të kaluar në central (përdoret për provën `pre_cutover`, para se ACK të ekzistojë)."""

import hashlib
import json
from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core import readiness as core_readiness
from app.core.config import settings
from app.core.timeutil import as_utc, utcnow
from app.models.sender_authority import SenderBootstrapIssue
from app.models.sender_sync import SyncedSenderAuthorization
from app.services import sender_authority as sau
from app.services import sender_authority_readiness as ar
from app.services import sender_request_readiness as rr

PASS, WARN, FAIL = ar.PASS, ar.WARN, ar.FAIL
Check = ar.Check


def migration_state() -> tuple[str | None, str | None]:
    """(version në DB, koka e kodit). Funksion i veçantë që testet ta zëvendësojnë."""
    from app.core.db import engine

    head = core_readiness._head()
    try:
        with engine.connect() as c:
            cur = c.execute(
                text(f"select version_num from {core_readiness.VERSION_TABLE}")
            ).scalar()
    except Exception:  # noqa: BLE001
        cur = None
    return cur, head


def open_blocking_issues(db: Session) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(SenderBootstrapIssue)
            .where(SenderBootstrapIssue.resolved_at.is_(None))
        )
        or 0
    )


def checks(
    db: Session,
    *,
    now: datetime | None = None,
    target: str | None = None,
    central_readiness: dict | None = None,
    min_samples: int | None = None,
    window_hours: int | None = None,
) -> list[Check]:
    """`target="central"`: vlerëso gatishmërinë për central pavarësisht modës aktuale (pa kërkuar ACK, që krijohet pas kësaj)."""
    now = as_utc(now or utcnow())
    tgt = target or settings.sender_authority
    base = ar.checks(db, now=now, min_samples=min_samples, window_hours=window_hours)
    out: list[Check] = []
    for c in base:
        if target == "central" and c.name == "authority_mode":
            out.append(
                Check(
                    "authority_mode",
                    PASS if settings.sender_authority in ("shadow", "central") else FAIL,
                    f"current mode={settings.sender_authority}; evidence is collected in shadow",
                )
            )
        elif target == "central" and c.name == "production_ack":
            out.append(Check("production_ack", PASS, "ACK is issued from this evidence"))
        else:
            out.append(c)
    cur, head = migration_state()
    out.append(
        Check(
            "migrations_current",
            PASS if (cur is not None and cur == head) else FAIL,
            f"schema {cur} == code head {head}"
            if cur == head and cur
            else f"schema {cur} != code head {head}",
        )
    )
    n_issues = open_blocking_issues(db)
    out.append(
        Check(
            "bootstrap_issues_resolved",
            FAIL if n_issues else PASS,
            f"{n_issues} open bootstrap issue(s)" if n_issues else "no open bootstrap issue",
        )
    )
    recheck_ok = settings.sender_dispatch_recheck
    out.append(
        Check(
            "dispatch_recheck_enabled",
            PASS if recheck_ok else (FAIL if tgt == "central" else WARN),
            "dispatch recheck active for Central-authorised messages"
            if recheck_ok
            else "SMS_SENDER_DISPATCH_RECHECK is false",
        )
    )
    out.append(
        Check(
            "review_freeze",
            PASS if (tgt != "central" or sau.frozen() or target == "central") else FAIL,
            "local review freezes under central (code-enforced)",
        )
    )
    out.append(
        Check(
            "read_model_available",
            PASS,
            "effective sender read model is served by the API (additive fields)",
        )
    )
    # shtrirja e skopeve: Enterprise s'sheh konfigurimin e Central; prova është dërgimi/sinkronizimi i suksesshëm
    tr = {c.name: c for c in rr.checks(db, now)}
    synced = db.scalar(select(func.count()).select_from(SyncedSenderAuthorization)) or 0
    out.append(
        Check(
            "scope_sender_read",
            PASS if synced else WARN,
            "feed read proven by synchronized state"
            if synced
            else "no synchronized sender yet: sender:read not yet proven",
        )
    )
    out.append(
        Check(
            "scope_sender_report",
            FAIL if tr["scope_granted"].level == FAIL else PASS,
            tr["scope_granted"].reason,
        )
    )
    sec = (
        [
            p
            for p in settings.production_problems()
            if "SENDER" in p and not (target == "central" and "AUTHORITY_ACK" in p)
        ]
        if settings.env == "production"
        else []
    )
    out.append(
        Check(
            "production_security_config",
            FAIL if sec else PASS,
            "; ".join(sec) if sec else "ok (or not production)",
        )
    )
    rb = {c.name: c for c in ar.rollback_checks(db, "local")}
    out.append(
        Check(
            "rollback_safety",
            WARN if rb["reauthorize_risk"].level == FAIL else PASS,
            rb["reauthorize_risk"].reason + " (rollback to local would be blocked)"
            if rb["reauthorize_risk"].level == FAIL
            else "rollback to local is safe",
        )
    )
    if central_readiness is None:
        out.append(
            Check(
                "central_readiness",
                WARN,
                "Central S1 readiness JSON not provided (--central-readiness)",
            )
        )
    else:
        lvl = central_readiness.get("status")
        out.append(
            Check(
                "central_readiness",
                {"PASS": PASS, "WARN": WARN}.get(lvl, FAIL),
                f"Central S1 readiness: {lvl}",
            )
        )
    return out


def overall(items: list[Check]) -> str:
    return ar.overall(items)


def readiness_hash(items: list[Check]) -> str:
    doc = sorted((c.name, c.level) for c in items)
    return hashlib.sha256(json.dumps(doc, separators=(",", ":")).encode()).hexdigest()
