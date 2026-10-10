"""Sender IDs dhe templates me rrjedhë miratimi (pending → approved | rejected)."""

import enum
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
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
from app.core.timeutil import utcnow
from app.models.rates import PK
from app.models.tenant import TenantOwned


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
    # M10-S0: çelësi kanonik i krahasimit (alfanumerik → lowercase; numerik → vetëm shifra). `value` mbetet si u shtyp (display).
    norm_value: Mapped[str] = mapped_column(String(16), default="", server_default="")
    # M10-S0: tregues (pa FK) te vendimi më i fundit në `sms_sender_decisions`; ngarkohet me të njëjtin rresht të autorizimit.
    current_decision_id: Mapped[int | None] = mapped_column(PK)
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("owner_ref", "country", "value"),
        Index("ix_sms_sender_ids_lookup", "owner_ref", "country", "norm_value"),
    )


SENDER_DECISIONS = ("requested", "approved", "rejected", "revoked", "resubmitted")
DECISION_SOURCES = ("local", "backfill")


class SenderDecisionImmutableError(RuntimeError):
    pass


class SenderDecision(Base):
    """M10-S0: historia VETËM-SHTIM e vendimeve mbi një sender (kërkesë/miratim/refuzim/revokim/ridërgim). `SenderId.status` është projeksioni
    i gjendjes aktuale; ky regjistër shpjegon "pse/kur/kush" dhe nuk mbishkruhet kurrë. `policy_revision` NULL = vendim lokal para Central."""

    __tablename__ = "sms_sender_decisions"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    sender_id: Mapped[int] = mapped_column(ForeignKey("sms_sender_ids.id"), index=True)
    decision: Mapped[str] = mapped_column(String(16))
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_by: Mapped[str | None] = mapped_column(
        String(64)
    )  # NULL vetëm për source='backfill' (aktori historik s'dihet; s'shpikim aktor)
    reason: Mapped[str | None] = mapped_column(String(255))
    policy_revision: Mapped[int | None] = mapped_column(Integer)
    evidence_ref: Mapped[str | None] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(12), default="local", server_default="local")

    __table_args__ = (
        CheckConstraint("decision in ('" + "', '".join(SENDER_DECISIONS) + "')", name="decision"),
        CheckConstraint("source in ('" + "', '".join(DECISION_SOURCES) + "')", name="source"),
        CheckConstraint("source = 'backfill' OR (decided_by IS NOT NULL AND length(trim(decided_by)) > 0)", name="actor"),
        CheckConstraint(
            "source = 'backfill' OR decision NOT IN ('rejected', 'revoked') OR (reason IS NOT NULL AND length(trim(reason)) > 0)",
            name="reason_required",
        ),
        Index("ix_sms_sender_decisions_sender_id_id", "sender_id", "id"),
    )  # fmt: skip


@event.listens_for(SenderDecision, "before_update")
@event.listens_for(SenderDecision, "before_delete")
def _decision_append_only(*_) -> None:
    raise SenderDecisionImmutableError("sender decision history is append-only")


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
