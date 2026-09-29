"""Webhook-et e klientëve: menaxhim endpoint-esh, dërgim i nënshkruar, retry, circuit breaker."""

import hashlib
import hmac
import json
import secrets
import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.config import settings
from app.core.timeutil import as_utc
from app.models.events import (
    DeliveryStatus,
    EndpointStatus,
    Event,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.services import events, net_guard
from app.services.wallet import Conflict, NotFound, WalletError

MAX_ENDPOINTS = 10
RETRY_DELAYS = [30, 120, 600, 1800, 7200, 21600, 43200]  # sekonda; 8 përpjekje gjithsej
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
LEASE_SECONDS = 120  # nëse worker-i vdes gjatë dërgimit, delivery ripërpiqet pas kësaj
DISABLE_AFTER = 5  # delivery të shterura radhazi → endpoint çaktivizohet


class InvalidWebhook(WalletError):
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
    db: Session, owner_ref: str, url: str, event_types: list[str] | None = None,
    description: str | None = None,
) -> tuple[WebhookEndpoint, str]:  # fmt: skip
    """→ (endpoint, sekreti). Sekreti kthehet vetëm këtu dhe pas rotate_secret."""
    event_types = ["*"] if event_types is None else event_types  # [] është i pavlefshëm
    _validate(url, event_types)
    count = db.scalar(
        select(func.count())
        .select_from(WebhookEndpoint)
        .where(WebhookEndpoint.owner_ref == owner_ref)
    )
    if count >= MAX_ENDPOINTS:
        raise Conflict(f"at most {MAX_ENDPOINTS} webhook endpoints per account")
    secret = new_secret()
    ep = WebhookEndpoint(
        owner_ref=owner_ref, url=url, secret_enc=crypto.encrypt(secret.encode()),
        event_types=event_types, description=description,
    )  # fmt: skip
    db.add(ep)
    db.flush()
    return ep, secret


def _get(db: Session, owner_ref: str, endpoint_id: int, lock: bool = False) -> WebhookEndpoint:
    q = select(WebhookEndpoint).where(
        WebhookEndpoint.id == endpoint_id, WebhookEndpoint.owner_ref == owner_ref
    )
    ep = db.scalar(q.with_for_update() if lock else q)
    if ep is None:
        raise NotFound("webhook endpoint not found")
    return ep


def update_endpoint(
    db: Session, owner_ref: str, endpoint_id: int, url: str | None = None,
    event_types: list[str] | None = None, enabled: bool | None = None,
    description: str | None = None,
) -> WebhookEndpoint:  # fmt: skip
    ep = _get(db, owner_ref, endpoint_id, lock=True)
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


def delete_endpoint(db: Session, owner_ref: str, endpoint_id: int) -> None:
    ep = _get(db, owner_ref, endpoint_id, lock=True)
    for d in db.scalars(select(WebhookDelivery).where(WebhookDelivery.endpoint_id == ep.id)):
        db.delete(d)
    db.delete(ep)


def rotate_secret(db: Session, owner_ref: str, endpoint_id: int) -> tuple[WebhookEndpoint, str]:
    ep = _get(db, owner_ref, endpoint_id, lock=True)
    secret = new_secret()
    ep.secret_enc = crypto.encrypt(secret.encode())
    ep.updated_at = datetime.now(UTC)
    db.flush()
    return ep, secret


def send_test(db: Session, owner_ref: str, endpoint_id: int) -> Event:
    ep = _get(db, owner_ref, endpoint_id)
    if ep.status != EndpointStatus.ACTIVE:
        raise Conflict("endpoint is disabled")
    return events.emit(db, owner_ref, "webhook.ping", "endpoint", ep.id, {"ok": True},
                       only_endpoint_id=ep.id)  # fmt: skip


def redeliver(db: Session, owner_ref: str, delivery_id: int) -> WebhookDelivery:
    d = db.scalar(
        select(WebhookDelivery)
        .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
        .where(WebhookDelivery.id == delivery_id, WebhookEndpoint.owner_ref == owner_ref)
        .with_for_update(of=WebhookDelivery)
    )
    if d is None:
        raise NotFound("delivery not found")
    if d.status == DeliveryStatus.PENDING:
        raise Conflict("delivery is still pending")
    d.status, d.attempts, d.next_attempt_at = DeliveryStatus.PENDING, 0, datetime.now(UTC)
    d.last_error = None
    db.flush()
    return d


# --- Nënshkrimi -----------------------------------------------------------------------


def sign(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def verify_signature(secret: str, header: str, body: bytes, tolerance: int = 300, now=None) -> bool:
    """Për dokumentim/testim: ashtu si duhet ta verifikojë marrësi (me mbrojtje replay)."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (KeyError, ValueError):
        return False
    if abs((now or time.time()) - ts) > tolerance:
        return False
    expected = sign(secret, ts, body).split("v1=")[1]
    return hmac.compare_digest(parts.get("v1", ""), expected)


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
    d = db.scalar(
        select(WebhookDelivery)
        .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
        .where(
            WebhookDelivery.status == DeliveryStatus.PENDING,
            WebhookDelivery.next_attempt_at <= now,
            WebhookEndpoint.status == EndpointStatus.ACTIVE,
        )
        .order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.id)
        .limit(1)
        .with_for_update(of=WebhookDelivery, skip_locked=True)
    )
    if d is None:
        return None
    d.attempts += 1
    d.next_attempt_at = now + timedelta(seconds=LEASE_SECONDS)
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

    d = db.get(WebhookDelivery, delivery_id, with_for_update=True)
    ep = db.get(WebhookEndpoint, d.endpoint_id, with_for_update=True)
    d.last_status_code, d.last_error = code, error
    if error is None:
        d.status, d.delivered_at = DeliveryStatus.SUCCEEDED, now
        ep.consecutive_failures = 0
    elif permanent or d.attempts >= MAX_ATTEMPTS:
        d.status = DeliveryStatus.FAILED
        ep.consecutive_failures += 1
        if permanent or ep.consecutive_failures >= DISABLE_AFTER:
            ep.status = EndpointStatus.DISABLED
            ep.disabled_reason = (
                "gone"
                if code == 410
                else ("unsafe_url" if error.startswith("unsafe") else "too_many_failures")
            )
    else:
        d.next_attempt_at = now + timedelta(seconds=RETRY_DELAYS[d.attempts - 1])
    db.commit()
    return d
