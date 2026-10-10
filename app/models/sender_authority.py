"""M10-S4: dëshmia e autoritetit të sender-ave (Enterprise).

`SenderAuthorityComparison`: krahasimet SHADOW (autoriteti lokal vs projeksioni Central) — APPEND-ONLY, pa vlera sender (vetëm referenca/ID dhe një hash i shkurtër i çelësit kanonik për deduplikim).
Mospërputhjet ruhen GJITHMONË; përputhjet mostrohen (deterministikisht sipas `ref`). Kategoritë janë taksonomi e kufizuar dhe e qëndrueshme.
`SenderBootstrapState`: singleton — prova e qëndrueshme që bootstrap-i i sender-ave ekzistues u rakordua (versioni, kohët, numrat, hash i raportit)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Uuid,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.timeutil import utcnow
from app.models.rates import PK

CATEGORIES = (
    "match_allowed", "match_denied", "local_allow_central_deny", "local_deny_central_allow",
    "central_missing", "central_pending", "central_rejected", "central_revoked",
    "policy_mismatch", "sender_identity_mismatch", "projection_stale",
)  # fmt: skip
CRITICAL = ("local_allow_central_deny", "local_deny_central_allow", "central_missing",
            "central_pending", "central_rejected", "central_revoked", "policy_mismatch",
            "sender_identity_mismatch")  # fmt: skip
MATCHES = ("match_allowed", "match_denied")


class SenderAuthorityImmutableError(RuntimeError):
    pass


class SenderAuthorityComparison(Base):
    __tablename__ = "sms_sender_authority_comparisons"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    ref: Mapped[str] = mapped_column(String(64))  # public_id i mesazhit
    enterprise_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    country: Mapped[str] = mapped_column(String(2))
    sender_kind: Mapped[str | None] = mapped_column(String(12))
    category: Mapped[str] = mapped_column(String(32))
    local_allowed: Mapped[bool] = mapped_column(Boolean)
    central_allowed: Mapped[bool] = mapped_column(Boolean)
    central_reason: Mapped[str] = mapped_column(String(24))  # kategoria e evaluatorit Central
    local_sender_ref: Mapped[int | None] = mapped_column(PK)
    central_registry_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    central_policy_revision: Mapped[int | None] = mapped_column(BigInteger)
    central_cp_revision: Mapped[int | None] = mapped_column(BigInteger)
    identity_hash: Mapped[str] = mapped_column(
        String(16)
    )  # sha256(çelësi kanonik)[:16]: grupim pa vlerë sender
    projection_stale: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_sms_sender_authority_cmp_cat", "category", "created_at"),
        CheckConstraint("category in ('" + "', '".join(CATEGORIES) + "')", name="category"),
    )


class SenderBootstrapState(Base):
    __tablename__ = "sms_sender_bootstrap_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    bootstrap_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_revision: Mapped[str | None] = mapped_column(String(64))
    tenant_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    sender_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    unresolved_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    report_hash: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)


@event.listens_for(SenderAuthorityComparison, "before_update")
@event.listens_for(SenderAuthorityComparison, "before_delete")
def _append_only(*_) -> None:
    raise SenderAuthorityImmutableError("sender authority comparisons are append-only")
