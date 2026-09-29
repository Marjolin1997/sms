"""Pipeline email: validim → domen i verifikuar → consent → radhë → provider → events."""

import hashlib
import hmac
import json
import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import as_utc
from app.models.email import (
    EMAIL_TRANSITIONS,
    Email,
    EmailEvent,
    EmailStatus,
)
from app.providers import ProviderError, get_email_provider
from app.providers.email import EmailRequest
from app.services import consent, email_domains, email_mime, switches
from app.services.wallet import Conflict, NotFound, WalletError

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = 30
DEFAULT_RATE_LIMIT = 600
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


class InvalidEmail(WalletError):
    code = "invalid_email"


class SenderDomainNotVerified(WalletError):
    code = "sender_domain_not_verified"


def _find(db: Session, owner_ref: str, key: str) -> Email | None:
    return db.scalar(
        select(Email).where(Email.owner_ref == owner_ref, Email.idempotency_key == key)
    )


def _move(db: Session, e: Email, to: EmailStatus, detail: str | None = None) -> None:
    if to not in EMAIL_TRANSITIONS[e.status]:
        raise Conflict(f"illegal email status transition {e.status.value} -> {to.value}")
    db.add(EmailEvent(email_id=e.id, from_status=e.status.value, to_status=to.value, detail=detail))
    e.status = to
    e.updated_at = datetime.now(UTC)


def submit(
    db: Session,
    owner_ref: str,
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

    existing = _find(db, owner_ref, key)
    if existing:
        if existing.request_hash != digest:
            raise Conflict("idempotency key reused with a different request")
        return existing

    if not switches.is_enabled(db, switches.SUBMIT):
        from app.services.messages import SendingPaused

        raise SendingPaused("sending is temporarily paused")
    plan = db.scalar(select(AccountPlan).where(AccountPlan.owner_ref == owner_ref))
    if plan is None or not plan.enabled:
        from app.services.messages import AccountDisabled

        raise AccountDisabled("account has no active sending plan")
    limit = plan.email_rate_limit_per_min or DEFAULT_RATE_LIMIT
    recent = db.scalar(
        select(func.count())
        .select_from(Email)
        .where(Email.owner_ref == owner_ref, Email.created_at > now - timedelta(minutes=1))
    )
    if recent >= limit:
        from app.services.messages import RateLimited

        raise RateLimited(f"limit of {limit} emails per minute exceeded")
    domain = email_domains.verified_domain_for(db, owner_ref, from_email)
    if domain is None:
        raise SenderDomainNotVerified("from address is not on a verified domain of this account")
    consent.assert_may_send(db, owner_ref, "email", to_email, category)

    public_id = str(uuid.uuid4())
    try:
        with db.begin_nested():
            e = Email(
                public_id=public_id, owner_ref=owner_ref, idempotency_key=key,
                request_hash=digest, category=category, domain_id=domain.id,
                from_email=from_email, from_name=from_name, to_email=to_email,
                subject=subject, text_body=text, html_body=html,
                provider=settings.email_provider, next_attempt_at=now,
            )  # fmt: skip
            db.add(e)
            db.flush()
            db.add(EmailEvent(email_id=e.id, from_status=None, to_status="queued"))
    except IntegrityError:
        again = _find(db, owner_ref, key)
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
        db, e.owner_ref, "email", e.to_email, "opt_out", "unsubscribe", "email_link", "recipient",
        evidence=f"one-click unsubscribe from email {public_id}",
    )  # fmt: skip
    return e


# --- Worker -------------------------------------------------------------------------


def claim_next(db: Session, now: datetime | None = None) -> Email | None:
    now = as_utc(now or datetime.now(UTC))
    e = db.scalar(
        select(Email)
        .where(Email.status == EmailStatus.QUEUED, Email.next_attempt_at <= now)
        .order_by(Email.next_attempt_at, Email.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if e is None:
        return None
    _move(db, e, EmailStatus.SENDING)
    e.attempts += 1
    return e


def _fail(db: Session, e: Email, code: str) -> None:
    _move(db, e, EmailStatus.FAILED, code)
    e.error_code = code


def process_one(db: Session, now: datetime | None = None) -> Email | None:
    """Commit para dhe pas thirrjes së provider-it: një crash s'shkakton dërgim të dyfishtë."""
    now = as_utc(now or datetime.now(UTC))
    if not switches.is_enabled(db, switches.DISPATCH):
        return None
    e = claim_next(db, now)
    if e is None:
        return None
    db.commit()
    try:
        domain = email_domains.verified_domain_for(db, e.owner_ref, e.from_email)
        if domain is None:  # DNS u hoq ndërkohë
            raise ProviderError("domain_unverified", temporary=False)
        token = unsubscribe_token(e.public_id) if e.category == "marketing" else None
        msg_id, raw = email_mime.build(
            public_id=e.public_id, domain=domain.domain, from_email=e.from_email,
            from_name=e.from_name, to_email=e.to_email, subject=e.subject,
            text_body=e.text_body, html_body=e.html_body, unsubscribe_token=token,
            dkim_selector=domain.dkim_selector,
            dkim_private_pem=email_domains.decrypt_private_key(domain),
        )  # fmt: skip
        req = EmailRequest(e.public_id, msg_id, e.from_email, e.to_email, raw)
        result = get_email_provider(e.provider).send(req)
    except email_mime.UnsafeHeader:
        _after_error(db, e, "unsafe_header", False, now)
    except ProviderError as ex:
        _after_error(db, e, ex.code, ex.temporary, now)
    except Exception:  # rezultat i panjohur; provider-i deduplikon me Message-ID
        _after_error(db, e, "provider_exception", True, now)
    else:
        e.provider_message_id = result.provider_message_id
        _move(db, e, EmailStatus.SENT, result.provider_message_id)
    db.commit()
    return e


def _after_error(db: Session, e: Email, code: str, temporary: bool, now: datetime) -> None:
    if temporary and e.attempts < MAX_ATTEMPTS:
        e.next_attempt_at = now + timedelta(seconds=BACKOFF_SECONDS * 2 ** (e.attempts - 1))
        e.error_code = code
        _move(db, e, EmailStatus.QUEUED, f"retry:{code}")
    else:
        _fail(db, e, code)


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
    _move(db, e, target, code)
    if target in (EmailStatus.BOUNCED, EmailStatus.COMPLAINED):
        reason = "bounce_hard" if target == EmailStatus.BOUNCED else "complaint"
        consent.record(db, e.owner_ref, "email", e.to_email, "opt_out", reason,
                       "provider_webhook", "system", evidence=code)  # fmt: skip
    return e


def cancel_if_queued(db: Session, email_id: int) -> bool:
    """Anulon një email që worker-i s'e ka marrë ende (SKIP LOCKED)."""
    e = db.scalar(
        select(Email)
        .where(Email.id == email_id, Email.status == EmailStatus.QUEUED)
        .with_for_update(skip_locked=True)
    )
    if e is None:
        return False
    _fail(db, e, "campaign_cancelled")
    return True
