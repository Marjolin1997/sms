"""`PostgresDispatchQueue`: implementimi i parë (dhe i vetmi) i `DispatchQueue`.

Ruan SQL-in e sotëm pa ndryshim: `SELECT … WHERE pending AND next_attempt_at <= :now
ORDER BY next_attempt_at, id LIMIT 1 FOR UPDATE SKIP LOCKED`. Nuk bën kurrë commit/rollback."""

from datetime import datetime, timedelta

from sqlalchemy import and_, select

from app.queue.delivery import DeliveryHooks, DeliveryOutcome, DeliverySpec
from app.queue.dispatch import DispatchHooks, DispatchSpec, Outcome


class PostgresDispatchQueue:
    def __init__(self, spec: DispatchSpec, hooks: DispatchHooks) -> None:
        self.spec, self.hooks = spec, hooks

    def publish(self, db, item, *, not_before: datetime | None = None):
        if not_before is not None:
            setattr(item, self.spec.next_attempt_at.key, not_before)
        db.add(item)
        db.flush()
        return item

    def reserve(self, db, now: datetime):
        s = self.spec
        item = db.scalar(
            select(s.model)
            .where(and_(s.pending, s.next_attempt_at <= now))
            .order_by(s.next_attempt_at, s.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if item is None:
            return None
        self.hooks.reserved(db, item)
        setattr(item, s.attempts.key, getattr(item, s.attempts.key) + 1)
        return item

    def acknowledge(self, db, item, provider_ref: str) -> None:
        self.hooks.sent(db, item, provider_ref)

    def retry(self, db, item, *, error: str, temporary: bool, now: datetime) -> Outcome:
        s = self.spec
        attempts = getattr(item, s.attempts.key)
        if not temporary:
            self.hooks.failed(db, item, error)
            return Outcome.FAILED
        if attempts >= s.max_attempts:
            self.hooks.failed(db, item, error)
            return Outcome.EXHAUSTED
        delay = s.backoff_s * 2 ** (attempts - 1)
        setattr(item, s.next_attempt_at.key, now + timedelta(seconds=delay))
        self.hooks.requeued(db, item, error)
        return Outcome.RETRIED

    def fail(self, db, item, reason: str) -> None:
        self.hooks.failed(db, item, reason)

    def unknown(self, db, item, reason: str) -> None:
        self.hooks.unknown(db, item, reason)

    def cancel_if_pending(self, db, item_id: int, *, reason: str) -> bool:
        """SKIP LOCKED: një item që worker-i e ka në dorë nuk preket (do të dërgohet)."""
        s = self.spec
        item = db.scalar(
            select(s.model).where(s.id == item_id, s.pending).with_for_update(skip_locked=True)
        )
        if item is None:
            return False
        self.hooks.failed(db, item, reason)
        return True


class PostgresDeliveryQueue:
    """Implementimi PostgreSQL i `DeliveryQueue`: lease me `next_attempt_at`, `FOR UPDATE OF <model>
    SKIP LOCKED`. Nuk bën kurrë commit/rollback; caller commit-on lease-in para punës së jashtme."""

    def __init__(self, spec: DeliverySpec, hooks: DeliveryHooks) -> None:
        self.spec, self.hooks = spec, hooks

    def publish(self, db, items) -> None:
        for item in items:
            db.add(item)  # pa flush: rreshtat dalin me transaksionin e biznesit

    def reserve(self, db, now: datetime):
        s = self.spec
        item = db.scalar(
            s.base()
            .where(s.eligible(now))
            .order_by(s.next_attempt_at, s.id)
            .limit(1)
            .with_for_update(of=s.model, skip_locked=True)
        )
        if item is None:
            return None
        setattr(item, s.attempts.key, getattr(item, s.attempts.key) + 1)
        setattr(item, s.next_attempt_at.key, now + timedelta(seconds=s.lease_s))
        return item

    def lock(self, db, item_id: int, *, where=None):
        """FOR UPDATE (pret, jo SKIP LOCKED). Pa `where`: sipas çelësit primar; me `where`: SELECT-i
        bazë me predikatin shtesë të domain-it (p.sh. izolimi i tenant-it)."""
        s = self.spec
        if where is None:
            return db.get(s.model, item_id, with_for_update=True)
        return db.scalar(s.base().where(s.id == item_id, where).with_for_update(of=s.model))

    def complete(self, db, item, now: datetime) -> None:
        self.hooks.completed(db, item, now)

    def retry(self, db, item, *, now: datetime, permanent: bool) -> DeliveryOutcome:
        s = self.spec
        attempts = getattr(item, s.attempts.key)
        if permanent:
            self.hooks.failed(db, item, DeliveryOutcome.FAILED)
            return DeliveryOutcome.FAILED
        if attempts >= s.max_attempts:
            self.hooks.failed(db, item, DeliveryOutcome.EXHAUSTED)
            return DeliveryOutcome.EXHAUSTED
        delay = s.retry_delays_s[attempts - 1]
        setattr(item, s.next_attempt_at.key, now + timedelta(seconds=delay))
        return DeliveryOutcome.RETRIED

    def fail(self, db, item, outcome: DeliveryOutcome) -> None:
        self.hooks.failed(db, item, outcome)

    def replay(self, db, item, *, now: datetime) -> None:
        """Kthen të njëjtin element (identiteti i ruajtur) në pritje: attempts=0, due tani."""
        s = self.spec
        setattr(item, s.attempts.key, 0)
        setattr(item, s.next_attempt_at.key, now)
        self.hooks.replayed(db, item)
        db.flush()
