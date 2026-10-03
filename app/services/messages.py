"""Pipeline: validim → routing → çmim → rezervim → radhë (outbox në DB) → provider → DLR.

Radha është tabela sms_messages (outbox): mesazhi dhe rezervimi i parave kalojnë në të
njëjtën transaksion, prandaj nuk mund të humbasë asnjë punë ose të rezervohen para pa mesazh.
"""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.context import worker_owner
from app.core.errors import Conflict, DomainError, NotFound
from app.core.scope import Owner, owned, ref
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
from app.queue.dispatch import DispatchSpec
from app.queue.postgres import PostgresDispatchQueue
from app.services import consent, entitlements, events, rates, sender_ids, switches, templates
from app.services import wallet as wallets
from app.services.sms_text import count_segments

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = 30


class InvalidMessage(DomainError):
    code = "invalid_message"


class AccountDisabled(DomainError):
    code = "account_disabled"


# M7-g: refuzime nga entitlement-i i Control Plane (enforce). Nënklasa të AccountDisabled (403;
# fushatat pezullohen me `code`); mesazhet janë të sigurta: asnjë detaj sync/kursori te klienti.
class ProductNotEntitled(AccountDisabled):
    code = "product_not_entitled"


class EnterpriseSuspended(AccountDisabled):
    code = "enterprise_suspended"


class ProductSuspended(AccountDisabled):
    code = "product_suspended"


def denial(dec) -> AccountDisabled:
    """Gabimi publik i qëndrueshëm për një `entitlements.Decision` të refuzuar."""
    cls = {c.code: c for c in (ProductNotEntitled, EnterpriseSuspended, ProductSuspended)}[dec.code]
    return cls(entitlements.public_message(dec))


class NoRoute(DomainError):
    code = "no_route"


class SendingPaused(DomainError):
    code = "sending_paused"


class RateLimited(DomainError):
    code = "rate_limited"


DEFAULT_RATE_LIMIT = 600  # mesazhe/minutë për llogari


def _find(db: Session, owner: Owner, key: str) -> Message | None:
    return db.scalar(select(Message).where(owned(Message, owner), Message.idempotency_key == key))


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
    if to in (MessageStatus.SENT, MessageStatus.DELIVERED, MessageStatus.FAILED):
        data = {"message_id": m.public_id, "status": to.value, "segments": m.segments}
        if to == MessageStatus.FAILED:
            data["error_code"] = detail
        events.emit(db, worker_owner(db, m), f"message.{to.value}", "message", m.public_id, data)


def submit(
    db: Session,
    owner: Owner,
    key: str,
    destination: str,
    sender: str,
    text: str | None = None,
    template_id: int | None = None,
    values: dict[str, str] | None = None,
    now: datetime | None = None,
    category: str = "transactional",
) -> Message:
    """Pranon mesazhin: idempotent sipas (owner_ref, key). Kthen mesazhin në QUEUED."""
    if bool(text) == bool(template_id):
        raise InvalidMessage("provide exactly one of text or template_id")
    if not key or len(key) > 128:
        raise InvalidMessage("idempotency key is required (max 128 chars)")
    now = rates.as_utc(now or datetime.now(UTC))
    digest = hashlib.sha256(
        json.dumps(
            [destination, sender, text, template_id, values or {}, category], sort_keys=True
        ).encode()
    ).hexdigest()

    existing = _find(db, owner, key)
    if existing:
        if existing.request_hash != digest:
            raise Conflict("idempotency key reused with a different request")
        return existing

    if not switches.is_enabled(db, switches.SUBMIT):
        raise SendingPaused("sending is temporarily paused")
    plan = db.scalar(select(AccountPlan).where(owned(AccountPlan, owner)))
    cp = entitlements.gate(  # M7-g: off → None; shadow → vëzhgim; enforce → Decision
        db, plan, owner, "sms", plan is not None and bool(plan.enabled),
        (plan.rate_limit_per_min or DEFAULT_RATE_LIMIT) if plan is not None else None,
    )  # fmt: skip
    if plan is None or not plan.enabled:  # legacy deny fiton (break-glass lokal)
        raise AccountDisabled("account has no active sending plan")
    if cp is not None and not cp.allow:
        raise denial(cp)
    # Vetëm BURIMI i vlerës ndryshon në enforce; numëruesi/algoritmi poshtë mbeten të pandryshuar.
    limit = (
        (cp.rate_limit or DEFAULT_RATE_LIMIT)
        if cp is not None
        else (plan.rate_limit_per_min or DEFAULT_RATE_LIMIT)
    )
    recent = db.scalar(
        select(func.count())
        .select_from(Message)
        .where(owned(Message, owner), Message.created_at > now - timedelta(minutes=1))
    )
    if recent >= limit:
        raise RateLimited(f"limit of {limit} messages per minute exceeded")
    if not rates.E164.match(destination):
        raise rates.InvalidNumber("destination must be E.164")
    destination = destination.lstrip("+")
    route = find_route(db, destination)
    sender_ids.assert_usable(db, owner, route.country, sender)
    consent.assert_may_send(db, owner, "sms", destination, category)

    template_version_id = None
    if template_id:
        r = templates.render(db, owner, template_id, values or {})
        text, template_version_id = r.text, r.version_id
    else:
        try:
            count_segments(text)
        except ValueError as e:
            raise InvalidMessage(str(e)) from e

    q = rates.quote(db, plan.rate_card_id, destination, text, now)
    if q.total <= 0:
        raise rates.NoRate("zero-priced destinations are not supported")
    wallet = db.scalar(select(Wallet).where(owned(Wallet, owner), Wallet.currency == q.currency))
    if wallet is None:
        raise NotFound(f"no {q.currency} wallet for account")

    public_id = str(uuid.uuid4())
    try:
        with db.begin_nested():  # dështimi rikthen edhe rezervimin
            hold = wallets.reserve(db, wallet.id, q.total, reference=public_id)
            m = Message(
                public_id=public_id, owner_ref=ref(owner), idempotency_key=key,
                request_hash=digest, wallet_id=wallet.id, hold_id=hold.id,
                category=category, sender=sender, destination=destination,
                country=route.country, text=text,
                template_version_id=template_version_id, encoding=q.encoding,
                segments=q.segments, currency=q.currency, unit_price=q.unit_price,
                total_price=q.total, rate_version_id=q.version_id, rate_id=q.rate_id,
                provider=route.provider, next_attempt_at=now,
            )  # fmt: skip
            queue.publish(db, m)
            db.add(MessageEvent(message_id=m.id, from_status=None, to_status="queued"))
    except IntegrityError:
        # kërkesë paralele me të njëjtin key fitoi garën
        again = _find(db, owner, key)
        if again and again.request_hash == digest:
            return again
        raise Conflict("idempotency key reused with a different request") from None
    return m


