"""M10-S3: gatishmëria e transportit të kërkesave të sender-ave (`sender_request_readiness`) — VETËM LEXIM, pa PII (asnjë vlerë sender), pa rrjet. NUK është gatishmëria finale e autoritetit (`sender_policy_readiness`, më vonë).

PASS/WARN/FAIL: raportuesi aktiv · endpoint/çelës i konfiguruar · https në prodhim · skop i pranuar nga Central (nxirret nga gabimet e dërgimit; Enterprise s'sheh konfigurimin e Central) · dështime
permanente · mosha e veprimit më të vjetër të hapur · rreshta `sending` me lease të skaduar · dërgimi i fundit i suksesshëm."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import as_utc, utcnow
from app.models.sender_request import Q_RETRY, SenderRequestOutbox
from app.services import sender_request_outbox as ob

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: str
    reason: str


def status(db: Session, now: datetime | None = None) -> dict:
    st = ob.stats(db, now)
    st["enabled"] = settings.sender_request_reporting
    return st


def checks(db: Session, now: datetime | None = None) -> list[Check]:
    now = as_utc(now or utcnow())
    st = ob.stats(db, now)
    alert = settings.sender_request_alert_age_seconds
    out: list[Check] = []
    on = settings.sender_request_reporting
    out.append(
        Check(
            "reporter_enabled",
            PASS if on else WARN,
            "enabled"
            if on
            else "SMS_SENDER_REQUEST_REPORTING is false: requests accumulate locally and are not sent to Central",
        )
    )
    missing = [
        n
        for n, v in (
            ("SMS_CP_BASE_URL", settings.cp_base_url),
            ("SMS_CP_CLIENT_ID", settings.cp_client_id),
            ("SMS_CP_KEY_ID", settings.cp_key_id),
            ("SMS_CP_PRIVATE_KEY_PATH", settings.cp_private_key_path),
        )
        if not v
    ]
    key_ok = bool(settings.cp_private_key_path) and Path(settings.cp_private_key_path).is_file()
    if missing:
        out.append(
            Check("endpoint_configured", FAIL if on else WARN, f"missing: {', '.join(missing)}")
        )
    elif not key_ok:
        out.append(
            Check("endpoint_configured", FAIL if on else WARN, "private key file is not readable")
        )
    else:
        out.append(Check("endpoint_configured", PASS, "endpoint, client and key configured"))
    https = settings.cp_base_url.startswith("https://")
    prod_bad = settings.env == "production" and bool(settings.cp_base_url) and not https
    out.append(
        Check(
            "https_required",
            FAIL if prod_bad else PASS,
            "SMS_CP_BASE_URL must be https:// in production" if prod_bad else "ok",
        )
    )
    last = db.scalar(
        select(SenderRequestOutbox.last_error_code)
        .where(SenderRequestOutbox.state == Q_RETRY)
        .order_by(SenderRequestOutbox.updated_at.desc())
        .limit(1)
    )
    denied = last in ("forbidden", "auth")
    out.append(
        Check(
            "scope_granted",
            FAIL if denied else PASS,
            f"Central rejected the credentials/scope ({last}): grant sender:report and authorize the enterprise"
            if denied
            else (
                "no auth/scope error observed"
                if st["sent"]
                else "not yet verified (no successful delivery)"
            ),
        )
    )
    out.append(
        Check(
            "no_permanent_failures",
            FAIL if st["failed"] else PASS,
            f"{st['failed']} request(s) rejected permanently by Central {st['failed_by_category']}"
            if st["failed"]
            else "none",
        )
    )
    age = st["oldest_open_age_seconds"]
    lvl = PASS if age is None or age <= alert else (WARN if age <= alert * 6 else FAIL)
    out.append(
        Check(
            "oldest_pending_age",
            lvl,
            "nothing pending" if age is None else f"oldest open request is {age}s old",
        )
    )
    out.append(
        Check(
            "no_stuck_sending",
            WARN if st["stuck_sending"] else PASS,
            f"{st['stuck_sending']} row(s) have an expired lease (reporter down?)"
            if st["stuck_sending"]
            else "none",
        )
    )
    ls = st["last_sent_age_seconds"]
    stale = (st["pending"] + st["retry"] + st["sending"]) > 0 and (ls is None or ls > alert * 6)
    out.append(
        Check(
            "recent_delivery",
            WARN if stale else PASS,
            "requests are waiting and no delivery succeeded recently" if stale else "ok",
        )
    )
    return out


def overall(items: list[Check]) -> str:
    return (
        FAIL
        if any(c.level == FAIL for c in items)
        else (WARN if any(c.level == WARN for c in items) else PASS)
    )
