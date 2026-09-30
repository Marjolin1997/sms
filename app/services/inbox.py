"""SMS hyrës: ruajtje në inbox, STOP/START, fjalë kyçe me përgjigje automatike, event."""

import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from app.models.contacts import Contact, ContactStatus
from app.models.inbound import InboundMessage, Keyword
from app.services import consent, events, sender_ids
from app.services import messages as msg_svc
from app.services.wallet import Conflict, NotFound, WalletError

MAX_KEYWORDS = 50
REPLY_COOLDOWN_S = 60  # një përgjigje automatike për numër në minutë
_KW = re.compile(r"^[a-z0-9]{2,32}$")
RESERVED = consent.STOP_WORDS | consent.START_WORDS


class InvalidKeyword(WalletError):
    code = "invalid_keyword"


def first_word(text: str) -> str:
    m = re.match(r"\s*([^\W_]+)", text.lower())
    return m.group(1) if m else ""


# --- Fjalët kyçe ------------------------------------------------------------------


def set_keyword(db: Session, owner_ref: str, keyword: str, reply_text: str | None) -> Keyword:
    kw = keyword.strip().lower()
    if not _KW.match(kw):
        raise InvalidKeyword("a keyword is 2-32 letters or digits, no spaces")
    if kw in RESERVED:
        raise InvalidKeyword(f"'{kw}' is reserved (opt-out/opt-in words are handled automatically)")
    row = db.scalar(select(Keyword).where(Keyword.owner_ref == owner_ref, Keyword.keyword == kw))
    if row is None:
        n = db.scalar(
            select(func.count()).select_from(Keyword).where(Keyword.owner_ref == owner_ref)
        )
        if n >= MAX_KEYWORDS:
            raise Conflict(f"at most {MAX_KEYWORDS} keywords per account")
        row = Keyword(owner_ref=owner_ref, keyword=kw)
        db.add(row)
    row.reply_text = (reply_text or "").strip() or None
    db.flush()
    return row


def delete_keyword(db: Session, owner_ref: str, keyword_id: int) -> None:
    row = db.get(Keyword, keyword_id)
    if row is None or row.owner_ref != owner_ref:
        raise NotFound("keyword not found")
    db.delete(row)
    db.flush()


def list_keywords(db: Session, owner_ref: str) -> list[Keyword]:
    return list(
        db.scalars(select(Keyword).where(Keyword.owner_ref == owner_ref).order_by(Keyword.id))
    )


# --- Marrja -----------------------------------------------------------------------


def receive(
    db: Session,
    provider: str,
    provider_message_id: str | None,
    to: str,
    from_: str,
    text: str,
) -> tuple[str, InboundMessage | None]:
    """→ (outcome, rreshti). outcome: opt_out | opt_in | keyword | ignored (të gjitha të ruajtura),
    duplicate, unrouted, invalid_number. Idempotent sipas (provider, provider_message_id)."""
    owners = sender_ids.owners_of_number(db, to)
    if len(owners) != 1:
        return "unrouted", None  # asnjë ose shumë pronarë: nuk hamendësojmë kujt i përket
    owner = next(iter(owners))
    if provider_message_id:
        dup = db.scalar(
            select(InboundMessage).where(
                InboundMessage.provider == provider,
                InboundMessage.provider_message_id == provider_message_id,
            )
        )
        if dup is not None:
            return "duplicate", dup
    try:
        norm_from = consent.normalize("sms", from_)
    except consent.InvalidAddress:
        return "invalid_number", None
    action = consent.apply_inbound_keyword(db, owner, norm_from, text)
    rule = None
    if action is None:
        word = first_word(text)
        if word:
            rule = db.scalar(
                select(Keyword).where(Keyword.owner_ref == owner, Keyword.keyword == word)
            )
    contact_id = db.scalar(
        select(Contact.id).where(
            Contact.owner_ref == owner,
            Contact.phone == norm_from,
            Contact.status == ContactStatus.ACTIVE,
        )
    )
    row = InboundMessage(
        public_id=str(uuid.uuid4()), owner_ref=owner, provider=provider,
        provider_message_id=provider_message_id, from_number=norm_from,
        to_number=to.lstrip("+"), text=text, action=action,
        keyword=rule.keyword if rule else None, contact_id=contact_id,
    )  # fmt: skip
    db.add(row)
    db.flush()
    events.emit(
        db, owner, "message.received", "inbound", row.public_id,
        {"from": norm_from, "to": row.to_number, "text": text, "action": action,
         "keyword": row.keyword},
    )  # fmt: skip
    if rule and rule.reply_text:
        _auto_reply(db, row, rule.reply_text)
    return action or ("keyword" if rule else "ignored"), row


def _auto_reply(db: Session, row: InboundMessage, text: str) -> None:
    """Përgjigje automatike nga numri që mori mesazhin; tarifohet si çdo SMS. Dështimi
    (pa balancë, pa rrugë, pëlqim) nuk e prish marrjen: ruhet si reply_status."""
    since = datetime.now(UTC) - timedelta(seconds=REPLY_COOLDOWN_S)
    recent = db.scalar(
        select(func.count())
        .select_from(InboundMessage)
        .where(
            InboundMessage.owner_ref == row.owner_ref,
            InboundMessage.from_number == row.from_number,
            InboundMessage.reply_message_id.is_not(None),
            InboundMessage.created_at > since,
        )
    )
    if recent:  # kundër ciklit bot-me-bot dhe spam-it të një numri
        row.reply_status = "skipped:cooldown"
        db.flush()
        return
    try:
        with db.begin_nested():
            m = msg_svc.submit(
                db, row.owner_ref, f"mo-reply-{row.public_id}", "+" + row.from_number,
                row.to_number, text=text,
            )  # fmt: skip
        row.reply_message_id = m.public_id
        row.reply_status = "queued"
    except WalletError as e:
        row.reply_status = f"failed:{e.code}"[:64]
    db.flush()


# --- Inbox -------------------------------------------------------------------------


def unread_count(db: Session, owner_ref: str) -> int:
    return db.scalar(
        select(func.count())
        .select_from(InboundMessage)
        .where(InboundMessage.owner_ref == owner_ref, InboundMessage.read_at.is_(None))
    )


def mark_read(db: Session, owner_ref: str, ids: list[int] | None = None) -> int:
    """ids=None → të gjitha të palexuarat e llogarisë. → sa u shënuan."""
    stmt = (
        sa_update(InboundMessage)
        .where(InboundMessage.owner_ref == owner_ref, InboundMessage.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    if ids is not None:
        stmt = stmt.where(InboundMessage.id.in_(ids))
    return db.execute(stmt).rowcount


def scrub_contact(db: Session, owner_ref: str, phone: str) -> None:
    """GDPR: teksti dhe numri i një personi të fshirë largohen nga inbox-i."""
    db.execute(
        sa_update(InboundMessage)
        .where(InboundMessage.owner_ref == owner_ref, InboundMessage.from_number == phone)
        .values(text="[erased]", from_number="erased", contact_id=None)
    )
