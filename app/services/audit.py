import json

from sqlalchemy.orm import Session

from app.core.security import Principal
from app.models.admin import AuditLog


def audit(
    db: Session, p: Principal, action: str, target_type: str, target_id, detail: dict | None = None
) -> None:
    """Shkruhet në të njëjtin transaksion me ndryshimin: ose të dyja, ose asnjëra."""
    db.add(
        AuditLog(
            actor=p.actor, role=p.role, action=action, target_type=target_type,
            target_id=str(target_id),
            detail=json.dumps(detail, default=str, sort_keys=True) if detail else None,
        )
    )  # fmt: skip
