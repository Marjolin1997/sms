"""Sender IDs dhe templates me rrjedhë miratimi (pending → approved | rejected)."""

import enum
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.rates import PK
from app.models.tenant import TenantOwned
from app.models.wallet import utcnow


class ApprovalStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REVOKED = "revoked"


class SenderKind(enum.StrEnum):
    ALPHANUMERIC = "alphanumeric"
    NUMERIC = "numeric"


def _status():
    return Enum(ApprovalStatus, native_enum=False, length=16)


class SenderId(TenantOwned, Base):
    __tablename__ = "sms_sender_ids"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_ref: Mapped[str] = mapped_column(String(64), index=True)
    country: Mapped[str] = mapped_column(String(2))  # ISO 3166-1 alpha-2
    value: Mapped[str] = mapped_column(String(16))
    kind: Mapped[SenderKind] = mapped_column(Enum(SenderKind, native_enum=False, length=16))
    status: Mapped[ApprovalStatus] = mapped_column(_status(), default=ApprovalStatus.PENDING)
    # I plotësuar vetëm kur është approved; UNIQUE => dy klientë nuk mund ta kenë
    # njëkohësisht të miratuar të njëjtin sender në të njëjtin shtet.
    approved_key: Mapped[str | None] = mapped_column(String(32), unique=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("owner_ref", "country", "value"),)


class Template(TenantOwned, Base):
    __tablename__ = "sms_templates"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_ref: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("owner_ref", "name"),)


class TemplateVersion(Base):
    """Trupi i një versioni nuk ndryshon kurrë; redaktimi krijon version të ri."""

    __tablename__ = "sms_template_versions"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("sms_templates.id"))
    version: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[ApprovalStatus] = mapped_column(_status(), default=ApprovalStatus.PENDING)
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("template_id", "version"),
        Index("ix_sms_tv_template_status", "template_id", "status"),
    )


class TemplateBodyFrozenError(RuntimeError):
    pass


@event.listens_for(TemplateVersion, "before_update")
def _body_frozen(_mapper, _conn, target):
    if inspect(target).attrs.body.history.has_changes():
        raise TemplateBodyFrozenError("template body is immutable; create a new version")
