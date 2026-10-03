"""Contacts, lista dhe consent (opt-in/opt-out) me provë të pandryshueshme."""

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.timeutil import utcnow
from app.models.rates import PK
from app.models.tenant import TenantOwned


class ContactStatus(enum.StrEnum):
    ACTIVE = "active"
    ERASED = "erased"  # PII e fshirë (GDPR); rreshti mbetet vetëm si mbajtës referencash


class Contact(TenantOwned, Base):
    __tablename__ = "sms_contacts"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_ref: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(String(64))
    phone: Mapped[str | None] = mapped_column(String(16))  # E.164 pa "+"
    email: Mapped[str | None] = mapped_column(String(254))  # me shkronja të vogla
    first_name: Mapped[str | None] = mapped_column(String(64))
    last_name: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[ContactStatus] = mapped_column(
        Enum(ContactStatus, native_enum=False, length=16), default=ContactStatus.ACTIVE
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        # emra eksplicitë: konventa merr vetëm kolonën e parë dhe do t'i përplaste tre-shen
        UniqueConstraint("owner_ref", "phone", name="uq_sms_contacts_owner_phone"),
        UniqueConstraint("owner_ref", "email", name="uq_sms_contacts_owner_email"),
        UniqueConstraint("owner_ref", "external_id", name="uq_sms_contacts_owner_external"),
        CheckConstraint(
            "status = 'ERASED' OR phone IS NOT NULL OR email IS NOT NULL", name="has_address"
        ),
        Index("ix_sms_contacts_owner_id", "owner_ref", "id"),
    )


class ContactList(TenantOwned, Base):
    __tablename__ = "sms_contact_lists"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_ref: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("owner_ref", "name"),)


class ListMember(Base):
    __tablename__ = "sms_list_members"

    list_id: Mapped[int] = mapped_column(ForeignKey("sms_contact_lists.id"), primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("sms_contacts.id"), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_sms_list_members_contact", "contact_id"),)


class ConsentAction(enum.StrEnum):
    OPT_IN = "opt_in"
    OPT_OUT = "opt_out"


class ConsentEvent(TenantOwned, Base):
    """Prova ligjore: kush, kur, nga cili burim, me çfarë evidence. Vetëm-shtim.
    Adresa nuk ruhet kurrë në tekst të hapur, vetëm HMAC-i i saj."""

    __tablename__ = "sms_consent_events"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_ref: Mapped[str] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(8))
    address_hash: Mapped[str] = mapped_column(String(64))
    action: Mapped[ConsentAction] = mapped_column(Enum(ConsentAction, native_enum=False, length=8))
    reason: Mapped[str] = mapped_column(String(24))
    source: Mapped[str] = mapped_column(String(48))
    evidence: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_sms_consent_events_lookup", "owner_ref", "channel", "address_hash"),
    )


class ConsentState(TenantOwned, Base):
    """Gjendja aktuale (e nxjerrshme nga ConsentEvent), për kontroll të shpejtë para dërgimit."""

    __tablename__ = "sms_consent_state"

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    owner_ref: Mapped[str] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(8))
    address_hash: Mapped[str] = mapped_column(String(64))
    opted_in: Mapped[bool] = mapped_column(Boolean)
    # hard: bllokon çdo kategori (STOP, bounce, complaint, erasure); përndryshe vetëm marketing.
    hard: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(24))
    last_event_id: Mapped[int] = mapped_column(ForeignKey("sms_consent_events.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("owner_ref", "channel", "address_hash"),)


class ConsentImmutableError(RuntimeError):
    pass


@event.listens_for(ConsentEvent, "before_update")
@event.listens_for(ConsentEvent, "before_delete")
def _consent_events_append_only(*_):
    raise ConsentImmutableError("consent events are append-only")
