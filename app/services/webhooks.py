"""Webhook-et e klientëve: menaxhim endpoint-esh, dërgim i nënshkruar, retry, circuit breaker."""

import json
import secrets
from datetime import UTC, datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.contracts.signature import sign_v1, verify_v1
from app.core import crypto
from app.core.config import settings
from app.core.errors import Conflict, DomainError, NotFound
from app.core.scope import Owner, owned, ref
from app.core.timeutil import as_utc
from app.models.events import (
    DeliveryStatus,
    EndpointStatus,
    Event,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.services import events, net_guard
from app.services.webhook_queue import (  # noqa: F401  (ri-eksportuar: konstantet e kontratës)
    DISABLE_AFTER,
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    RETRY_DELAYS,
    queue,
)

MAX_ENDPOINTS = 10


class InvalidWebhook(DomainError):
    code = "invalid_webhook"


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def _validate(url: str, event_types: list[str]) -> None:
    try:
        net_guard.validate_url(url)
    except net_guard.UnsafeUrl as e:
        raise InvalidWebhook(str(e)) from e
    if not event_types or len(event_types) > 30:
        raise InvalidWebhook("event_types must have 1..30 entries")
    bad = [t for t in event_types if not events.valid_filter(t)]
    if bad:
        raise InvalidWebhook(f"unknown event types: {bad}")


def create_endpoint(
    db: Session, owner: Owner, url: str, event_types: list[str] | None = None,
    description: str | None = None,
) -> tuple[WebhookEndpoint, str]:  # fmt: skip
    """→ (endpoint, sekreti). Sekreti kthehet vetëm këtu dhe pas rotate_secret."""
    event_types = ["*"] if event_types is None else event_types  # [] është i pavlefshëm
    _validate(url, event_types)
    count = db.scalar(
        select(func.count()).select_from(WebhookEndpoint).where(owned(WebhookEndpoint, owner))
    )
    if count >= MAX_ENDPOINTS:
        raise Conflict(f"at most {MAX_ENDPOINTS} webhook endpoints per account")
    secret = new_secret()
    ep = WebhookEndpoint(
        owner_ref=ref(owner), url=url, secret_enc=crypto.encrypt(secret.encode()),
        event_types=event_types, description=description,
    )  # fmt: skip
    db.add(ep)
    db.flush()
    return ep, secret


def _get(db: Session, owner: Owner, endpoint_id: int, lock: bool = False) -> WebhookEndpoint:
    q = select(WebhookEndpoint).where(
        WebhookEndpoint.id == endpoint_id, owned(WebhookEndpoint, owner)
    )
    ep = db.scalar(q.with_for_update() if lock else q)
    if ep is None:
        raise NotFound("webhook endpoint not found")
    return ep


def update_endpoint(
    db: Session, owner: Owner, endpoint_id: int, url: str | None = None,
    event_types: list[str] | None = None, enabled: bool | None = None,
    description: str | None = None,
) -> WebhookEndpoint:  # fmt: skip
    ep = _get(db, owner, endpoint_id, lock=True)
    new_url = url or ep.url
    new_types = ep.event_types if event_types is None else event_types
    _validate(new_url, new_types)
    ep.url, ep.event_types = new_url, new_types
    if description is not None:
        ep.description = description[:120]
    if enabled is True:
        ep.status, ep.disabled_reason, ep.consecutive_failures = EndpointStatus.ACTIVE, None, 0
    elif enabled is False:
        ep.status, ep.disabled_reason = EndpointStatus.DISABLED, "manual"
    ep.updated_at = datetime.now(UTC)
    db.flush()
    return ep


def delete_endpoint(db: Session, owner: Owner, endpoint_id: int) -> None:
    ep = _get(db, owner, endpoint_id, lock=True)
    for d in db.scalars(select(WebhookDelivery).where(WebhookDelivery.endpoint_id == ep.id)):
        db.delete(d)
    db.delete(ep)


def rotate_secret(db: Session, owner: Owner, endpoint_id: int) -> tuple[WebhookEndpoint, str]:
    ep = _get(db, owner, endpoint_id, lock=True)
    secret = new_secret()
    ep.secret_enc = crypto.encrypt(secret.encode())
    ep.updated_at = datetime.now(UTC)
    db.flush()
    return ep, secret


def send_test(db: Session, owner: Owner, endpoint_id: int) -> Event:
    ep = _get(db, owner, endpoint_id)
    if ep.status != EndpointStatus.ACTIVE:
        raise Conflict("endpoint is disabled")
    return events.emit(db, owner, "webhook.ping", "endpoint", ep.id, {"ok": True},
                       only_endpoint_id=ep.id)  # fmt: skip


def redeliver(db: Session, owner: Owner, delivery_id: int) -> WebhookDelivery:
    d = queue.lock(db, delivery_id, where=owned(WebhookEndpoint, owner))
    if d is None:
        raise NotFound("delivery not found")
    if d.status == DeliveryStatus.PENDING:
        raise Conflict("delivery is still pending")
    queue.replay(db, d, now=datetime.now(UTC))
    return d


# --- Nënshkrimi -----------------------------------------------------------------------


# Kontrata V1 e nënshkrimit jeton te `app.contracts.signature`; emrat e vjetër mbeten (compat).
sign = sign_v1
verify_signature = verify_v1


def envelope(ev: Event) -> bytes:
    return json.dumps(
        {
            "id": f"evt_{ev.id}", "type": ev.type, "created_at": as_utc(ev.created_at).isoformat(),
            "data": {"resource_type": ev.resource_type, "resource_id": ev.resource_id,
                     **(ev.data or {})},
        },
        separators=(",", ":"), sort_keys=True,
    ).encode()  # fmt: skip


# --- Worker ---------------------------------------------------------------------------

_client: httpx.Client | None = None


def set_client(c: httpx.Client | None) -> None:
    global _client
    _client = c


def _http() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=settings.webhook_timeout, follow_redirects=False)
    return _client


