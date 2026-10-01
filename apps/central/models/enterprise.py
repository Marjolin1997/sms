"""Enterprise në Central (control plane): identiteti kanonik ndër-sistem dhe cikli i jetës.

`id` (UUID) është identiteti kanonik. Nuk mban asgjë operacionale (wallet, sender IDs, kontakte,
fushata, çelësa API, provider, numërues) dhe nuk ka `owner_ref`: ai është kompat vetëm i Enterprise.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from apps.central.core.db import Base
from apps.central.core.timeutil import utcnow


class EnterpriseStatus(enum.StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class Enterprise(Base):
    __tablename__ = "enterprises"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(
        String(16), default=EnterpriseStatus.ACTIVE.value, server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint("status in ('active', 'suspended')", name="status"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
    )
