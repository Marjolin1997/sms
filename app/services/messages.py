"""Pipeline: validim → routing → çmim → rezervim → radhë (outbox në DB) → provider → DLR.

Radha është tabela sms_messages (outbox): mesazhi dhe rezervimi i parave kalojnë në të
njëjtën transaksion, prandaj nuk mund të humbasë asnjë punë ose të rezervohen para pa mesazh.
"""

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
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
from app.models.wallet import Hold, Wallet
from app.providers import ProviderError, SendRequest, get_provider
from app.providers.base import is_idempotent
from app.queue.dispatch import DispatchSpec
from app.queue.postgres import PostgresDispatchQueue
from app.services import (
    audit,
    consent,
    entitlements,
    events,
    pricing,
    rates,
    sender_ids,
    switches,
    templates,
)
from app.services import dispatch_outcome as outcome
from app.services import wallet as wallets
from app.services.sms_text import count_segments

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = 30
AUTO_RESOLVER = "system:dlr_reconciliation"
RESOLUTIONS = ("billable_delivered", "non_billable_failed")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
REASON_MAX = 500


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

    q = pricing.quote(
        db, owner, destination, text, now, plan=plan
    )  # M9-e: motori i vetëm i çmimit (authority-aware)
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
                template_version_id=template_version_id, **pricing.message_fields(q),
                provider=route.provider, next_attempt_at=now,
            )  # fmt: skip
            queue.publish(db, m)
            pricing.record_comparison(
                db, q.shadow, public_id
            )  # shadow: krahasim (pa efekt parash); lokali ngarkohet
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
        m.dispatch_started_at = None  # claim i ri: provider-i s'është thirrur në këtë përpjekje
        _move(db, m, MessageStatus.SENDING)

    def requeued(self, db, m: Message, error: str) -> None:
        m.error_code = error
        m.dispatch_started_at = None  # përpjekje e re: s'është thirrur ende
        _move(db, m, MessageStatus.QUEUED, f"retry:{error}")

    def unknown(self, db, m: Message, reason: str) -> None:
        """M9-a: pa efekt parash (hold-i mbetet ACTIVE), pa ridërgim; vetëm gjendja + historiku."""
        m.error_code = reason[:64]
        _move(db, m, MessageStatus.UNKNOWN, reason)

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
    """Një cikël punonjësi: claim → COMMIT#1 → (kërkesa + marker) → COMMIT#1b → provider (pa tx) →
    finalizim me FOR UPDATE → COMMIT#2.

    M9-a: `dispatch_started_at` ruhet para thirrjes, ndaj sweeper-i dallon "s'u thirr kurrë" (NULL)
    nga "mund të jetë thirrur". Pas thirrjes, gabim i paqartë ose përjashtim i papritur ⇒
    provider idempotent: retry me të njëjtën reference; përndryshe UNKNOWN (pa ridërgim të verbër).
    Finalizimi kërkon që claim-i të jetë ende yni (sweeper/DLR mund ta kenë lëvizur mesazhin)."""
    now = rates.as_utc(now or datetime.now(UTC))
    if not switches.is_enabled(db, switches.DISPATCH):
        return None
    m = claim_next(db, now)
    if m is None:
        return None
    db.commit()  # COMMIT#1: claim (SENDING, attempts+1)
    claim = m.attempts
    try:  # PARA invokimit: gabimet këtu janë të sigurta për retry
        provider = get_provider(m.provider)
        req = SendRequest(m.public_id, m.sender, m.destination, m.text, m.encoding, m.segments)
    except ProviderError as e:
        queue.retry(db, m, error=e.code, temporary=e.temporary, now=now)
        db.commit()
        return m
    except Exception:
        queue.retry(db, m, error="provider_exception", temporary=True, now=now)
        db.commit()
        return m
    m.dispatch_started_at = now
    db.commit()  # COMMIT#1b: nga këtu provider-i MUND të jetë thirrur
    result, err, code, ambiguous, temporary = None, None, None, False, False
    try:
        result = provider.send(req)
    except ProviderError as e:
        err, code, ambiguous, temporary = e, e.code, e.ambiguous, e.temporary
    except Exception:  # thirrja kishte nisur: rezultati i panjohur
        err, code, ambiguous, temporary = True, "provider_exception", True, True
    if not outcome.lock_claim(db, m, claim, MessageStatus.SENDING):
        _lost_claim(db, m, result)  # sweeper/DLR e lëvizi mesazhin gjatë thirrjes
        db.commit()
        return m
    if err is None:
        queue.acknowledge(db, m, result.provider_message_id)
    else:
        outcome.settle_error(
            queue, db, m, provider, code=code, temporary=temporary, ambiguous=ambiguous, now=now
        )
    db.commit()
    return m


