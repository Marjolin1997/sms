"""M10-S4: dëshmia e autoritetit të sender-ave (Enterprise).

`SenderAuthorityComparison`: krahasimet SHADOW (autoriteti lokal vs projeksioni Central) — APPEND-ONLY, pa vlera sender (vetëm referenca/ID dhe një hash i shkurtër i çelësit kanonik për deduplikim).
Mospërputhjet ruhen GJITHMONË; përputhjet mostrohen (deterministikisht sipas `ref`). Kategoritë janë taksonomi e kufizuar dhe e qëndrueshme.
`SenderBootstrapState`: singleton — prova e qëndrueshme që bootstrap-i i sender-ave ekzistues u rakordua (versioni, kohët, numrat, hash i raportit)."""

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
    String,
    Uuid,
    event,
    text,
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
# Ashpërsia (M10-S5): CRITICAL = lokali lejon dhe Central mohon (ose identitet/politikë e papërputhshme); INFO = e pritshme gjatë migrimit / vetëm raportim.
CRITICAL = ("local_allow_central_deny", "central_missing", "central_pending", "central_rejected",
            "central_revoked", "policy_mismatch", "sender_identity_mismatch")  # fmt: skip
WARNING = ("local_deny_central_allow",)
INFO = ("match_allowed", "match_denied", "projection_stale")
MATCHES = ("match_allowed", "match_denied")
SEVERITY = {
    **{c: "critical" for c in CRITICAL},
    **{c: "warning" for c in WARNING},
    **{c: "info" for c in INFO},
}
ISSUE_CATEGORIES = (
    "identity_conflict", "global_key_conflict", "policy_denied", "invalid_legacy_identity", "missing_enterprise_mapping",
    "local_approved_central_pending", "local_approved_central_rejected", "local_approved_central_revoked", "missing_in_central",
)  # fmt: skip
RESOLUTIONS = ("accepted_not_migrated", "sender_deactivated", "corrected")
EVIDENCE_KINDS = ("pre_cutover", "post_cutover", "rollback_ack")


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


class SenderBootstrapIssue(Base):
    """M10-S5: çështje bootstrap bllokuese (rakordim lokal-vs-Central). Historia s'fshihet kurrë; zgjidhja vendoset NJË herë (NULL → vlerë), pa prekur Central (zgjidhja = "e kuptuar dhe e pranuar", jo "politika anashkalohet")."""

    __tablename__ = "sms_sender_bootstrap_issues"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    enterprise_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    sender_id: Mapped[int] = mapped_column(ForeignKey("sms_sender_ids.id"))
    category: Mapped[str] = mapped_column(String(40))
    identity_hash: Mapped[str] = mapped_column(String(16))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(String(24))
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(255))
    evidence_ref: Mapped[str | None] = mapped_column(String(128))
    report_hash: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_sms_sender_bootstrap_issues_sender", "sender_id", "category"),
        Index(
            "uq_sms_sender_bootstrap_issues_open", "sender_id", "category", unique=True,
            sqlite_where=text("resolved_at IS NULL"), postgresql_where=text("resolved_at IS NULL"),
        ),  # fmt: skip
        CheckConstraint("category in ('" + "', '".join(ISSUE_CATEGORIES) + "')", name="category"),
        CheckConstraint("resolution IS NULL OR resolution in ('" + "', '".join(RESOLUTIONS) + "')", name="resolution"),
        CheckConstraint("(resolved_at IS NULL) = (resolution IS NULL)", name="resolved_consistency"),
        CheckConstraint("resolution IS NULL OR (resolved_by IS NOT NULL AND length(trim(resolved_by)) > 0)", name="resolved_by"),
    )  # fmt: skip


class SenderCutoverEvidence(Base):
    """M10-S5: prova e pandryshueshme e cutover-it/rollback-ut (machine-readable). `evidence_hash` UNIQUE = idempotencë; ACK-u i prodhimit lidhet me këtë hash."""

    __tablename__ = "sms_sender_cutover_evidence"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))
    evidence_hash: Mapped[str] = mapped_column(String(64), unique=True)
    authority_version: Mapped[int] = mapped_column(Integer)
    environment: Mapped[str] = mapped_column(String(16))
    code_revision: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(64))
    bootstrap_report_hash: Mapped[str | None] = mapped_column(String(64))
    readiness_status: Mapped[str] = mapped_column(String(8))
    readiness_hash: Mapped[str] = mapped_column(String(64))
    canary_ref: Mapped[str | None] = mapped_column(String(64))
    ref_hash: Mapped[str | None] = mapped_column(
        String(64)
    )  # post_cutover: hash-i i provës pre_cutover që e ka lejuar
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_sms_sender_cutover_evidence_kind", "kind", "created_at"),
        CheckConstraint("kind in ('" + "', '".join(EVIDENCE_KINDS) + "')", name="kind"),
        CheckConstraint("readiness_status in ('PASS', 'WARN', 'FAIL')", name="readiness_status"),
    )


@event.listens_for(SenderCutoverEvidence, "before_update")
@event.listens_for(SenderCutoverEvidence, "before_delete")
@event.listens_for(SenderBootstrapIssue, "before_delete")
def _evidence_append_only(*_) -> None:
    raise SenderAuthorityImmutableError(
        "cutover evidence and bootstrap issues are never rewritten or deleted"
    )


@event.listens_for(SenderBootstrapIssue, "before_update")
def _issue_frozen(_m, _c, target) -> None:
    from sqlalchemy import inspect as sa_inspect

    attrs = sa_inspect(target).attrs
    frozen = ("enterprise_id", "sender_id", "category", "identity_hash", "detected_at")
    if any(getattr(attrs, f).history.has_changes() for f in frozen):
        raise SenderAuthorityImmutableError("bootstrap issue identity is immutable")
    old = attrs.resolution.history.deleted
    if old and old[0] is not None:
        raise SenderAuthorityImmutableError("a bootstrap issue resolution is final")
