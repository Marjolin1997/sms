"""Pipeline: validim → routing → çmim → rezervim → radhë (outbox në DB) → provider → DLR.

Radha është tabela sms_messages (outbox): mesazhi dhe rezervimi i parave kalojnë në të
njëjtën transaksion, prandaj nuk mund të humbasë asnjë punë ose të rezervohen para pa mesazh.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.sending import (
    TERMINAL,
    TRANSITIONS,
    AccountPlan,
    Message,
    MessageEvent,
    MessageStatus,
    Route,
)
from app.models.wallet import Wallet
from app.providers import ProviderError, SendRequest, get_provider
from app.services import rates, sender_ids, templates
from app.services import wallet as wallets
from app.services.sms_text import count_segments
from app.services.wallet import Conflict, NotFound, WalletError

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = 30


class InvalidMessage(WalletError):
    code = "invalid_message"


class AccountDisabled(WalletError):
    code = "account_disabled"


class NoRoute(WalletError):
    code = "no_route"


def _find(db: Session, owner_ref: str, key: str) -> Message | None:
    return db.scalar(
        select(Message).where(Message.owner_ref == owner_ref, Message.idempotency_key == key)
    )


def find_route(db: Session, destination: str) -> Route:
    digits = destination.lstrip("+")
    prefixes = [digits[:i] for i in range(1, len(digits) + 1)]
    cands = db.scalars(
        select(Route).where(Route.prefix.in_(prefixes), Route.enabled.is_(True))
    ).all()
    if not cands:
        raise NoRoute("no enabled route for destination")
    return max(cands, key=lambda r: (len(r.prefix), r.priority))


def _move(db: Session, m: Message, to: MessageStatus, detail: str | None = None) -> None:
    if to not in TRANSITIONS[m.status]:
        raise Conflict(f"illegal status transition {m.status.value} -> {to.value}")
    db.add(
        MessageEvent(message_id=m.id, from_status=m.status.value, to_status=to.value, detail=detail)
    )
    m.status = to
    m.updated_at = datetime.now(UTC)


def submit(
    db: Session,
    owner_ref: str,
    key: str,
    destination: str,
    sender: str,
    text: str | None = None,
    template_id: int | None = None,
    values: dict[str, str] | None = None,
    now: datetime | None = None,
) -> Message:
    """Pranon mesazhin: idempotent sipas (owner_ref, key). Kthen mesazhin në QUEUED."""
    if bool(text) == bool(template_id):
        raise InvalidMessage("provide exactly one of text or template_id")
    if not key or len(key) > 128:
        raise InvalidMessage("idempotency key is required (max 128 chars)")
    now = rates.as_utc(now or datetime.now(UTC))
    digest = hashlib.sha256(
        json.dumps([destination, sender, text, template_id, values or {}], sort_keys=True).encode()
    ).hexdigest()

    existing = _find(db, owner_ref, key)
    if existing:
        if existing.request_hash != digest:
            raise Conflict("idempotency key reused with a different request")
        return existing

    plan = db.scalar(select(AccountPlan).where(AccountPlan.owner_ref == owner_ref))
    if plan is None or not plan.enabled:
        raise AccountDisabled("account has no active sending plan")
    if not rates.E164.match(destination):
        raise rates.InvalidNumber("destination must be E.164")
    destination = destination.lstrip("+")
    route = find_route(db, destination)
    sender_ids.assert_usable(db, owner_ref, route.country, sender)

    template_version_id = None
    if template_id:
        r = templates.render(db, owner_ref, template_id, values or {})
        text, template_version_id = r.text, r.version_id
    else:
        try:
            count_segments(text)
        except ValueError as e:
            raise InvalidMessage(str(e)) from e

    q = rates.quote(db, plan.rate_card_id, destination, text, now)
    if q.total <= 0:
        raise rates.NoRate("zero-priced destinations are not supported")
    wallet = db.scalar(
        select(Wallet).where(Wallet.owner_ref == owner_ref, Wallet.currency == q.currency)
    )
    if wallet is None:
        raise NotFound(f"no {q.currency} wallet for account")

    public_id = str(uuid.uuid4())
    try:
        with db.begin_nested():  # dështimi rikthen edhe rezervimin
            hold = wallets.reserve(db, wallet.id, q.total, reference=public_id)
            m = Message(
                public_id=public_id, owner_ref=owner_ref, idempotency_key=key,
                request_hash=digest, wallet_id=wallet.id, hold_id=hold.id,
                sender=sender, destination=destination, country=route.country, text=text,
                template_version_id=template_version_id, encoding=q.encoding,
                segments=q.segments, currency=q.currency, unit_price=q.unit_price,
                total_price=q.total, rate_version_id=q.version_id, rate_id=q.rate_id,
                provider=route.provider, next_attempt_at=now,
            )  # fmt: skip
            db.add(m)
            db.flush()
            db.add(MessageEvent(message_id=m.id, from_status=None, to_status="queued"))
    except IntegrityError:
        # kërkesë paralele me të njëjtin key fitoi garën
        again = _find(db, owner_ref, key)
        if again and again.request_hash == digest:
            return again
        raise Conflict("idempotency key reused with a different request") from None
    return m


# --- Worker -----------------------------------------------------------------


def claim_next(db: Session, now: datetime | None = None) -> Message | None:
    """Merr një mesazh të gatshëm dhe e shënon SENDING. SKIP LOCKED lejon shumë workers."""
    now = rates.as_utc(now or datetime.now(UTC))
    m = db.scalar(
        select(Message)
        .where(Message.status == MessageStatus.QUEUED, Message.next_attempt_at <= now)
        .order_by(Message.next_attempt_at, Message.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if m is None:
        return None
    _move(db, m, MessageStatus.SENDING)
    m.attempts += 1
    return m


def _fail(db: Session, m: Message, code: str) -> None:
    _move(db, m, MessageStatus.FAILED, code)
    m.error_code = code
    wallets.release(db, m.hold_id)


def process_one(db: Session, now: datetime | None = None) -> Message | None:
    """Një cikël punonjësi. Commit para dhe pas thirrjes së provider-it, që një crash
    të mos rezultojë në dërgim të dyfishtë (mesazhi mbetet SENDING për shqyrtim)."""
    now = rates.as_utc(now or datetime.now(UTC))
    m = claim_next(db, now)
    if m is None:
        return None
    db.commit()
    req = SendRequest(m.public_id, m.sender, m.destination, m.text, m.encoding, m.segments)
    try:
        result = get_provider(m.provider).send(req)
    except ProviderError as e:
        _after_error(db, m, e.code, e.temporary, now)
    except Exception:  # rezultati i panjohur; provider-i është idempotent sipas reference
        _after_error(db, m, "provider_exception", True, now)
    else:
        m.provider_message_id = result.provider_message_id
        _move(db, m, MessageStatus.SENT, result.provider_message_id)
    db.commit()
    return m


def _after_error(db: Session, m: Message, code: str, temporary: bool, now: datetime) -> None:
    if temporary and m.attempts < MAX_ATTEMPTS:
        delay = BACKOFF_SECONDS * 2 ** (m.attempts - 1)
        m.next_attempt_at = now + timedelta(seconds=delay)
        m.error_code = code
        _move(db, m, MessageStatus.QUEUED, f"retry:{code}")
    else:
        _fail(db, m, code)


# --- DLR --------------------------------------------------------------------


def apply_dlr(
    db: Session, provider: str, provider_message_id: str, delivered: bool, code: str | None = None
) -> Message:
    """Statusi final nga provider-i: DELIVERED → kap shumën, FAILED → e lëshon.
    Idempotent; DLR kontradiktor pas gjendjes finale refuzohet."""
    m = db.scalar(
        select(Message)
        .where(Message.provider == provider, Message.provider_message_id == provider_message_id)
        .with_for_update()
    )
    if m is None:
        raise NotFound("message not found (DLR may have arrived before SENT was committed)")
    target = MessageStatus.DELIVERED if delivered else MessageStatus.FAILED
    if m.status in TERMINAL:
        if m.status == target:
            return m
        raise Conflict(f"conflicting DLR: message already {m.status.value}")
    if delivered:
        _move(db, m, MessageStatus.DELIVERED)
        wallets.capture(db, m.hold_id)
    else:
        _fail(db, m, code or "dlr_failed")
    return m
