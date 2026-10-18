from datetime import UTC, datetime


def as_utc(dt: datetime) -> datetime:
    """Kohë naive = UTC (SQLite); kohë me timezone konvertohet në UTC (PostgreSQL)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def utcnow() -> datetime:
    """Koha aktuale UTC, aware. Përdoret si `default=utcnow` (callable), jo `utcnow()`."""
    return datetime.now(UTC)
