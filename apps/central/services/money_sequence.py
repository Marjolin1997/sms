"""Numërues transaksional i parave (M9-b), i njëjti parim si `sync_sequence` (M7): rresht singleton i
kyçur `FOR UPDATE` deri në commit ⇒ `seq` N është i dukshëm para N+1 (kursor i sigurt për feed-in e
M9-c), dhe rollback heq edhe rritjen (asnjë `seq` fantazmë). Rendi i kyçjeve për çdo mutacion parash:
(1) `money_sequence`, (2) rreshti i llogarisë, (3) pagesa/granti. Serializon të gjitha mutacionet e
parave (volum i ulët administrativ): pranohet; garat e overspend-it shuhen nga ky kyç."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.models.money import MoneySequence

_SEQ = MoneySequence.__table__


def lock(db: Session) -> uuid.UUID:
    """Kyç numëruesin deri në fund të tx; kthen `epoch`."""
    row = db.execute(select(_SEQ.c.epoch).where(_SEQ.c.id == 1).with_for_update()).first()
    if row is None:
        raise RuntimeError("money_sequence singleton row is missing (run migrations)")
    return row[0]


def next_seq(db: Session) -> int:
    """Alokon `seq` të radhës (thirret vetëm pasi `lock` u mor në këtë tx)."""
    stmt = (
        _SEQ.update()
        .where(_SEQ.c.id == 1)
        .values(last_seq=_SEQ.c.last_seq + 1)
        .returning(_SEQ.c.last_seq)
    )
    return int(db.execute(stmt).scalar_one())


def current(db: Session) -> tuple[uuid.UUID, int]:
    """Lexim pa kyçje: (epoch, last_seq) i commit-uar."""
    row = db.execute(select(_SEQ.c.epoch, _SEQ.c.last_seq).where(_SEQ.c.id == 1)).one()
    return row[0], int(row[1])
