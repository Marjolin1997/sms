"""Spec-u dhe hooks të webhook-ëve për `DeliveryQueue`; këtu jeton politika e endpoint-it.

Moduli i veçantë shmang ciklin events ↔ webhooks: `events.emit` publikon, `webhooks` dërgon."""

from datetime import datetime

from sqlalchemy import and_, select

from app.models.events import DeliveryStatus, EndpointStatus, WebhookDelivery, WebhookEndpoint
from app.queue.delivery import DeliveryOutcome, DeliverySpec
from app.queue.postgres import PostgresDeliveryQueue

RETRY_DELAYS = [30, 120, 600, 1800, 7200, 21600, 43200]  # sekonda; 8 përpjekje gjithsej
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
LEASE_SECONDS = 120  # nëse worker-i vdes gjatë dërgimit, delivery ripërpiqet pas kësaj
DISABLE_AFTER = 5  # delivery të shterura radhazi → endpoint çaktivizohet


class _WebhookHooks:
    """Efektet e domain-it: gjendja e delivery-t dhe politika e endpoint-it."""

    def completed(self, db, d: WebhookDelivery, now: datetime) -> None:
        d.status, d.delivered_at = DeliveryStatus.SUCCEEDED, now
        db.get(WebhookEndpoint, d.endpoint_id).consecutive_failures = 0

    def failed(self, db, d: WebhookDelivery, outcome: DeliveryOutcome) -> None:
        d.status = DeliveryStatus.FAILED
        ep = db.get(WebhookEndpoint, d.endpoint_id)
        ep.consecutive_failures += 1
        permanent = outcome is DeliveryOutcome.FAILED
        if permanent or ep.consecutive_failures >= DISABLE_AFTER:
            ep.status = EndpointStatus.DISABLED
            error = d.last_error or ""
            ep.disabled_reason = (
                "gone"
                if d.last_status_code == 410
                else ("unsafe_url" if error.startswith("unsafe") else "too_many_failures")
            )

    def replayed(self, db, d: WebhookDelivery) -> None:
        d.status = DeliveryStatus.PENDING
        d.last_error = None


queue = PostgresDeliveryQueue(
    DeliverySpec(
        model=WebhookDelivery,
        base=lambda: select(WebhookDelivery).join(
            WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id
        ),
        eligible=lambda now: and_(
            WebhookDelivery.status == DeliveryStatus.PENDING,
            WebhookDelivery.next_attempt_at <= now,
            WebhookEndpoint.status == EndpointStatus.ACTIVE,
        ),
        attempts=WebhookDelivery.attempts,
        next_attempt_at=WebhookDelivery.next_attempt_at,
        id=WebhookDelivery.id,
        lease_s=LEASE_SECONDS,
        retry_delays_s=tuple(RETRY_DELAYS),
    ),
    _WebhookHooks(),
)
