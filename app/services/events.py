"""Event log + fan-out drejt webhook-eve, në të njëjtin transaksion me ndryshimin (outbox)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.events import (
    DeliveryStatus,
    EndpointStatus,
    Event,
    WebhookDelivery,
    WebhookEndpoint,
)

KNOWN_TYPES = {
    "message.sent", "message.delivered", "message.failed",
    "email.sent", "email.delivered", "email.bounced", "email.complained", "email.failed",
    "campaign.running", "campaign.paused", "campaign.completed", "campaign.cancelled",
    "consent.opted_out", "consent.opted_in", "webhook.ping",
    "invoice.issued", "invoice.paid", "payment.succeeded", "payment.failed",
    "wallet.low_balance", "message.received",
}  # fmt: skip


def valid_filter(pattern: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        return any(t.startswith(pattern[:-1]) for t in KNOWN_TYPES)
    return pattern in KNOWN_TYPES


def matches(patterns: list[str], event_type: str) -> bool:
    return any(
        p == "*" or p == event_type or (p.endswith(".*") and event_type.startswith(p[:-1]))
        for p in patterns
    )


def emit(
    db: Session,
    owner_ref: str,
    type_: str,
    resource_type: str,
    resource_id,
    data: dict | None = None,
    only_endpoint_id: int | None = None,
    now: datetime | None = None,
) -> Event:
    """Shkruan eventin dhe krijon një delivery për çdo endpoint aktiv që përputhet."""
    if type_ not in KNOWN_TYPES:
        raise ValueError(f"unknown event type {type_}")
    now = now or datetime.now(UTC)
    ev = Event(
        owner_ref=owner_ref, type=type_, resource_type=resource_type,
        resource_id=str(resource_id), data=data, created_at=now,
    )  # fmt: skip
    db.add(ev)
    db.flush()
    q = select(WebhookEndpoint).where(
        WebhookEndpoint.owner_ref == owner_ref, WebhookEndpoint.status == EndpointStatus.ACTIVE
    )
    if only_endpoint_id is not None:
        q = q.where(WebhookEndpoint.id == only_endpoint_id)
    for ep in db.scalars(q):
        if only_endpoint_id is not None or matches(ep.event_types, type_):
            db.add(WebhookDelivery(endpoint_id=ep.id, event_id=ev.id, next_attempt_at=now))
    return ev


def purge_old(db: Session, days: int, now: datetime | None = None) -> int:
    """Retention: fshin eventet e vjetra dhe deliveries e tyre të mbyllura (jo ato pending)."""
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    old = select(Event.id).where(Event.created_at < cutoff)
    pending = select(WebhookDelivery.event_id).where(
        WebhookDelivery.status == DeliveryStatus.PENDING
    )
    db.execute(
        delete(WebhookDelivery).where(
            WebhookDelivery.event_id.in_(old), WebhookDelivery.status != DeliveryStatus.PENDING
        )
    )
    res = db.execute(delete(Event).where(Event.id.in_(old), Event.id.not_in(pending)))
    return res.rowcount or 0
