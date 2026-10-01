from datetime import UTC, datetime

from app.core.errors import Conflict
from app.models.messaging import ApprovalStatus as S

_ALLOWED = {
    "approve": ({S.PENDING}, S.APPROVED),
    "reject": ({S.PENDING}, S.REJECTED),
    "revoke": ({S.APPROVED}, S.REVOKED),
    "resubmit": ({S.REJECTED, S.REVOKED}, S.PENDING),
}


def transition(obj, action: str, actor: str, reason: str | None = None) -> None:
    """Makina e vetme e gjendjeve për çdo objekt me miratim."""
    if not actor:
        raise Conflict("actor is required")
    sources, target = _ALLOWED[action]
    if obj.status not in sources:
        raise Conflict(f"cannot {action} from status {obj.status.value}")
    if action in ("reject", "revoke") and not reason:
        raise Conflict("a reason is required")
    obj.status = target
    obj.reviewed_by = actor
    obj.reviewed_at = datetime.now(UTC)
    obj.reason = reason
