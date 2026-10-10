"""Raport operacional vetëm-lexim për regjistrimin (M8-e): backlog-e nga DB, jo numërues në-proçes.
Përdoret nga `GET /admin/registration-ops` dhe `tools.registration_readiness`. Pa mutacion, pa retry.

Kushtet e alarmit (përkufizime të përdorura nga readiness): provisioning `failed` (kërkon operator),
`approved+pending` më i vjetër se PENDING_WARN_S/PENDING_FAIL_S, outbox email `pending` i vjetër ose
`failed`, kërkesa `submitted` të pa-verifikuara të vjetra, dhe shenja e shëndetit M7 të dukshme nga
Central: aktiviteti i fundit i konsumatorit (`service_assertion_jti.expires_at` ≈ autentikimi i fundit).
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.central.core.timeutil import utcnow
from apps.central.models.registration import (
    APPROVED,
    FAILED,
    PENDING,
    PROVISIONED,
    SUBMITTED,
    NotificationOutbox,
    RegistrationRequest,
)
from apps.central.models.service_auth import ServiceAssertionJti, ServiceClient, ServiceKey
from apps.central.services.contact_verification import KIND, _aware

PENDING_WARN_S, PENDING_FAIL_S = 3600, 86400
FAILED_FAIL_S = 86400
OUTBOX_WARN_S, OUTBOX_FAIL_S = 900, 3600
UNVERIFIED_WARN_S = 86400
M7_STALE_WARN_S, M7_STALE_FAIL_S = 900, 3600  # SLO 15 min (M7)


def _age(now: datetime, ts) -> int | None:
    return None if ts is None else max(0, int((now - _aware(ts)).total_seconds()))


def report(db: Session, *, now: datetime | None = None) -> dict:
    now = now or utcnow()
    rr = RegistrationRequest

    def count(*where) -> int:
        return db.scalar(select(func.count()).select_from(rr).where(*where)) or 0

    def oldest(col, *where):
        return db.scalar(select(func.min(col)).where(*where))

    pending_w = (rr.status == APPROVED, rr.provisioning_status == PENDING)
    failed_w = (rr.status == APPROVED, rr.provisioning_status == FAILED)
    unver_w = (rr.status == SUBMITTED, rr.verified_at.is_(None))
    ob = NotificationOutbox
    ob_pending = (ob.kind == KIND, ob.state.in_(("pending", "sending")))
    last_consumer = db.scalar(select(func.max(ServiceAssertionJti.expires_at)))
    clients = (
        db.scalar(
            select(func.count(func.distinct(ServiceClient.id)))
            .join(ServiceKey, ServiceKey.client_pk == ServiceClient.id)
            .where(ServiceClient.status == "active", ServiceKey.status == "active")
        )
        or 0
    )
    recent = count(
        rr.provisioning_status == PROVISIONED, rr.updated_at > now - timedelta(seconds=3600)
    )
    return {
        "generated_at": now.isoformat(),
        "provisioning": {
            "failed": count(*failed_w),
            "failed_oldest_age_s": _age(now, oldest(rr.updated_at, *failed_w)),
            "pending": count(*pending_w),
            "pending_oldest_age_s": _age(now, oldest(rr.decided_at, *pending_w)),
            "provisioned_last_hour": recent,
        },
        "verification": {
            "unverified_submitted": count(*unver_w),
            "unverified_oldest_age_s": _age(now, oldest(rr.created_at, *unver_w)),
        },
        "email_outbox": {
            "pending": db.scalar(select(func.count()).select_from(ob).where(*ob_pending)) or 0,
            "pending_oldest_age_s": _age(
                now, db.scalar(select(func.min(ob.available_at)).where(*ob_pending))
            ),
            "failed": db.scalar(
                select(func.count()).select_from(ob).where(ob.kind == KIND, ob.state == "failed")
            )
            or 0,
        },
        "control_plane": {
            "active_consumer_clients": clients,
            "consumer_last_seen_age_s": _age(now, last_consumer),
        },
    }
