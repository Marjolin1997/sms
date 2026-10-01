"""Event log + fan-out drejt webhook-eve, në të njëjtin transaksion me ndryshimin (outbox)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.contracts.events import PUBLIC_EVENT_TYPES_V1, EventEnvelopeV1
from app.core.context import TenantContext
from app.core.scope import Owner, owned, ref
from app.models.events import (
    DeliveryStatus,
    EndpointStatus,
    Event,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.services.webhook_queue import queue

KNOWN_TYPES = PUBLIC_EVENT_TYPES_V1  # alias kompatibiliteti; burimi i vetëm: app.contracts.events


def to_envelope_v1(ev: Event) -> EventEnvelopeV1:
    """Mapper ORM → kontratë. `ev.data` vjen PAS resource_* dhe fiton në përplasje (e ngrirë)."""
    return EventEnvelopeV1(
        id=f"evt_{ev.id}",
        type=ev.type,
        created_at=ev.created_at,
        data={"resource_type": ev.resource_type, "resource_id": ev.resource_id, **(ev.data or {})},
    )


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


def _eid(owner: Owner):
    """Eventet trashëgojnë identitetin e tenant-it nga konteksti i burimit që po procesohet."""
    return owner.enterprise_id if isinstance(owner, TenantContext) else None


def emit(
    db: Session,
    owner: Owner,
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
        owner_ref=ref(owner), enterprise_id=_eid(owner), type=type_, resource_type=resource_type,
        resource_id=str(resource_id), data=data, created_at=now,
    )  # fmt: skip
    db.add(ev)
    db.flush()
    q = select(WebhookEndpoint).where(
        owned(WebhookEndpoint, owner), WebhookEndpoint.status == EndpointStatus.ACTIVE
    )
    if only_endpoint_id is not None:
        q = q.where(WebhookEndpoint.id == only_endpoint_id)
    for ep in db.scalars(q):
        if only_endpoint_id is not None or matches(ep.event_types, type_):
            queue.publish(
                db, [WebhookDelivery(endpoint_id=ep.id, event_id=ev.id, next_attempt_at=now)]
            )
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