def deliver_next(db: Session, now: datetime | None = None) -> WebhookDelivery | None:
    """Merr një delivery të gatshme, dërgon, regjistron rezultatin. Commit para HTTP: një crash
    lë delivery-n me lease dhe ripërpiqet pas LEASE_SECONDS (dërgim të paktën një herë)."""
    now = as_utc(now or datetime.now(UTC))
    d = queue.reserve(db, now)
    if d is None:
        return None
    ep = db.get(WebhookEndpoint, d.endpoint_id)
    ev = db.get(Event, d.event_id)
    url, secret, body = ep.url, crypto.decrypt(ep.secret_enc).decode(), envelope(ev)
    delivery_id, event_id = d.id, ev.id
    db.commit()

    code, error, permanent = None, None, False
    try:
        net_guard.validate_url(url)  # rezolvim i ri para çdo dërgimi (SSRF)
        ts = int(now.timestamp())
        r = _http().post(
            url, content=body,
            headers={
                "Content-Type": "application/json", "User-Agent": "sms-platform-webhooks/1",
                "X-SMS-Signature": sign(secret, ts, body), "X-SMS-Event-Id": f"evt_{event_id}",
                "X-SMS-Delivery-Id": str(delivery_id),
            },
        )  # fmt: skip
        code = r.status_code
        if not 200 <= code < 300:
            error = f"http_{code}"
            permanent = code == 410  # Gone: klienti e ka hequr endpoint-in
    except net_guard.UnsafeUrl as e:
        error, permanent = f"unsafe_url:{e}"[:120], True
    except httpx.HTTPError as e:
        error = f"network:{type(e).__name__}"

    d = queue.lock(db, delivery_id)
    # kyç edhe endpoint-in: numëruesit e dështimeve ndryshohen nga hooks
    db.get(WebhookEndpoint, d.endpoint_id, with_for_update=True)
    d.last_status_code, d.last_error = code, error
    if error is None:
        queue.complete(db, d, now)
    else:
        queue.retry(db, d, now=now, permanent=permanent)
    db.commit()
    return d
