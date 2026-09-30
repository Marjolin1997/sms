"""Përdorues me email + fjalëkalim, sesione dhe tokena ftese/rivendosje."""

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.rates import PK
from app.models.wallet import utcnow


class UserStatus(enum.StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class User(Base):
    __tablename__ = "sms_users"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)  # shkronja të vogla
    password_hash: Mapped[str | None] = mapped_column(String(200))  # NULL = ftesa s'është pranuar
    role: Mapped[str] = mapped_column(String(16))
    owner_ref: Mapped[str | None] = mapped_column(String(64))  # e detyrueshme për role=client
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, native_enum=False, length=16), default=UserStatus.ACTIVE
    )
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserSession(Base):
    """Token sesioni `sess_<prefix>_<sekreti>`; ruhet vetëm SHA-256 i sekretit."""

    __tablename__ = "sms_user_sessions"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("sms_users.id"), index=True)
    prefix: Mapped[str] = mapped_column(String(12), unique=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserToken(Base):
    """Ftesë (vendos fjalëkalimin e parë) ose rivendosje; përdoret një herë dhe skadon."""

    __tablename__ = "sms_user_tokens"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("sms_users.id"))
    kind: Mapped[str] = mapped_column(String(8))  # invite | reset
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_sms_user_tokens_user", "user_id", "used_at"),)
