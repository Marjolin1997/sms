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

from app.core.config import settings
from app.core.context import TenantContext

log = logging.getLogger("sms.scope")
LEGACY_READS: Counter = Counter()  # tabela → sa herë u skopua vetëm me owner_ref (diagnostikim)

Owner = TenantContext | str


def owned(model, owner: Owner):
    if isinstance(owner, TenantContext):
        if settings.tenant_scoping == "owner_ref":  # rikthim emergjent, i shprehur në konfigurim
            return model.owner_ref == owner.owner_ref
        return and_(model.enterprise_id == owner.enterprise_id, model.owner_ref == owner.owner_ref)
    if not isinstance(owner, str):
        raise TypeError(f"owner must be TenantContext or owner_ref str, got {type(owner)!r}")
    LEGACY_READS[model.__tablename__] += 1
    return model.owner_ref == owner


def belongs(row, owner: Owner) -> bool:
    """Rreshti i ngarkuar me `db.get` i përket këtij pronari? (të dyja identitetet përputhen)"""
    if isinstance(owner, TenantContext):
        if settings.tenant_scoping == "owner_ref":
            return row.owner_ref == owner.owner_ref
        return row.enterprise_id == owner.enterprise_id and row.owner_ref == owner.owner_ref
    LEGACY_READS[row.__tablename__] += 1
    return row.owner_ref == owner


def ref(owner: Owner) -> str:
    """`owner_ref` (fushë përputhshmërie) e një pronari, për shkrim/HMAC."""
    return owner.owner_ref if isinstance(owner, TenantContext) else owner