def _lost_claim(db: Session, m: Message, result) -> None:
    """Punonjësi e humbi claim-in (p.sh. sweeper → UNKNOWN). Asnjë lëvizje parash/gjendjeje; nëse
    provider-i ktheu id dhe mesazhi është UNKNOWN pa id, ruhet id-ja që DLR-ja ta mbyllë vetë."""
    if result is not None and m.status == MessageStatus.UNKNOWN and not m.provider_message_id:
        m.provider_message_id = result.provider_message_id
        db.add(MessageEvent(message_id=m.id, from_status="unknown", to_status="unknown",
                            detail="late_ack:provider_id_recorded"))  # fmt: skip


def recover_stuck(
    db: Session, lease: timedelta | None = None, now: datetime | None = None, limit: int = 200
) -> outcome.RecoverReport:
    """Sweeper i SENDING të ngecur (> lease). Klasifikim sipas fazës (jo një veprim për të gjitha):
      A. `dispatch_started_at IS NULL` ⇒ provider-i definitivisht s'u thirr ⇒ rirradhitje (ose, pas
         MAX_ATTEMPTS, FAILED+release për provider jo-idempotent; idempotent ⇒ UNKNOWN);
      B. thirrja mund të ketë nisur + provider idempotent + attempts<max ⇒ rirradhitje me të njëjtën
         reference; C. çdo rast tjetër ⇒ UNKNOWN (hold i pandryshuar).
    Idempotent: një rresht i kaluar në UNKNOWN/QUEUED nuk zgjidhet më. Kurrë release/capture."""
    now = rates.as_utc(now or datetime.now(UTC))
    lease = lease or timedelta(seconds=settings.sending_lease_seconds)
    rep = outcome.RecoverReport()
    rows = db.scalars(
        select(Message)
        .where(Message.status == MessageStatus.SENDING, Message.updated_at <= now - lease)
        .order_by(Message.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    for m in rows:
        try:
            provider = get_provider(m.provider)
        except ProviderError:
            provider = None
        idem = provider is not None and is_idempotent(provider)
        exhausted = m.attempts >= MAX_ATTEMPTS
        if m.dispatch_started_at is None:
            if not exhausted:
                _requeue(db, m, now, "recovered:not_dispatched")
                rep.requeued += 1
            elif idem:  # provider idempotent: riprovat e mëparshme mund të kenë qenë të paqarta
                queue.unknown(db, m, "stuck_sending")
                rep.unknown += 1
            else:  # s'u thirr kurrë dhe s'ka riprova të paqarta ⇒ jo-faturueshëm
                _fail(db, m, "recovery_exhausted")
                rep.failed += 1
        elif idem and not exhausted:
            _requeue(db, m, now, "recovered:idempotent_retry")
            rep.requeued += 1
        else:
            queue.unknown(db, m, "stuck_sending")
            rep.unknown += 1
    return rep


def _requeue(db: Session, m: Message, now: datetime, reason: str) -> None:
    m.error_code = reason
    m.dispatch_started_at = None
    m.next_attempt_at = now
    _move(db, m, MessageStatus.QUEUED, reason)


# --- DLR --------------------------------------------------------------------


def apply_dlr(
    db: Session,
    provider: str,
    provider_message_id: str,
    delivered: bool,
    code: str | None = None,
    reference: str | None = None,
) -> Message:
    """Statusi final nga provider-i: DELIVERED → kap shumën, FAILED → e lëshon.
    Idempotent; DLR kontradiktor pas gjendjes finale refuzohet.

    M9-a: një DLR i vlefshëm për mesazh UNKNOWN e zgjidh vetë (delivered ⇒ capture një herë; failed
    final ⇒ release një herë) me audit të sistemit `dlr_reconciliation`. UNKNOWN pa id lidhet vetëm
    me `reference` (= public_id, kur provider-i e kthen te DLR) dhe vetëm për të njëjtin provider;
    një id i ndryshëm nga ai i ruajtur ⇒ Conflict (kurrë mbishkrim)."""
    m = db.scalar(
        select(Message)
        .where(Message.provider == provider, Message.provider_message_id == provider_message_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if m is None and reference:
        m = db.scalar(
            select(Message)
            .where(
                Message.public_id == reference,
                Message.provider == provider,
                Message.status == MessageStatus.UNKNOWN,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if m is not None:
            if m.provider_message_id and m.provider_message_id != provider_message_id:
                raise Conflict("DLR provider id conflicts with the recorded provider id")
            m.provider_message_id = provider_message_id
    if m is None:
        raise NotFound("message not found (DLR may have arrived before SENT was committed)")
    target = MessageStatus.DELIVERED if delivered else MessageStatus.FAILED
    if m.status in TERMINAL:
        if m.status == target:
            return m
        raise Conflict(f"conflicting DLR: message already {m.status.value}")
    from_unknown = m.status == MessageStatus.UNKNOWN
    if delivered:
        _move(db, m, MessageStatus.DELIVERED, "dlr:late" if from_unknown else None)
        wallets.capture(db, m.hold_id)
    else:
        _fail(db, m, code or "dlr_failed")
    if from_unknown:
        audit.system_event(
            db, AUTO_RESOLVER, "message.unknown_auto_resolve", "message", m.public_id,
            _resolution_detail(db, m, "billable_delivered" if delivered else "non_billable_failed",
                               source="dlr", reason=None),
        )  # fmt: skip
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


# --- UNKNOWN (M9-a) ---------------------------------------------------------------------------


def public_status(m: Message) -> str:
    """Statusi për klientin: UNKNOWN është detaj i brendshëm ⇒ klienti sheh `sending`."""
    return "sending" if m.status == MessageStatus.UNKNOWN else m.status.value


def _reason(value) -> str:
    if not isinstance(value, str):
        raise InvalidMessage("a reason is required")
    reason = value.strip()
    if not reason or len(reason) > REASON_MAX or _CTRL.search(reason):
        raise InvalidMessage(
            f"reason must be 1..{REASON_MAX} characters without control characters"
        )
    return reason


def _resolution_detail(db: Session, m: Message, outcome_: str, *, source: str, reason) -> dict:
    hold = db.get(Hold, m.hold_id)
    return {
        "previous_state": "unknown", "outcome": outcome_, "source": source, "reason": reason,
        "hold_id": m.hold_id, "amount": str(hold.amount) if hold else None, "currency": m.currency,
        "provider": m.provider, "provider_message_id": m.provider_message_id,
    }  # fmt: skip


def _locked_by_public_id(db: Session, public_id: str) -> Message:
    m = db.scalar(
        select(Message)
        .where(Message.public_id == public_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if m is None:
        raise NotFound("message not found")
    return m


def _bind_provider_id(db: Session, m: Message, provider_message_id: str | None) -> None:
    if not provider_message_id:
        return
    if len(provider_message_id) > 128 or _CTRL.search(provider_message_id):
        raise InvalidMessage("invalid provider_message_id")
    if m.provider_message_id and m.provider_message_id != provider_message_id:
        raise Conflict("provider_message_id conflicts with the recorded one")
    clash = db.scalar(
        select(Message.id).where(
            Message.provider == m.provider,
            Message.provider_message_id == provider_message_id,
            Message.id != m.id,
        )
    )
    if clash is not None:
        raise Conflict("provider_message_id already belongs to another message")
    m.provider_message_id = provider_message_id


def resolve_unknown(
    db: Session,
    public_id: str,
    outcome_: str,
    *,
    actor: str,
    role: str,
    reason: str,
    provider_message_id: str | None = None,
) -> Message:
    """Zgjidhje eksplicite e një mesazhi UNKNOWN nga stafi (audit + para në të njëjtin transaksion).
      * `billable_delivered`   → DELIVERED + `wallets.capture(hold)` një herë;
      * `non_billable_failed`  → FAILED + `wallets.release(hold)` një herë.
    Vetëm UNKNOWN zgjidhet. Replay me të njëjtin rezultat = no-op (pa audit/lëvizje të dytë);
    rezultat tjetër pas finalizimit = Conflict. Id-ja e provider-it ruhet vetëm pa konflikt."""
    if outcome_ not in RESOLUTIONS:
        raise InvalidMessage(f"outcome must be one of {list(RESOLUTIONS)}")
    reason = _reason(reason)
    m = _locked_by_public_id(db, public_id)
    target = MessageStatus.DELIVERED if outcome_ == "billable_delivered" else MessageStatus.FAILED
    if m.status in TERMINAL:
        if m.status == target:
            return m  # replay / e zgjidhur nga DLR me të njëjtin rezultat: asnjë lëvizje e dytë
        raise Conflict(f"message already {m.status.value}; conflicting resolution refused")
    if m.status != MessageStatus.UNKNOWN:
        raise Conflict(f"only UNKNOWN messages can be resolved (message is {m.status.value})")
    _bind_provider_id(db, m, provider_message_id)
    if target == MessageStatus.DELIVERED:
        _move(db, m, MessageStatus.DELIVERED, "resolved:billable_delivered")
        wallets.capture(db, m.hold_id)
    else:
        _fail(db, m, "unknown_resolved_non_billable")
    detail = _resolution_detail(db, m, outcome_, source="manual", reason=reason)
    audit._append(
        db, actor=actor, role=role, action="message.unknown_resolve", target_type="message",
        target_id=m.public_id, detail=detail,
    )  # fmt: skip
    return m


def attach_provider_message_id(
    db: Session, public_id: str, provider_message_id: str, *, actor: str, role: str, reason: str
) -> Message:
    """Regjistron id-në e provider-it te një UNKNOWN (pa lëvizur para/gjendje) që një DLR i
    mëvonshëm ta zgjidhë vetë. Id e ndryshme nga ajo e ruajtur ⇒ Conflict."""
    reason = _reason(reason)
    m = _locked_by_public_id(db, public_id)
    if m.status != MessageStatus.UNKNOWN:
        raise Conflict(f"only UNKNOWN messages can be updated (message is {m.status.value})")
    if not provider_message_id:
        raise InvalidMessage("provider_message_id is required")
    if m.provider_message_id == provider_message_id:
        return m
    _bind_provider_id(db, m, provider_message_id)
    db.add(MessageEvent(message_id=m.id, from_status="unknown", to_status="unknown",
                        detail="provider_id_attached"))  # fmt: skip
    audit._append(
        db, actor=actor, role=role, action="message.unknown_attach_id", target_type="message",
        target_id=m.public_id, detail=_resolution_detail(db, m, "attach_provider_id",
                                                         source="manual", reason=reason),
    )  # fmt: skip
    return m


def list_unknown(db: Session, now: datetime | None = None, limit: int = 200) -> list[dict]:
    """Vetëm lexim për staf: UNKNOWN me moshë, provider, shumën e mbajtur, përpjekjen e fundit."""
    now = rates.as_utc(now or datetime.now(UTC))
    rows = db.execute(
        select(Message, Hold.amount)
        .join(Hold, Hold.id == Message.hold_id)
        .where(Message.status == MessageStatus.UNKNOWN)
        .order_by(Message.updated_at, Message.id)
        .limit(max(1, min(limit, 500)))
    ).all()
    return [
        {"kind": "sms", "id": m.public_id, "owner_ref": m.owner_ref, "provider": m.provider,
         "provider_message_id": m.provider_message_id, "attempts": m.attempts,
         "reason": m.error_code, "held_amount": str(amount), "currency": m.currency,
         "dispatch_started_at": m.dispatch_started_at, "unknown_since": m.updated_at,
         "age_seconds": max(0, int((now - rates.as_utc(m.updated_at)).total_seconds())),
         "created_at": m.created_at}
        for m, amount in rows
    ]  # fmt: skip
