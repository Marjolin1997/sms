"""M1c-a: skopimi i query-ve, i shprehur (asnjë filtër global SQLAlchemy).

`owned(Model, owner)`:
  • `TenantContext` → `enterprise_id == ctx.enterprise_id AND owner_ref == ctx.owner_ref`.
    Rreshtat legacy pa enterprise_id NUK shihen (fail-closed); pa fallback të heshtur në owner_ref.
  • `str` → rruga LEGACY e shprehur (`owner_ref == …`) për thirrësit e pa migruar (skripte, rrugë
    të pa migruara). Numërohet (`LEGACY_READS`) që M1d të dijë çfarë mbetet.

`cross_tenant(...)`: e vetmja derë për lexim pa filtër tenant; kërkon `SystemContext` dhe audit."""

import logging
from collections import Counter

from sqlalchemy import and_

from app.core.context import SystemContext, TenantContext

log = logging.getLogger("sms.scope")
DEDUP_MINUTES = 5
LEGACY_READS: Counter = Counter()  # tabela → sa herë u skopua vetëm me owner_ref (diagnostikim)

Owner = TenantContext | str


def owned(model, owner: Owner):
    if isinstance(owner, TenantContext):
        return and_(model.enterprise_id == owner.enterprise_id, model.owner_ref == owner.owner_ref)
    if not isinstance(owner, str):
        raise TypeError(f"owner must be TenantContext or owner_ref str, got {type(owner)!r}")
    LEGACY_READS[model.__tablename__] += 1
    return model.owner_ref == owner


def belongs(row, owner: Owner) -> bool:
    """Rreshti i ngarkuar me `db.get` i përket këtij pronari? (të dyja identitetet përputhen)"""
    if isinstance(owner, TenantContext):
        return row.enterprise_id == owner.enterprise_id and row.owner_ref == owner.owner_ref
    LEGACY_READS[row.__tablename__] += 1
    return row.owner_ref == owner


def ref(owner: Owner) -> str:
    """`owner_ref` (fushë përputhshmërie) e një pronari, për shkrim/HMAC."""
    return owner.owner_ref if isinstance(owner, TenantContext) else owner


def cross_tenant(db, ctx: SystemContext, resource: str, action: str, detail: dict | None = None):
    """Hap lexim ndër-tenant për `resource`; regjistron në audit (i njëjti transaksion)."""
    import json

    from app.models.admin import AuditLog

    if not isinstance(ctx, SystemContext):
        raise TypeError("cross-tenant access requires an explicit SystemContext")
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    action_name = f"cross_tenant.{action}"
    recent = datetime.now(UTC) - timedelta(minutes=DEDUP_MINUTES)
    if db.scalar(  # e njëjta qasje e të njëjtit aktor brenda dritares: një rresht
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
    db.add(
        AuditLog(
            actor=ctx.actor, role="system", action=f"cross_tenant.{action}", target_type=resource,
            target_id="*",
            detail=json.dumps({"reason": ctx.reason, **(detail or {})}, default=str,
                              sort_keys=True),
        )
    )  # fmt: skip
