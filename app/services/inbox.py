"""Inbox: ruajtja e SMS-ve hyrës, bisedat (thread) sipas numrit, statusi i leximit."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.contacts import Contact
from app.models.inbox import InboundMessage
from app.models.sending import Message
from app.services import consent, events


def record_inbound(
    db: Session,
    owner_ref: str,
    provider: str,
    to_number: str,
    from_number: str,
    text: str,
    provider_message_id: str | None = None,
) -> tuple[InboundMessage | None, str | None]:
    """Ruan mesazhin dhe zbaton STOP/START. Kthen (mesazhi, veprimi); mesazhi është None nëse
    provider-i e ka dërguar më parë (retry) — atëherë asgjë nuk ndryshon."""
    frm, to = from_number.lstrip("+"), to_number.lstrip("+")
    if provider_message_id and db.scalar(
        select(InboundMessage.id).where(
            InboundMessage.provider == provider,
            InboundMessage.provider_message_id == provider_message_id,
        )
    ):
        return None, None
    action = consent.apply_inbound_keyword(db, owner_ref, frm, text)
    now = datetime.now(UTC)
    m = InboundMessage(
        public_id=str(uuid.uuid4()), owner_ref=owner_ref, provider=provider,
        provider_message_id=provider_message_id, to_number=to, from_number=frm, text=text,
        keyword_action=action,
        read_at=now if action else None,  # STOP/START përpunohen automatikisht: s'kërkojnë lexim
        received_at=now,
    )  # fmt: skip
    db.add(m)
    try:
        db.flush()
    except IntegrityError:  # dy retry njëkohësisht
        db.rollback()
        return None, None
    events.emit(db, owner_ref, "message.received", "inbound_message", m.public_id)
    return m, action


def _preview(t: str) -> str:
    t = " ".join(t.split())
    return t if len(t) <= 120 else t[:117] + "..."


def threads(
    db: Session,
    owner_ref: str,
    q: str | None = None,
    unread_only: bool = False,
    before_id: int | None = None,
    limit: int = 30,
) -> dict:
    """Një rresht për numër, me mesazhin e fundit hyrës; më aktivet së pari."""
    I = InboundMessage  # noqa: E741
    unread = func.sum(case((I.read_at.is_(None), 1), else_=0))
    stmt = select(I.from_number, func.max(I.id).label("last_id"), unread.label("unread")).where(
        I.owner_ref == owner_ref
    )
    if q:
        esc = q.lstrip("+").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(I.from_number.like(f"%{esc}%", escape="\\"))
    stmt = stmt.group_by(I.from_number)
    if unread_only:
        stmt = stmt.having(unread > 0)
    if before_id:
        stmt = stmt.having(func.max(I.id) < before_id)
    rows = db.execute(stmt.order_by(func.max(I.id).desc()).limit(limit + 1)).all()
    more = len(rows) > limit
    rows = rows[:limit]
    last = {m.id: m for m in db.scalars(select(I).where(I.id.in_([r.last_id for r in rows])))}
    contacts = {
        c.phone: c
        for c in db.scalars(
            select(Contact).where(
                Contact.owner_ref == owner_ref, Contact.phone.in_([r.from_number for r in rows])
            )
        )
    }
    items = []
    for r in rows:
        m, c = last[r.last_id], contacts.get(r.from_number)
        name = " ".join(x for x in (c.first_name, c.last_name) if x) if c else ""
        items.append({
            "number": r.from_number, "contact_id": c.id if c else None, "name": name or None,
            "unread": int(r.unread or 0), "last_id": r.last_id, "preview": _preview(m.text),
            "keyword_action": m.keyword_action, "last_at": m.received_at,
        })  # fmt: skip
    return {"items": items, "next_before_id": rows[-1].last_id if more else None}


def conversation(db: Session, owner_ref: str, number: str, limit: int = 100) -> dict:
    """Mesazhet hyrëse dhe përgjigjet tona për një numër, të bashkuara në kohë."""
    number = number.lstrip("+")
    inbound = db.scalars(
        select(InboundMessage)
        .where(InboundMessage.owner_ref == owner_ref, InboundMessage.from_number == number)
        .order_by(InboundMessage.id.desc())
        .limit(limit)
    ).all()
    outbound = db.scalars(
        select(Message)
        .where(Message.owner_ref == owner_ref, Message.destination == number)
        .order_by(Message.id.desc())
        .limit(limit)
    ).all()
    items = [
        {"direction": "in", "id": m.public_id, "text": m.text, "at": m.received_at,
         "keyword_action": m.keyword_action, "via": m.to_number}
        for m in inbound
    ] + [
        {"direction": "out", "id": m.public_id, "text": m.text, "at": m.created_at,
         "status": m.status.value, "segments": m.segments, "via": m.sender,
         "error_code": m.error_code}
        for m in outbound
    ]  # fmt: skip
    items.sort(key=lambda i: (_utc(i["at"]), i["direction"]))
    items = items[-limit:]
    c = db.scalar(select(Contact).where(Contact.owner_ref == owner_ref, Contact.phone == number))
    try:
        d = consent.check(db, owner_ref, "sms", number, "transactional")
        blocked = None if d.allowed else d.reason
    except consent.InvalidAddress:
        blocked = "invalid_number"
    return {
        "number": number,
        "contact": {
            "id": c.id,
            "name": " ".join(x for x in (c.first_name, c.last_name) if x) or None,
        }
        if c
        else None,  # fmt: skip
        "blocked": blocked,  # p.sh. blocked:stop_keyword: përgjigjja s'do të dërgohet
        "reply_from": inbound[0].to_number if inbound else None,
        "items": items,
    }


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def mark_read(db: Session, owner_ref: str, number: str) -> int:
    r = db.execute(
        update(InboundMessage)
        .where(
            InboundMessage.owner_ref == owner_ref,
            InboundMessage.from_number == number.lstrip("+"),
            InboundMessage.read_at.is_(None),
        )
        .values(read_at=datetime.now(UTC))
    )
    return r.rowcount


def unread_count(db: Session, owner_ref: str) -> int:
    return int(
        db.scalar(
            select(func.count(InboundMessage.id)).where(
                InboundMessage.owner_ref == owner_ref, InboundMessage.read_at.is_(None)
            )
        )
        or 0
    )


def erase_number(db: Session, owner_ref: str, number: str) -> int:
    """GDPR: fshin çdo mesazh hyrës nga ky numër."""
    return db.execute(
        delete(InboundMessage).where(
            InboundMessage.owner_ref == owner_ref, InboundMessage.from_number == number.lstrip("+")
        )
    ).rowcount


def purge_old(db: Session, days: int, now: datetime | None = None) -> int:
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    return db.execute(delete(InboundMessage).where(InboundMessage.received_at < cutoff)).rowcount
