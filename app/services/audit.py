import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.context import SystemContext
from app.core.security import Principal
from app.models.admin import AuditLog

DEDUP_MINUTES = 5


def _append(
    db: Session, *, actor: str, role: str, action: str, target_type: str, target_id: str,
    detail: dict | None,
) -> None:  # fmt: skip
    """Rruga e vetme e shkrimit të audit-it: `AuditLog` në sesionin (transaksionin) e thirrësit.
    Asnjë commit: rreshti del ose zhduket bashkë me ndryshimin e biznesit."""
    db.add(
        AuditLog(
            actor=actor, role=role, action=action, target_type=target_type, target_id=target_id,
            detail=json.dumps(detail, default=str, sort_keys=True) if detail else None,
        )
    )  # fmt: skip


def audit(
    db: Session, p: Principal, action: str, target_type: str, target_id, detail: dict | None = None
) -> None:
    """Shkruhet në të njëjtin transaksion me ndryshimin: ose të dyja, ose asnjëra."""
    _append(
        db, actor=p.actor, role=p.role, action=action, target_type=target_type,
        target_id=str(target_id), detail=detail,
    )  # fmt: skip


def cross_tenant(db, ctx: SystemContext, resource: str, action: str, detail: dict | None = None):
    """Hap lexim ndër-tenant për `resource`; regjistron në audit (i njëjti transaksion).
    Dedup: e njëjta qasje e të njëjtit aktor brenda `DEDUP_MINUTES` shkruan një rresht."""
    if not isinstance(ctx, SystemContext):
        raise TypeError("cross-tenant access requires an explicit SystemContext")
    action_name = f"cross_tenant.{action}"
    recent = datetime.now(UTC) - timedelta(minutes=DEDUP_MINUTES)
    if db.scalar(
        select(AuditLog.id)
        .where(
            AuditLog.actor == ctx.actor,
            AuditLog.action == action_name,
            AuditLog.target_type == resource,
            AuditLog.created_at >= recent,
        )
        .limit(1)
    ):
        return
    _append(
        db, actor=ctx.actor, role="system", action=action_name, target_type=resource,
        target_id="*", detail={"reason": ctx.reason, **(detail or {})},
    )  # fmt: skip


def system_event(
    db: Session, actor: str, action: str, target_type: str, target_id, detail: dict | None = None
) -> None:
    """Veprim automatik i sistemit (jo njeri): role="system", i njëjti transaksion me ndryshimin.
    Thirrësi s'duhet të vendosë sekrete/JWT/çelësa në `detail`."""
    _append(
        db, actor=actor, role="system", action=action, target_type=target_type,
        target_id=str(target_id), detail=detail,
    )  # fmt: skip
