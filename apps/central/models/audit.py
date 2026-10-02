"""Audit minimal i Central: një rresht për çdo veprim shkrimi të një aktori të autentikuar.

Shtohet në të njëjtin transaksion me ndryshimin. Vetëm-shtim (ORM refuzon UPDATE/DELETE).
Pa event sourcing, pa audit të leximeve.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Uuid, event
from sqlalchemy.orm import Mapped, mapped_column

from apps.central.core.db import Base
from apps.central.core.timeutil import utcnow


class AuditImmutableError(RuntimeError):
    pass


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="RESTRICT"))
    action: Mapped[str] = mapped_column(String(48))  # p.sh. "product.create"
    resource_type: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(
        JSON
    )  # {"before": {...}, "after": {...}} vetëm fushat
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_audit_log_resource", "resource_type", "resource_id", "created_at"),)


@event.listens_for(AuditLog, "before_update")
@event.listens_for(AuditLog, "before_delete")
def _append_only(*_) -> None:
    raise AuditImmutableError("audit rows are append-only")
