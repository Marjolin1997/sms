"""Pipeline email: validim → domen i verifikuar → consent → radhë → provider → events."""

import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.context import worker_owner
from app.core.errors import Conflict, DomainError, NotFound
from app.core.scope import Owner, owned, ref
from app.core.timeutil import as_utc
from app.models.billing_usage import BILLABLE_STATUSES
from app.models.email import (
    EMAIL_TRANSITIONS,
    Email,
    EmailEvent,
    EmailStatus,
)
from app.providers import ProviderError, get_email_provider
from app.providers.base import is_idempotent
from app.providers.email import EmailRequest
from app.queue.dispatch import DispatchSpec
from app.queue.postgres import PostgresDispatchQueue
from app.services import audit, consent, email_domains, email_mime, entitlements, events, switches
from app.services import dispatch_outcome as outcome
from app.services.billing_usage import record_first_billable

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = 30
AUTO_RESOLVER = "system:email_event_reconciliation"
RESOLUTIONS = ("confirmed_sent", "not_sent")
REASON_MAX = 500
DEFAULT_RATE_LIMIT = 600
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


class InvalidEmail(DomainError):
    code = "invalid_email"


class SenderDomainNotVerified(DomainError):
    code = "sender_domain_not_verified"


def _find(db: Session, owner: Owner, key: str) -> Email | None:
    return db.scalar(select(Email).where(owned(Email, owner), Email.idempotency_key == key))


def _move(db: Session, e: Email, to: EmailStatus, detail: str | None = None) -> None:
    if to not in EMAIL_TRANSITIONS[e.status]:
        raise Conflict(f"illegal email status transition {e.status.value} -> {to.value}")
    db.add(EmailEvent(email_id=e.id, from_status=e.status.value, to_status=to.value, detail=detail))
    previous = e.status
    e.status = to
    e.updated_at = datetime.now(UTC)
    if to.value in BILLABLE_STATUSES and previous.value not in BILLABLE_STATUSES:
        record_first_billable(db, e, to, e.updated_at)  # M9-g2: prova e parë e faturueshmërisë (një INSERT, idempotent)
    if to not in (
        EmailStatus.QUEUED,
        EmailStatus.SENDING,
        EmailStatus.UNKNOWN,
    ):  # UNKNOWN: i brendshëm
        data = {"email_id": e.public_id, "status": to.value}
        if to in (EmailStatus.FAILED, EmailStatus.BOUNCED, EmailStatus.COMPLAINED):
            data["reason"] = detail
        events.emit(db, worker_owner(db, e), f"email.{to.value}", "email", e.public_id, data)


