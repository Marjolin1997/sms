"""Kërkesa regjistrimi (M8-a): kontakt + emër i enterprise-it të kërkuar + produktet e zgjedhura.

Tri koncepte të ndara (kolona të ndara, jo një status i vetëm):
  * `status`              — kërkesa/vendimi: submitted → approved | rejected
  * `provisioning_status` — rezultati i provisioning-ut: NULL → pending → provisioned | failed
                            (M8-a vendos vetëm `pending`; `provisioned/failed` = kontrata për M8-c)
  * `decision_mode`       — manual | automatic (M8-a krijon vetëm manual)

Nuk përmban asnjë përdorues Central (stafi ≠ klienti). `access_token_hash` = SHA-256 e një sekreti
të rastësishëm me entropi të lartë (kurrë plaintext). `submission_key` (idempotencë e klientit) dhe
tokeni janë koncepte të ndara. Pa fshirje fizike: vetëm kalime gjendjesh.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from apps.central.core.db import Base
from apps.central.core.timeutil import utcnow
from apps.central.models.product import ImmutableError

SUBMITTED, APPROVED, REJECTED = "submitted", "approved", "rejected"
MANUAL, AUTOMATIC = "manual", "automatic"
PENDING, PROVISIONED, FAILED = "pending", "provisioned", "failed"


class RegistrationRequest(Base):
    __tablename__ = "registration_requests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    contact_email: Mapped[str] = mapped_column(String(254))  # i normalizuar: trim + lowercase
    contact_name: Mapped[str | None] = mapped_column(String(120))
    enterprise_name: Mapped[str] = mapped_column(String(200))  # i normalizuar si `Enterprise.name`
    submission_key: Mapped[str | None] = mapped_column(String(64))  # idempotenca e klientit
    request_hash: Mapped[str] = mapped_column(
        String(64)
    )  # sha256 i përmbajtjes (zbulon ripërdorim)
    access_token_hash: Mapped[str] = mapped_column(String(64))  # sha256 hex; kurrë plaintext
    status: Mapped[str] = mapped_column(String(16), default=SUBMITTED, server_default=SUBMITTED)
    decision_mode: Mapped[str | None] = mapped_column(String(16))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT")
    )
    decided_by_label: Mapped[str | None] = mapped_column(String(64))  # aktor sistemi (M8-b+)
    decision_reason: Mapped[str | None] = mapped_column(String(500))
    provisioning_status: Mapped[str | None] = mapped_column(String(16))
    provisioning_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    provisioning_error_code: Mapped[str | None] = mapped_column(String(48))
    enterprise_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("enterprises.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        # Idempotenca e skopuar: (kontakt, çelës) unik; NULL-et nuk përplasen
        UniqueConstraint("contact_email", "submission_key"),
        Index("ix_registration_requests_status_created", "status", "created_at"),
        CheckConstraint("contact_email = lower(trim(contact_email))", name="email_normalized"),
        CheckConstraint("length(trim(enterprise_name)) > 0", name="name_not_blank"),
        CheckConstraint("length(access_token_hash) = 64", name="token_hash_len"),
        CheckConstraint("status in ('submitted', 'approved', 'rejected')", name="status"),
        CheckConstraint(
            "decision_mode IS NULL OR decision_mode in ('manual', 'automatic')",
            name="decision_mode",
        ),
        CheckConstraint(
            "provisioning_status IS NULL OR "
            "provisioning_status in ('pending', 'provisioned', 'failed')",
            name="provisioning_status",
        ),
        # Vendimi: bosh kur `submitted`; përndryshe kohë + saktësisht NJË aktor (njeri XOR sistem)
        CheckConstraint(
            "(status = 'submitted' AND decided_at IS NULL AND decided_by_id IS NULL "
            "AND decided_by_label IS NULL AND decision_mode IS NULL) OR "
            "(status <> 'submitted' AND decided_at IS NOT NULL AND decision_mode IS NOT NULL AND "
            "((decided_by_id IS NOT NULL AND decided_by_label IS NULL) OR "
            "(decided_by_id IS NULL AND decided_by_label IS NOT NULL)))",
            name="decision_consistency",
        ),
        CheckConstraint(
            "status <> 'rejected' OR "
            "(decision_reason IS NOT NULL AND length(trim(decision_reason)) > 0)",
            name="reject_needs_reason",
        ),
        # Provisioning: NULL kur submitted; i detyrueshëm kur approved; rejected: vetëm failed/NULL
        CheckConstraint(
            "(status = 'submitted' AND provisioning_status IS NULL) OR "
            "(status = 'approved' AND provisioning_status IS NOT NULL) OR "
            "(status = 'rejected' AND "
            "(provisioning_status IS NULL OR provisioning_status = 'failed'))",
            name="provisioning_consistency",
        ),
        CheckConstraint(
            "provisioning_status IS NULL OR provisioning_status <> 'provisioned' "
            "OR enterprise_id IS NOT NULL",
            name="provisioned_has_enterprise",
        ),
    )


class RegistrationProduct(Base):
    __tablename__ = "registration_products"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("registration_requests.id", ondelete="RESTRICT")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("products.id", ondelete="RESTRICT")
    )
    # I plotësuar nga provisioning-u (M8-c); NULL deri atëherë
    assignment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("enterprise_products.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("request_id", "product_id"),)


@event.listens_for(RegistrationRequest, "before_delete")
@event.listens_for(RegistrationProduct, "before_delete")
def _no_hard_delete(*_) -> None:
    raise ImmutableError("registration rows are never deleted; use state transitions")


@event.listens_for(RegistrationProduct, "before_update")
def _immutable_selection(_mapper, _conn, target: RegistrationProduct) -> None:
    state = inspect(target)
    for attr in ("request_id", "product_id"):
        if state.attrs[attr].history.has_changes():
            raise ImmutableError(f"registration_product.{attr} is immutable")