# --- Worker -----------------------------------------------------------------


def claim_next(db: Session, now: datetime | None = None) -> Message | None:
    """Merr një mesazh të gatshëm dhe e shënon SENDING. SKIP LOCKED lejon shumë workers."""
    return queue.reserve(db, rates.as_utc(now or datetime.now(UTC)))


def _fail(db: Session, m: Message, code: str) -> None:
    _move(db, m, MessageStatus.FAILED, code)
    m.error_code = code
    wallets.release(db, m.hold_id)


class _SmsHooks:
    """Tranzicionet e SMS: `_move` mbetet burimi i vetëm i të vërtetës për state machine-in."""

    def reserved(self, db, m: Message) -> None:
        _move(db, m, MessageStatus.SENDING)

    def requeued(self, db, m: Message, error: str) -> None:
        m.error_code = error
        _move(db, m, MessageStatus.QUEUED, f"retry:{error}")

    def sent(self, db, m: Message, provider_ref: str) -> None:
        m.provider_message_id = provider_ref
        _move(db, m, MessageStatus.SENT, provider_ref)

    def failed(self, db, m: Message, reason: str) -> None:
        _fail(db, m, reason)


queue = PostgresDispatchQueue(
    DispatchSpec(
        model=Message,
        pending=Message.status == MessageStatus.QUEUED,
        attempts=Message.attempts,
        next_attempt_at=Message.next_attempt_at,
        id=Message.id,
        backoff_s=BACKOFF_SECONDS,
        max_attempts=MAX_ATTEMPTS,
    ),
    _SmsHooks(),
)


def process_one(db: Session, now: datetime | None = None) -> Message | None:
    """Një cikël punonjësi. Commit para dhe pas thirrjes së provider-it, që një crash
    të mos rezultojë në dërgim të dyfishtë (mesazhi mbetet SENDING për shqyrtim)."""
    now = rates.as_utc(now or datetime.now(UTC))
    if not switches.is_enabled(db, switches.DISPATCH):
        return None
    m = claim_next(db, now)
    if m is None:
        return None
    db.commit()
    req = SendRequest(m.public_id, m.sender, m.destination, m.text, m.encoding, m.segments)
    try:
        result = get_provider(m.provider).send(req)
    except ProviderError as e:
        queue.retry(db, m, error=e.code, temporary=e.temporary, now=now)
    except Exception:  # rezultati i panjohur; provider-i është idempotent sipas reference
        queue.retry(db, m, error="provider_exception", temporary=True, now=now)
    else:
        queue.acknowledge(db, m, result.provider_message_id)
    db.commit()
    return m


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


def expire_stale(db: Session, older_than: timedelta, now: datetime | None = None) -> int:
    """SENT pa DLR për më shumë se `older_than` → FAILED dhe rezervimi lirohet, që paratë
    të mos mbeten të bllokuara pafundësisht. Politikë biznesi: klienti nuk paguan për
    mesazh pa konfirmim. DLR i vonuar 'delivered' pas kësaj refuzohet si kontradiktor."""
    now = rates.as_utc(now or datetime.now(UTC))
    stale = db.scalars(
        select(Message)
        .where(Message.status == MessageStatus.SENT, Message.updated_at <= now - older_than)
        .order_by(Message.id)
        .limit(500)
        .with_for_update(skip_locked=True)
    ).all()
    for m in stale:
        _fail(db, m, "dlr_timeout")
    return len(stale)


def stuck_sending(db: Session, older_than: timedelta, now: datetime | None = None) -> list[Message]:
    """Vetëm lexim: SENDING të vjetra (crash gjatë thirrjes së provider-it) për shqyrtim manual."""
    now = rates.as_utc(now or datetime.now(UTC))
    return list(
        db.scalars(
            select(Message).where(
                Message.status == MessageStatus.SENDING, Message.updated_at <= now - older_than
            )
        )
    )


def cancel_if_queued(db: Session, message_id: int) -> bool:
    """Anulon një mesazh që s'është marrë ende nga worker-i; rezervimi lirohet.
    SKIP LOCKED: nëse worker-i e ka në dorë, nuk e prekim (do të dërgohet)."""
    return queue.cancel_if_pending(db, message_id, reason="campaign_cancelled")
