"""M10-S1: autoriteti Central i sender-ave — politika e shtetit (e versionuar), regjistri global i sender-ave dhe historia e vendimeve.

- `CountrySenderPolicy`: revizione APPEND-ONLY për (shtet, lloj sender-i). Çdo revizion hyn në fuqi në çastin e krijimit (`effective_from = created_at`) dhe vlen
  deri te `effective_from` i revizionit të radhës (diapazoni është i nënkuptuar ⇒ asnjë mbivendosje e mundshme, asnjë UPDATE). Pa rresht ⇒ parazgjedhja
  `allowed=true, requires_approval=true` (sjellja e sotme e Enterprise); parazgjedhja nuk materializohet kurrë si rresht.
- `SenderRegistry`: identiteti kanonik = `enterprise_id` (UUID) + `external_ref` (i qëndrueshëm për transportin e ardhshëm). Gjendja aktuale është projeksion; identiteti nuk ndryshohet.
- `SenderDecision`: historia autoritative VETËM-SHTIM (kërkesë/miratim/refuzim/revokim/ridërgim) me politikën e saktë të përdorur."""

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
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from apps.central.core.db import Base
from apps.central.core.timeutil import utcnow
from apps.central.models.billing import BillingImmutableError

STATUSES = ("pending", "approved", "rejected", "revoked")
DECISIONS = ("requested", "approved", "rejected", "revoked", "resubmitted")
CATEGORIES = (
    "request",
    "manual",
    "resubmit",
    "policy_denied",
    "policy_auto_approved",
    "policy_revoked",
)
SOURCES = ("admin", "enterprise", "import")
KINDS_SQL = "('alphanumeric', 'numeric')"


class SenderImmutableError(BillingImmutableError):
    pass


class CountrySenderPolicy(Base):
    __tablename__ = "country_sender_policies"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    country: Mapped[str] = mapped_column(String(2))
    sender_kind: Mapped[str] = mapped_column(String(12))
    revision: Mapped[int] = mapped_column(Integer)
    allowed: Mapped[bool] = mapped_column(Boolean)
    requires_approval: Mapped[bool] = mapped_column(Boolean)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str] = mapped_column(String(500))
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "country", "sender_kind", "revision", name="uq_country_sender_policies_revision"
        ),
        UniqueConstraint(
            "country", "sender_kind", "effective_from", name="uq_country_sender_policies_effective"
        ),
        Index("ix_country_sender_policies_lookup", "country", "sender_kind", "effective_from"),
        CheckConstraint("length(country) = 2 AND country = upper(country)", name="country"),
        CheckConstraint(f"sender_kind in {KINDS_SQL}", name="kind"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint("allowed OR requires_approval", name="denied_implies_review"),
    )


class SenderRegistry(Base):
    __tablename__ = "sender_registry"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("enterprises.id", ondelete="RESTRICT")
    )
    external_ref: Mapped[str] = mapped_column(String(64))
    country: Mapped[str] = mapped_column(String(2))
    sender_kind: Mapped[str] = mapped_column(String(12))
    display_value: Mapped[str] = mapped_column(String(16))
    norm_value: Mapped[str] = mapped_column(String(16))
    request_hash: Mapped[str] = mapped_column(String(64))
    approved_key: Mapped[str | None] = mapped_column(String(32), unique=True)
    current_status: Mapped[str] = mapped_column(String(12), default="pending")
    current_decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    source: Mapped[str] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("enterprise_id", "external_ref", name="uq_sender_registry_external_ref"),
        UniqueConstraint(
            "enterprise_id", "country", "norm_value", name="uq_sender_registry_identity"
        ),
        Index("ix_sender_registry_status", "current_status", "created_at"),
        Index("ix_sender_registry_enterprise", "enterprise_id", "country"),
        CheckConstraint("length(country) = 2 AND country = upper(country)", name="country"),
        CheckConstraint(f"sender_kind in {KINDS_SQL}", name="kind"),
        CheckConstraint("current_status in " + str(STATUSES), name="status"),
        CheckConstraint("source in " + str(SOURCES), name="source"),
        CheckConstraint(
            "(current_status = 'approved') = (approved_key IS NOT NULL)",
            name="approved_key_consistency",
        ),
        CheckConstraint(
            "length(norm_value) >= 3 AND length(display_value) >= 3", name="value_length"
        ),
    )


