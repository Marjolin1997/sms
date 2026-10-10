"""API keys (RBAC), audit log dhe switches."""

import enum
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Index, String, Text, event
from sqlalchemy import false as sa_false
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.timeutil import utcnow
from app.models.rates import PK
from app.models.tenant import TenantOwned


class KeyStatus(enum.StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class ApiKey(TenantOwned, Base):
    """Sekreti nuk ruhet kurrë; vetëm SHA-256 (sekreti ka 256 bit entropi, prandaj
    hash i shpejtë mjafton). `prefix` është pjesa publike që gjen rreshtin."""

    __tablename__ = "sms_api_keys"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    prefix: Mapped[str] = mapped_column(String(12), unique=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))
    owner_ref: Mapped[str | None] = mapped_column(String(64))  # e detyrueshme për role=client
    status: Mapped[KeyStatus] = mapped_column(
        Enum(KeyStatus, native_enum=False, length=16), default=KeyStatus.ACTIVE
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    allowed_cidrs: Mapped[str | None] = mapped_column(Text)  # JSON: ["203.0.113.0/24", ...]
    totp_secret_enc: Mapped[str | None] = mapped_column(Text)  # Fernet; kurrë në tekst të hapur
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)  # kundër riluajtjes së kodit
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthFailure(Base):
    """Provë e dështuar autentikimi (kufizim sipas IP). Pa sekrete: vetëm IP dhe prefix-i publik."""

    __tablename__ = "sms_auth_failures"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    ip: Mapped[str] = mapped_column(String(45))
    prefix: Mapped[str | None] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_sms_auth_failures_ip", "ip", "created_at"),)


class AuditLog(Base):
    __tablename__ = "sms_audit_log"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(48))
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str | None] = mapped_column(Text)  # JSON pa sekrete
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_sms_audit_target", "target_type", "target_id"),
        Index("ix_sms_audit_actor", "actor"),
    )


class Switch(Base):
    __tablename__ = "sms_switches"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[str | None] = mapped_column(String(255))
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditImmutableError(RuntimeError):
    pass


@event.listens_for(AuditLog, "before_update")
@event.listens_for(AuditLog, "before_delete")
def _audit_append_only(*_):
    raise AuditImmutableError("audit log is append-only")
