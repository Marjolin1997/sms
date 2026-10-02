"""Shkrimi i audit-it (vetëm-shtim), në transaksionin e thirrësit; pa commit."""

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from apps.central.core.timeutil import utcnow
from apps.central.models.audit import AuditLog
from apps.central.models.user import CentralUser


def record(
    db: Session,
    actor: CentralUser,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | str,
    detail: dict | None = None,
    *,
    now: datetime | None = None,
) -> AuditLog:
    row = AuditLog(
        actor_id=actor.id, action=action, resource_type=resource_type,
        resource_id=str(resource_id), detail=detail or None, created_at=now or utcnow(),
    )  # fmt: skip
    db.add(row)
    db.flush()
    return row