def submit(
    db: Session,
    owner: Owner,
    key: str,
    from_email: str,
    to_email: str,
    subject: str,
    text: str,
    html: str | None = None,
    from_name: str | None = None,
    category: str = "transactional",
    now: datetime | None = None,
) -> Email:
    from app.models.sending import AccountPlan  # shmang varësi rrethore

    if not key or len(key) > 128:
        raise InvalidEmail("idempotency key is required (max 128 chars)")
    if category not in consent.CATEGORIES:
        raise InvalidEmail("category must be 'transactional' or 'marketing'")
    try:
        from_email = consent.normalize("email", from_email)
        to_email = consent.normalize("email", to_email)
    except consent.InvalidAddress as ex:
        raise InvalidEmail(str(ex)) from ex
    if not subject.strip() or len(subject) > 200 or _CTRL.search(subject):
        raise InvalidEmail("subject must be 1-200 characters without control characters")
    if from_name and (len(from_name) > 100 or _CTRL.search(from_name)):
        raise InvalidEmail("invalid from_name")
    if not text.strip() or len(text) > 100_000:
        raise InvalidEmail("text body is required (max 100000 chars)")
    if html and len(html) > 200_000:
        raise InvalidEmail("html body too large")
    now = as_utc(now or datetime.now(UTC))
    digest = hashlib.sha256(
        json.dumps(
            [from_email, from_name, to_email, subject, text, html, category], sort_keys=True
        ).encode()
    ).hexdigest()

    existing = _find(db, owner, key)
    if existing:
        if existing.request_hash != digest:
            raise Conflict("idempotency key reused with a different request")
        return existing

    if not switches.is_enabled(db, switches.SUBMIT):
        from app.services.messages import SendingPaused

        raise SendingPaused("sending is temporarily paused")
    plan = db.scalar(select(AccountPlan).where(owned(AccountPlan, owner)))
    cp = entitlements.gate(  # M7-g: off → None; shadow → vëzhgim; enforce → Decision
        db, plan, owner, "email", plan is not None and bool(plan.enabled),
        (plan.email_rate_limit_per_min or DEFAULT_RATE_LIMIT) if plan is not None else None,
    )  # fmt: skip
    if plan is None or not plan.enabled:
        from app.services.messages import AccountDisabled

        raise AccountDisabled("account has no active sending plan")  # legacy deny fiton
    if cp is not None and not cp.allow:
        from app.services.messages import denial

        raise denial(cp)
    # Vetëm BURIMI i vlerës ndryshon në enforce; numëruesi/algoritmi poshtë mbeten të pandryshuar.
    limit = (
        (cp.rate_limit or DEFAULT_RATE_LIMIT)
        if cp is not None
        else (plan.email_rate_limit_per_min or DEFAULT_RATE_LIMIT)
    )
    recent = db.scalar(
        select(func.count())
        .select_from(Email)
        .where(owned(Email, owner), Email.created_at > now - timedelta(minutes=1))
    )
    if recent >= limit:
        from app.services.messages import RateLimited

        raise RateLimited(f"limit of {limit} emails per minute exceeded")
    domain = email_domains.verified_domain_for(db, owner, from_email)
    if domain is None:
        raise SenderDomainNotVerified("from address is not on a verified domain of this account")
    consent.assert_may_send(db, owner, "email", to_email, category)

    public_id = str(uuid.uuid4())
    try:
        with db.begin_nested():
            e = Email(
                public_id=public_id, owner_ref=ref(owner), idempotency_key=key,
                request_hash=digest, category=category, domain_id=domain.id,
                from_email=from_email, from_name=from_name, to_email=to_email,
                subject=subject, text_body=text, html_body=html,
                provider=settings.email_provider, next_attempt_at=now,
            )  # fmt: skip
            queue.publish(db, e)
            db.add(EmailEvent(email_id=e.id, from_status=None, to_status="queued"))
    except IntegrityError:
        again = _find(db, owner, key)
        if again and again.request_hash == digest:
            return again
        raise Conflict("idempotency key reused with a different request") from None
    return e


# --- Unsubscribe --------------------------------------------------------------------


def _sig(public_id: str) -> str:
    if not settings.pii_hmac_key:
        raise RuntimeError("SMS_PII_HMAC_KEY is not configured")
    mac = hmac.new(settings.pii_hmac_key.encode(), f"unsub:{public_id}".encode(), hashlib.sha256)
    return mac.hexdigest()[:32]


def unsubscribe_token(public_id: str) -> str:
    """Pa PII në URL: vetëm id e email-it + nënshkrim."""
    return f"{public_id}.{_sig(public_id)}"


def valid_unsubscribe_token(token: str) -> bool:
    public_id, _, sig = token.partition(".")
    return bool(sig) and hmac.compare_digest(sig, _sig(public_id))


def unsubscribe(db: Session, token: str) -> Email:
    public_id, _, sig = token.partition(".")
    if not sig or not hmac.compare_digest(sig, _sig(public_id)):
        raise NotFound("invalid unsubscribe link")
    e = db.scalar(select(Email).where(Email.public_id == public_id))
    if e is None:
        raise NotFound("invalid unsubscribe link")
    consent.record(
        db, worker_owner(db, e), "email", e.to_email, "opt_out", "unsubscribe", "email_link",
        "recipient",
        evidence=f"one-click unsubscribe from email {public_id}",
    )  # fmt: skip
    return e


# --- Worker -------------------------------------------------------------------------


def claim_next(db: Session, now: datetime | None = None) -> Email | None:
    return queue.reserve(db, as_utc(now or datetime.now(UTC)))


def _fail(db: Session, e: Email, code: str) -> None:
    _move(db, e, EmailStatus.FAILED, code)
    e.error_code = code


