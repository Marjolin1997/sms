from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.admin import Switch

SUBMIT = "submit"  # pranimi i mesazheve të reja
DISPATCH = "dispatch"  # dërgimi nga worker-i te provider-i
NAMES = {SUBMIT, DISPATCH}


def is_enabled(db: Session, name: str) -> bool:
    """Mungesa e rreshtit = i lejuar. Lexohet gjithmonë nga DB (pa cache), që kill switch
    të veprojë menjëherë në të gjitha proceset."""
    row = db.get(Switch, name)
    return True if row is None else row.enabled


def set_switch(db: Session, name: str, enabled: bool, actor: str, reason: str | None) -> Switch:
    row = db.get(Switch, name)
    if row is None:
        row = Switch(name=name)
        db.add(row)
    row.enabled, row.reason, row.updated_by = enabled, reason, actor
    row.updated_at = datetime.now(UTC)
    db.flush()
    return row
