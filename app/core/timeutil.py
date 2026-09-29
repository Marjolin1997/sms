from datetime import UTC, datetime


def as_utc(dt: datetime) -> datetime:
    """Kohë naive = UTC (SQLite); kohë me timezone konvertohet në UTC (PostgreSQL)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
