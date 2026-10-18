"""Stafi i Central (control plane): identitet i veçantë, pa lidhje me përdoruesit e Enterprise."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from apps.central.core.db import Base
from apps.central.core.timeutil import utcnow


class Role(enum.StrEnum):
    ADMIN = "admin"
    OPERATOR = "operator"


class UserStatus(enum.StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class CentralUser(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(254), unique=True)  # i normalizuar: trim + lowercase
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(
        String(16), default=UserStatus.ACTIVE.value, server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint("role in ('admin', 'operator')", name="role"),
        CheckConstraint("status in ('active', 'disabled')", name="status"),
        CheckConstraint("email = lower(trim(email))", name="email_normalized"),
    )
