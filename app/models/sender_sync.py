"""M10-S2: projeksioni LOKAL i autorizimit të sender-ave nga Central (`cp.sender.v1`) — tabela të ndara nga `SenderId` (objekti lokal/klientit). Asnjë `owner_ref` këtu: identiteti është `enterprise_id`
(UUID i Central). Kjo NUK është burim autoriteti ende (S4): shërben vetëm si read model i sinkronizuar. `projection_state='withdrawn'` = dalë nga fusha e autorizuar / zëvendësuar nga snapshot-i
(gjendje, jo fshirje); `approved_key` mbahet VETËM për rreshta `active ∧ approved`."""

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
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.timeutil import utcnow
from app.models.rates import PK

PROJECTION_STATES = ("active", "withdrawn")


class SyncedSenderPolicy(Base):
    __tablename__ = "sms_synced_sender_policies"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    country: Mapped[str] = mapped_column(String(2))
    sender_kind: Mapped[str] = mapped_column(String(12))
    allowed: Mapped[bool] = mapped_column(Boolean)
    requires_approval: Mapped[bool] = mapped_column(Boolean)
    policy_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    policy_revision: Mapped[int] = mapped_column(BigInteger)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cp_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    projection_state: Mapped[str] = mapped_column(
        String(10), default="active", server_default="active"
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("country", "sender_kind", name="uq_sms_synced_sender_policies_scope"),
        CheckConstraint("sender_kind in ('alphanumeric', 'numeric')", name="kind"),
        CheckConstraint("projection_state in ('active', 'withdrawn')", name="state"),
        CheckConstraint("policy_revision >= 1", name="revision_positive"),
        CheckConstraint("allowed OR requires_approval", name="denied_implies_review"),
    )


class SyncedSenderAuthorization(Base):
    __tablename__ = "sms_synced_sender_authorizations"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    registry_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, unique=True
    )  # id e rreshtit të regjistrit te Central
    enterprise_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    external_ref: Mapped[str] = mapped_column(String(64))
    country: Mapped[str] = mapped_column(String(2))
    sender_kind: Mapped[str] = mapped_column(String(12))
    display_value: Mapped[str] = mapped_column(String(16))
    norm_value: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(10))
    approved_key: Mapped[str | None] = mapped_column(String(32), unique=True)
    decision_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    decision: Mapped[str] = mapped_column(String(12))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    policy_source: Mapped[str] = mapped_column(String(8))
    policy_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    policy_revision: Mapped[int | None] = mapped_column(BigInteger)
    cp_revision: Mapped[int] = mapped_column(BigInteger)
    cp_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    projection_state: Mapped[str] = mapped_column(
        String(10), default="active", server_default="active"
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_sms_synced_sender_auth_lookup", "enterprise_id", "country", "norm_value"),
        CheckConstraint("sender_kind in ('alphanumeric', 'numeric')", name="kind"),
        CheckConstraint("status in ('pending', 'approved', 'rejected', 'revoked')", name="status"),
        CheckConstraint("projection_state in ('active', 'withdrawn')", name="state"),
        CheckConstraint("policy_source in ('explicit', 'default')", name="policy_source"),
        CheckConstraint("cp_revision >= 1", name="revision_positive"),
        CheckConstraint(
            "approved_key IS NULL OR (status = 'approved' AND projection_state = 'active')",
            name="approved_key_consistency",
        ),
    )


class SenderSyncCursor(Base):
    """Kursori i feed-it `cp.sender.v1` (singleton), i NDARË nga cp.v1/money/pricing/billing."""

    __tablename__ = "sms_sender_sync_cursor"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    epoch: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # NULL = ende pa snapshot
    authorization_generation: Mapped[int | None] = mapped_column(BigInteger)
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    latest_central_seq: Mapped[int | None] = mapped_column(
        BigInteger
    )  # i fundit i raportuar nga Central (për lag)
    snapshot_seq: Mapped[int | None] = mapped_column(BigInteger)
    last_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_count: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    gap_recoveries: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    drift_repairs: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")

    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)