class _EmailHooks:
    """Tranzicionet e email: `_move` mbetet burimi i vetëm i state machine-it."""

    def reserved(self, db, e: Email) -> None:
        e.dispatch_started_at = None  # claim i ri: provider-i s'është thirrur në këtë përpjekje
        _move(db, e, EmailStatus.SENDING)

    def requeued(self, db, e: Email, error: str) -> None:
        e.error_code = error
        e.dispatch_started_at = None
        _move(db, e, EmailStatus.QUEUED, f"retry:{error}")

    def unknown(self, db, e: Email, reason: str) -> None:
        """M9-a: rezultat i panjohur ⇒ UNKNOWN; pa ridërgim, pa efekt tjetër (email s'ka para)."""
        e.error_code = reason[:64]
        _move(db, e, EmailStatus.UNKNOWN, reason)

    def sent(self, db, e: Email, provider_ref: str) -> None:
        e.provider_message_id = provider_ref
        _move(db, e, EmailStatus.SENT, provider_ref)

    def failed(self, db, e: Email, reason: str) -> None:
        _fail(db, e, reason)


queue = PostgresDispatchQueue(
    DispatchSpec(
        model=Email,
        pending=Email.status == EmailStatus.QUEUED,
        attempts=Email.attempts,
        next_attempt_at=Email.next_attempt_at,
        id=Email.id,
        backoff_s=BACKOFF_SECONDS,
        max_attempts=MAX_ATTEMPTS,
    ),
    _EmailHooks(),
)


@dataclass(frozen=True)
class EmailSendPayload:
    """Gjithçka që dërgimi i duhet, e materializuar në primitive PARA se transaksioni të mbyllet:
    provider-i, MIME dhe DKIM nuk prekin ORM/Session (asnjë lazy-load, asnjë SQL gjatë rrjetit)."""

    public_id: str
    category: str
    provider: str
    from_email: str
    from_name: str | None
    to_email: str
    subject: str
    text_body: str | None
    html_body: str | None
    domain: str
    dkim_selector: str
    dkim_private_pem: bytes


def _read_send_inputs(db: Session, e: Email) -> EmailSendPayload:
    """Leximet e DB-së që dërgimi kërkon (domeni i verifikuar, çelësi DKIM), brenda transaksionit të
    claim-it. Përjashtimet e domain-it riklasifikohen pas COMMIT#1 si më parë (shih process_one)."""
    domain = email_domains.verified_domain_for(db, worker_owner(db, e), e.from_email)
    if domain is None:  # DNS u hoq ndërkohë
        raise ProviderError("domain_unverified", temporary=False)
    return EmailSendPayload(
        public_id=e.public_id, category=e.category, provider=e.provider,
        from_email=e.from_email, from_name=e.from_name, to_email=e.to_email,
        subject=e.subject, text_body=e.text_body, html_body=e.html_body,
        domain=domain.domain, dkim_selector=domain.dkim_selector,
        dkim_private_pem=email_domains.decrypt_private_key(domain),
    )  # fmt: skip


