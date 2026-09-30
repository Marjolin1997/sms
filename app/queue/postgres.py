"""`PostgresDispatchQueue`: implementimi i parë (dhe i vetmi) i `DispatchQueue`.

Ruan SQL-in e sotëm pa ndryshim: `SELECT … WHERE pending AND next_attempt_at <= :now
ORDER BY next_attempt_at, id LIMIT 1 FOR UPDATE SKIP LOCKED`. Nuk bën kurrë commit/rollback."""

from datetime import datetime, timedelta

from sqlalchemy import and_, select

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
