"""Shkrimi i audit-it (vetëm-shtim), në transaksionin e thirrësit; pa commit."""

import re
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
        actor_kind="user", actor_id=actor.id, actor_label=None, action=action,
        resource_type=resource_type, resource_id=str(resource_id), detail=detail or None,
        created_at=now or utcnow(),
    )  # fmt: skip
    db.add(row)
    db.flush()
    return row


_LABEL = re.compile(r"^system:[a-z][a-z0-9_.-]{0,56}$")


def record_system(
    db: Session,
    *,
    label: str,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | str,
    detail: dict | None = None,
    now: datetime | None = None,
) -> AuditLog:
    """Audit për proces sistemi (jo njeri): `actor_kind=system`, `actor_label` i detyrueshëm
    (`system:<emër>`), `actor_id` NULL. Pa përdorues të rremë; i njëjti transaksion, pa commit."""
    if not isinstance(label, str) or not _LABEL.match(label):
        raise ValueError("system actor label must look like 'system:<name>' (lowercase, <=64)")
    row = AuditLog(
        actor_kind="system", actor_id=None, actor_label=label, action=action,
        resource_type=resource_type, resource_id=str(resource_id), detail=detail or None,
        created_at=now or utcnow(),
    )  # fmt: skip
    db.add(row)
    db.flush()
    return row