def process_one(db: Session, now: datetime | None = None) -> Email | None:
    """claim + leximet e nevojshme → COMMIT#1 → MIME/DKIM (pa tx) → marker `dispatch_started_at` →
    COMMIT#1b → provider.send pa transaksion → finalizim me FOR UPDATE → COMMIT#2.

    M9-a: gabim i paqartë ose përjashtim i papritur PAS fillimit të thirrjes ⇒ provider idempotent:
    retry me të njëjtën reference; përndryshe UNKNOWN (pa ridërgim të verbër). Gabimet para
    marker-it (MIME, domeni, çelësi) janë të sigurta për retry. Crash pas COMMIT#1b ⇒ sweeper-i."""
    now = as_utc(now or datetime.now(UTC))
    if not switches.is_enabled(db, switches.DISPATCH):
        return None
    e = claim_next(db, now)
    if e is None:
        return None
    payload, deferred = None, None
    try:
        payload = _read_send_inputs(db, e)
    except SQLAlchemyError:
        raise  # gabim DB: s'bëhet gabim provider-i; claim-i zhbëhet nga rollback
    except (
        Exception
    ) as ex:  # domen i hequr, çelës i palexueshëm...: klasifikohet pas COMMIT#1, si më parë
        deferred = ex
    db.commit()  # COMMIT#1: claim + leximet; asnjë transaksion i hapur gjatë provider-it
    claim = e.attempts
    try:  # PARA invokimit: gabimet këtu janë të sigurta për retry
        if deferred is not None:
            raise deferred
        token = unsubscribe_token(payload.public_id) if payload.category == "marketing" else None
        msg_id, raw = email_mime.build(
            public_id=payload.public_id, domain=payload.domain, from_email=payload.from_email,
            from_name=payload.from_name, to_email=payload.to_email, subject=payload.subject,
            text_body=payload.text_body, html_body=payload.html_body, unsubscribe_token=token,
            dkim_selector=payload.dkim_selector, dkim_private_pem=payload.dkim_private_pem,
        )  # fmt: skip
        req = EmailRequest(payload.public_id, msg_id, payload.from_email, payload.to_email, raw)
        provider = get_email_provider(payload.provider)
    except email_mime.UnsafeHeader:
        queue.retry(db, e, error="unsafe_header", temporary=False, now=now)
        db.commit()
        return e
    except ProviderError as ex:
        queue.retry(db, e, error=ex.code, temporary=ex.temporary, now=now)
        db.commit()
        return e
    except Exception:
        queue.retry(db, e, error="provider_exception", temporary=True, now=now)
        db.commit()
        return e
    e.dispatch_started_at = now
    db.commit()  # COMMIT#1b: nga këtu provider-i MUND të jetë thirrur
    result, err, code, ambiguous, temporary = None, None, None, False, False
    try:
        result = provider.send(req)
    except ProviderError as ex:
        err, code, ambiguous, temporary = ex, ex.code, ex.ambiguous, ex.temporary
    except Exception:  # thirrja kishte nisur: rezultati i panjohur
        err, code, ambiguous, temporary = True, "provider_exception", True, True
    if not outcome.lock_claim(db, e, claim, EmailStatus.SENDING):
        _lost_claim(db, e, result)  # sweeper/ngjarje e lëvizi email-in gjatë thirrjes
        db.commit()
        return e
    if err is None:
        queue.acknowledge(db, e, result.provider_message_id)
    else:
        outcome.settle_error(
            queue, db, e, provider, code=code, temporary=temporary, ambiguous=ambiguous, now=now
        )
    db.commit()  # COMMIT#2
    return e


def _lost_claim(db: Session, e: Email, result) -> None:
    if result is not None and e.status == EmailStatus.UNKNOWN and not e.provider_message_id:
        e.provider_message_id = result.provider_message_id
        db.add(EmailEvent(email_id=e.id, from_status="unknown", to_status="unknown",
                          detail="late_ack:provider_id_recorded"))  # fmt: skip


def _requeue(db: Session, e: Email, now: datetime, reason: str) -> None:
    e.error_code = reason
    e.dispatch_started_at = None
    e.next_attempt_at = now
    _move(db, e, EmailStatus.QUEUED, reason)