class SenderDecision(Base):
    __tablename__ = "sender_decisions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    registry_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sender_registry.id", ondelete="RESTRICT")
    )
    seq: Mapped[int] = mapped_column(
        Integer
    )  # 1..n për regjistër: renditje deterministike pa varësi nga ora
    decision: Mapped[str] = mapped_column(String(12))
    from_status: Mapped[str | None] = mapped_column(String(12))
    to_status: Mapped[str] = mapped_column(String(12))
    category: Mapped[str] = mapped_column(String(24))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    actor_label: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(500))
    evidence_ref: Mapped[str | None] = mapped_column(String(128))
    policy_source: Mapped[str] = mapped_column(
        String(8)
    )  # explicit | default (parazgjedhja shënohet shprehimisht)
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("country_sender_policies.id", ondelete="RESTRICT")
    )
    policy_revision: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("registry_id", "seq", name="uq_sender_decisions_seq"),
        Index("ix_sender_decisions_registry", "registry_id", "decided_at"),
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint("decision in " + str(DECISIONS), name="decision"),
        CheckConstraint("category in " + str(CATEGORIES), name="category"),
        CheckConstraint("source in " + str(SOURCES), name="source"),
        CheckConstraint("policy_source in ('explicit', 'default')", name="policy_source"),
        CheckConstraint("(decided_by_id IS NOT NULL) <> (actor_label IS NOT NULL)", name="actor"),
        CheckConstraint(
            "(policy_source = 'explicit') = (policy_id IS NOT NULL AND policy_revision IS NOT NULL)",
            name="policy_provenance",
        ),
        CheckConstraint(
            "decision NOT IN ('rejected', 'revoked') OR (reason IS NOT NULL AND length(trim(reason)) > 0)",
            name="reason_required",
        ),
    )


@event.listens_for(CountrySenderPolicy, "before_update")
@event.listens_for(CountrySenderPolicy, "before_delete")
@event.listens_for(SenderDecision, "before_update")
@event.listens_for(SenderDecision, "before_delete")
def _append_only(*_) -> None:
    raise SenderImmutableError("sender policy revisions and decision history are append-only")


@event.listens_for(SenderRegistry, "before_delete")
def _registry_never_deleted(*_) -> None:
    raise SenderImmutableError("sender registry rows are never deleted")


class SenderSyncSequence(Base):
    """M10-S2: numërues global transaksional i feed-it `cp.sender.v1` (rresht singleton i kyçur `FOR UPDATE` deri në commit ⇒ seq N i dukshëm para N+1; rollback heq edhe rritjen)."""

    __tablename__ = "sender_sync_sequence"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    epoch: Mapped[uuid.UUID] = mapped_column(Uuid, default=uuid.uuid4)
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    floor_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")

    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("last_seq >= 0", name="last_seq_non_negative"),
        CheckConstraint("floor_seq >= 0 and floor_seq <= last_seq", name="floor_within_range"),
    )


class SenderSyncOutbox(Base):
    """Ngjarje të ngrira (gjendje e plotë) të krijuara në të njëjtin transaksion me mutacionin autoritativ. `enterprise_id` NULL = politikë globale."""

    __tablename__ = "sender_sync_outbox"

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True, default=uuid.uuid4)
    enterprise_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("enterprises.id", ondelete="RESTRICT")
    )
    event_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(BigInteger)
    group_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "event_type", "entity_id", "revision", name="uq_sender_sync_outbox_entity_revision"
        ),
        Index("ix_sender_sync_outbox_enterprise_seq", "enterprise_id", "seq"),
        Index("ix_sender_sync_outbox_group", "group_id", "seq"),
        CheckConstraint("seq >= 1 and revision >= 1", name="positive"),
        CheckConstraint(
            "event_type in ('sender.policy.upserted', 'sender.registry.upserted')",
            name="event_type",
        ),
        CheckConstraint(
            "(event_type = 'sender.policy.upserted') = (enterprise_id IS NULL)",
            name="policy_is_global",
        ),
    )


@event.listens_for(SenderSyncOutbox, "before_update")
@event.listens_for(SenderSyncOutbox, "before_delete")
def _outbox_append_only(*_) -> None:
    raise SenderImmutableError("sender_sync_outbox rows are append-only")


class SenderRequestOperation(Base):
    """M10-S3: veprimet logjike të pranuara nga Enterprise (`sender.request.v1`). `operation_id` UNIQUE = dedupe i transportit at-least-once: i njëjti veprim
    nuk prodhon kurrë efekt të dytë. Shkruhet në fund të veprimit, në të njëjtin transaksion me regjistrin/vendimin; APPEND-ONLY."""

    __tablename__ = "sender_request_operations"

    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("enterprises.id", ondelete="RESTRICT")
    )
    registry_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sender_registry.id", ondelete="RESTRICT")
    )
    external_ref: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(8))
    request_hash: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(24))
    auto: Mapped[str] = mapped_column(String(16))
    status_after: Mapped[str] = mapped_column(String(12))
    decision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_sender_request_operations_registry", "registry_id", "received_at"),
        CheckConstraint("operation in ('request', 'resubmit')", name="operation"),
        CheckConstraint(
            "outcome in ('created', 'existing', 'resubmitted', 'noop_pending', 'noop_approved')",
            name="outcome",
        ),
        CheckConstraint("auto in ('not_applicable', 'approved', 'denied', 'blocked')", name="auto"),
    )


@event.listens_for(SenderRequestOperation, "before_update")
@event.listens_for(SenderRequestOperation, "before_delete")
def _operation_append_only(*_) -> None:
    raise SenderImmutableError("sender_request_operations rows are append-only")
