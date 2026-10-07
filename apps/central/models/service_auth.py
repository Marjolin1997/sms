"""Kredencialet e shërbimeve (Enterprise → Central): klient, çelësa publikë Ed25519, objektivat.

Central ruan VETËM çelësa publikë; çelësi privat mbetet te thirrësi. Një klient mund të ketë disa
çelësa (rotacion). `auth_generation` rritet sa herë ndryshon bashkësia e enterprise-ve të autorizuara:
konsumatori duhet snapshot të plotë kur ndryshon (parandalon boshllëkun historik te kursori `seq`).
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from apps.central.core.db import Base
from apps.central.core.timeutil import utcnow

ALLOWED_SCOPES = frozenset(
    {"sync:read", "money:read", "money:report", "pricing:read", "billing:report"}
)  # M9-c: money:read ≠ sync:read (ndarë)


class CredentialStatus(enum.StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class ServiceClient(Base):
    __tablename__ = "service_clients"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    client_id: Mapped[str] = mapped_column(
        String(64), unique=True
    )  # identiteti i qëndrueshëm (`iss`)
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    scopes: Mapped[list] = mapped_column(JSON)
    auth_generation: Mapped[int] = mapped_column(BigInteger, default=1, server_default="1")
    # M8-c: enterprise-et e SAPOKRIJUARA nga provisioning grantohen automatikisht (pa grant retroaktiv)
    auto_grant_new_enterprises: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint("status in ('active', 'disabled')", name="status"),
        CheckConstraint("auth_generation >= 1", name="generation_positive"),
    )


class ServiceKey(Base):
    __tablename__ = "service_keys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    client_pk: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_clients.id", ondelete="RESTRICT")
    )
    kid: Mapped[str] = mapped_column(String(64))
    public_key: Mapped[str] = mapped_column(Text)  # PEM SubjectPublicKeyInfo (Ed25519)
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("client_pk", "kid", name="uq_service_keys_client_pk_kid"),
        CheckConstraint("status in ('active', 'disabled')", name="status"),
    )


class ServiceClientEnterprise(Base):
    """Objektivi: cilat enterprise-e mund të lexojë një klient (jo supozim single-tenant)."""

    __tablename__ = "service_client_enterprises"

    client_pk: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_clients.id", ondelete="RESTRICT"), primary_key=True
    )
    enterprise_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("enterprises.id", ondelete="RESTRICT"), primary_key=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ServiceAssertionJti(Base):
    """Mbrojtje replay: `jti` i përdorur një herë per klient; pastrim i ngadaltë në shkrim."""

    __tablename__ = "service_assertion_jti"

    client_pk: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_clients.id", ondelete="RESTRICT"), primary_key=True
    )
    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_service_assertion_jti_expires_at", "expires_at"),)