def recover_stuck(
    db: Session, lease: timedelta | None = None, now: datetime | None = None, limit: int = 200
) -> outcome.RecoverReport:
    """Sweeper i SENDING të ngecur (si SMS, pa para): A. s'u thirr kurrë ⇒ rirradhitje; B. provider
    idempotent + attempts<max ⇒ rirradhitje me të njëjtën reference; C. tjetër ⇒ UNKNOWN."""
    now = as_utc(now or datetime.now(UTC))
    lease = lease or timedelta(seconds=settings.sending_lease_seconds)
    rep = outcome.RecoverReport()
    rows = db.scalars(
        select(Email)
        .where(Email.status == EmailStatus.SENDING, Email.updated_at <= now - lease)
        .order_by(Email.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    ).all()
    for e in rows:
        try:
            provider = get_email_provider(e.provider)
        except ProviderError:
            provider = None
        idem = provider is not None and is_idempotent(provider)
        exhausted = e.attempts >= MAX_ATTEMPTS
        if e.dispatch_started_at is None:
            if not exhausted:
                _requeue(db, e, now, "recovered:not_dispatched")
                rep.requeued += 1
            elif idem:
                queue.unknown(db, e, "stuck_sending")
                rep.unknown += 1
            else:
                _fail(db, e, "recovery_exhausted")
                rep.failed += 1
        elif idem and not exhausted:
            _requeue(db, e, now, "recovered:idempotent_retry")
            rep.requeued += 1
        else:
            queue.unknown(db, e, "stuck_sending")
            rep.unknown += 1
    return rep


# --- Events nga provider-i --------------------------------------------------------------

EVENTS = {"delivered", "bounce_hard", "bounce_soft", "complaint"}


def apply_event(
    db: Session, provider: str, provider_message_id: str, event: str, code: str | None = None
) -> Email:
    """delivered / bounce_hard / bounce_soft / complaint. Bounce i ashpër dhe complaint
    bllokojnë adresën (consent hard); bounce_soft vetëm regjistrohet. Idempotent."""
    if event not in EVENTS:
        raise Conflict("unknown event")
    e = db.scalar(
        select(Email)
        .where(Email.provider == provider, Email.provider_message_id == provider_message_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if e is None:
        raise NotFound("email not found (event may have arrived before SENT was committed)")
    if event == "bounce_soft":
        db.add(EmailEvent(email_id=e.id, from_status=e.status.value, to_status=e.status.value,
                          detail=f"soft_bounce:{code or ''}"[:255]))  # fmt: skip
        return e
    target = {
        "delivered": EmailStatus.DELIVERED,
        "bounce_hard": EmailStatus.BOUNCED,
        "complaint": EmailStatus.COMPLAINED,
    }[event]
    if e.status == target:
        return e
    from_unknown = e.status == EmailStatus.UNKNOWN
    _move(db, e, target, code)
    if from_unknown:  # ngjarje autoritative e provider-it e zgjidh UNKNOWN vetë (pa para)
        audit.system_event(db, AUTO_RESOLVER, "email.unknown_auto_resolve", "email", e.public_id,
                           {"previous_state": "unknown", "outcome": target.value,
                            "source": "provider_event", "provider": e.provider,
                            "provider_message_id": e.provider_message_id})  # fmt: skip
    if target in (EmailStatus.BOUNCED, EmailStatus.COMPLAINED):
        reason = "bounce_hard" if target == EmailStatus.BOUNCED else "complaint"
        consent.record(db, worker_owner(db, e), "email", e.to_email, "opt_out", reason,
                       "provider_webhook", "system", evidence=code)  # fmt: skip
    return e


def cancel_if_queued(db: Session, email_id: int) -> bool:
    """Anulon një email që worker-i s'e ka marrë ende (SKIP LOCKED)."""
    return queue.cancel_if_pending(db, email_id, reason="campaign_cancelled")


# --- UNKNOWN (M9-a) ---------------------------------------------------------------------------


def public_status(e: Email) -> str:
    """Klienti sheh `sending` për UNKNOWN (detaj i brendshëm)."""
    return "sending" if e.status == EmailStatus.UNKNOWN else e.status.value


def _reason(value) -> str:
    if not isinstance(value, str):
        raise InvalidEmail("a reason is required")
    reason = value.strip()
    if not reason or len(reason) > REASON_MAX or _CTRL.search(reason):
        raise InvalidEmail(f"reason must be 1..{REASON_MAX} characters without control characters")
    return reason


def _locked_by_public_id(db: Session, public_id: str) -> Email:
    e = db.scalar(
        select(Email)
        .where(Email.public_id == public_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if e is None:
        raise NotFound("email not found")
    return e


def _bind_provider_id(db: Session, e: Email, provider_message_id: str | None) -> None:
    if not provider_message_id:
        return
    if len(provider_message_id) > 190 or _CTRL.search(provider_message_id):
        raise InvalidEmail("invalid provider_message_id")
    if e.provider_message_id and e.provider_message_id != provider_message_id:
        raise Conflict("provider_message_id conflicts with the recorded one")
    clash = db.scalar(
        select(Email.id).where(
            Email.provider == e.provider,
            Email.provider_message_id == provider_message_id,
            Email.id != e.id,
        )
    )
    if clash is not None:
        raise Conflict("provider_message_id already belongs to another email")
    e.provider_message_id = provider_message_id


def _detail(e: Email, outcome_: str, *, source: str, reason) -> dict:
    return {"previous_state": "unknown", "outcome": outcome_, "source": source, "reason": reason,
            "provider": e.provider, "provider_message_id": e.provider_message_id}  # fmt: skip


def resolve_unknown(
    db: Session,
    public_id: str,
    outcome_: str,
    *,
    actor: str,
    role: str,
    reason: str,
    provider_message_id: str | None = None,
) -> Email:
    """Zgjidhje e stafit (pa para: email s'ka wallet). `confirmed_sent` → SENT (provider-i e pranoi;
    DELIVERED/BOUNCED vijnë me ngjarje; s'pretendohet "delivered"); `not_sent` → FAILED. Vetëm
    UNKNOWN; replay me të njëjtin rezultat = no-op; rezultat tjetër = Conflict; audit atomik."""
    if outcome_ not in RESOLUTIONS:
        raise InvalidEmail(f"outcome must be one of {list(RESOLUTIONS)}")
    reason = _reason(reason)
    e = _locked_by_public_id(db, public_id)
    target = EmailStatus.SENT if outcome_ == "confirmed_sent" else EmailStatus.FAILED
    if e.status != EmailStatus.UNKNOWN:
        if e.status == target or (
            target == EmailStatus.SENT
            and e.status in (EmailStatus.DELIVERED, EmailStatus.BOUNCED, EmailStatus.COMPLAINED)
        ):
            return e  # replay / e kaluar tashmë përtej SENT: asnjë ndryshim i dytë
        raise Conflict(f"only UNKNOWN emails can be resolved (email is {e.status.value})")
    _bind_provider_id(db, e, provider_message_id)
    if target == EmailStatus.SENT:
        _move(db, e, EmailStatus.SENT, "resolved:confirmed_sent")
    else:
        _fail(db, e, "unknown_resolved_not_sent")
    audit._append(
        db, actor=actor, role=role, action="email.unknown_resolve", target_type="email",
        target_id=e.public_id, detail=_detail(e, outcome_, source="manual", reason=reason),
    )  # fmt: skip
    return e


def attach_provider_message_id(
    db: Session, public_id: str, provider_message_id: str, *, actor: str, role: str, reason: str
) -> Email:
    reason = _reason(reason)
    e = _locked_by_public_id(db, public_id)
    if e.status != EmailStatus.UNKNOWN:
        raise Conflict(f"only UNKNOWN emails can be updated (email is {e.status.value})")
    if not provider_message_id:
        raise InvalidEmail("provider_message_id is required")
    if e.provider_message_id == provider_message_id:
        return e
    _bind_provider_id(db, e, provider_message_id)
    db.add(EmailEvent(email_id=e.id, from_status="unknown", to_status="unknown",
                      detail="provider_id_attached"))  # fmt: skip
    audit._append(
        db, actor=actor, role=role, action="email.unknown_attach_id", target_type="email",
        target_id=e.public_id,
        detail=_detail(e, "attach_provider_id", source="manual", reason=reason),
    )  # fmt: skip
    return e


def list_unknown(db: Session, now: datetime | None = None, limit: int = 200) -> list[dict]:
    now = as_utc(now or datetime.now(UTC))
    rows = db.scalars(
        select(Email)
        .where(Email.status == EmailStatus.UNKNOWN)
        .order_by(Email.updated_at, Email.id)
        .limit(max(1, min(limit, 500)))
    ).all()
    return [
        {"kind": "email", "id": e.public_id, "owner_ref": e.owner_ref, "provider": e.provider,
         "provider_message_id": e.provider_message_id, "attempts": e.attempts,
         "reason": e.error_code, "held_amount": None, "currency": None,
         "dispatch_started_at": e.dispatch_started_at, "unknown_since": e.updated_at,
         "age_seconds": max(0, int((now - as_utc(e.updated_at)).total_seconds())),
         "created_at": e.created_at}
        for e in rows
    ]  # fmt: skip
