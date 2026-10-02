"""Infrastruktura e feed-it të sync-ut (Central është burim pull; s'dërgon vetë).

`sync_sequence`: numërues global singleton, i rritur TRANSAKSIONALISHT (rresht i kyçur deri në
commit), jo sekuencë/identity e PostgreSQL: sekuencat nuk garantojnë rendin e commit-it dhe një
kursor `after_seq=N` mund të humbte një `seq` më të vogël që commit-ohet më vonë.
`sync_outbox`: një rresht per ndryshim real, me snapshot të ngrirë të gjendjes në çastin e tij.
`revision` (per entitet) dhe `seq` (feed global) janë dy koncepte të ndara. Retention i synuar ≥ 30
ditë (pa worker pastrimi ende); konsumatori me kursor më të vjetër kërkon snapshot të plotë.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
)
from sqlalchemy.orm import Mapped, mapped_column

from apps.central.core.db import Base
from apps.central.core.timeutil import utcnow


class SyncOutboxImmutableError(RuntimeError):
    pass


class SyncSequence(Base):
    __tablename__ = "sync_sequence"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")

    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint("last_seq >= 0", name="last_seq_non_negative"),
    )


class SyncOutbox(Base):
    __tablename__ = "sync_outbox"

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True, default=uuid.uuid4)
    enterprise_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("enterprises.id", ondelete="RESTRICT")
    )
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict] = mapped_column(JSON)  # snapshot i ngrirë i gjendjes, jo vetëm id
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "entity_type", "entity_id", "revision", name="uq_sync_outbox_entity_revision"
        ),
        CheckConstraint("seq >= 1 and revision >= 1", name="positive"),
        Index("ix_sync_outbox_enterprise_id_seq", "enterprise_id", "seq"),
    )


class RevisionError(RuntimeError):
    pass


def guard_revision(model: type, tracked: tuple[str, ...]) -> None:
    """Disiplina e `revision` në ORM: fushat e ndjekura ndryshojnë VETËM bashkë me `revision += 1`;
    revision s'ulet dhe s'rritet pa ndryshim real. (Anashkalohet vetëm nga SQL i drejtpërdrejtë.)"""

    @event.listens_for(model, "before_update")
    def _guard(_mapper, _conn, target) -> None:
        state = inspect(target)
        rev = state.attrs.revision.history
        changed = any(state.attrs[a].history.has_changes() for a in tracked)
        if rev.has_changes():
            old = rev.deleted[0] if rev.deleted else None
            if old is None or rev.added[0] != old + 1 or not changed:
                raise RevisionError("revision must increase by exactly 1 with a real change")
        elif changed:
            raise RevisionError(f"{model.__name__} changed without a revision bump")


@event.listens_for(SyncOutbox, "before_update")
@event.listens_for(SyncOutbox, "before_delete")
def _append_only(*_) -> None:
    raise SyncOutboxImmutableError("sync_outbox rows are append-only")
