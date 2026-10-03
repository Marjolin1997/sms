from datetime import UTC, datetime


def utcnow() -> datetime:
    """Koha aktuale UTC, aware. Primitive lokale e Central (pa varësi nga `app.core`)."""
    return datetime.now(UTC)
