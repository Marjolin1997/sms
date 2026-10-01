"""Identiteti kanonik i një klienti (Enterprise). M1a: vetëm regjistri; asnjë tabelë tjetër nuk e
referon ende. `owner_ref` mbetet për pajtueshmëri dhe është ruajtur SAKTËSISHT siç është (pa
normalizim); `id` (UUID) krijohet një herë dhe është identiteti i ri për fazat e ardhshme."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.timeutil import utcnow


class Enterprise(Base):
    __tablename__ = "sms_enterprises"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_ref: Mapped[str] = mapped_column(String(64), unique=True)
    external_id: Mapped[str | None] = mapped_column(String(64))
    legal_name: Mapped[str | None] = mapped_column(String(200))
    short_name: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("uq_sms_enterprises_owner_ref_lower", func.lower(owner_ref), unique=True),
        Index(
            "uq_sms_enterprises_external_id",
            external_id,
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
            sqlite_where=text("external_id IS NOT NULL"),
        ),
    )
