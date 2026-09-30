"""M1c: nga Principal → kontekst i shprehur. Vendi i vetëm ku rruga API vendos TENANT vs SYSTEM."""

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.context import SystemContext, TenantContext, TenantUnresolved, for_owner
from app.core.scope import belongs, owned
from app.core.security import Principal


def tenant(
    db: Session, p: Principal, owner_ref: str | None = None, *, write: bool = False
) -> TenantContext:
    """TENANT: klienti punon gjithmonë në Enterprise-in e çelësit të vet (cross-tenant → 404, pa
    zbulim metadata); stafi duhet të emërtojë `owner_ref` shprehimisht."""
    if p.owner_ref:
        p.check_owner(owner_ref or p.owner_ref)
        if p.enterprise_id is not None:
            ctx = TenantContext(p.enterprise_id, p.owner_ref, "principal")
            db.info.setdefault("_enterprise_ids", {})[ctx.owner_ref] = ctx.enterprise_id
            return ctx
        try:  # çelës legacy pa enterprise_id: zgjidhje vetëm-lexim për përputhshmëri
            return for_owner(db, p.owner_ref, origin="principal")
        except TenantUnresolved as e:
            raise HTTPException(
                403, {"code": "tenant_unresolved", "message": "tenant identity not resolved"}
            ) from e
    if not owner_ref:
        raise HTTPException(422, {"code": "invalid", "message": "owner_ref is required"})
    try:
        return for_owner(db, owner_ref, create=write, origin="staff")
    except TenantUnresolved as e:
        if write:
            raise HTTPException(
                422, {"code": "invalid_owner_ref", "message": "owner_ref has no valid identity"}
            ) from e
        raise HTTPException(404, {"code": "not_found", "message": "resource not found"}) from e


def system(p: Principal, reason: str) -> SystemContext:
    """SYSTEM: vetëm staf; klienti nuk mund ta ndërtojë kurrë."""
    if p.owner_ref:
        raise HTTPException(403, {"code": "forbidden", "message": "staff only"})
    return SystemContext(p.actor, reason)


def access_or_404(db: Session, p: Principal, row) -> None:
    """Rresht i ngarkuar sipas ID: klienti duhet ta ketë në Enterprise-in e vet (përndryshe 404, pa
    zbulim); stafi (me lejen e rrugës) ka qasje të shprehur ndër-tenant."""
    if p.owner_ref is not None and not belongs(row, tenant(db, p)):
        raise HTTPException(404, {"code": "not_found", "message": "resource not found"})


def scoped(db: Session, p: Principal, model, stmt):
    """Klienti: shton skopimin e Enterprise-it të vet në SELECT; stafi: pa filtër (qasje e shprehur,
    e kufizuar nga leja e rrugës). Përdoret për kërkim sipas ID/çelësi publik."""
    if p.owner_ref is None:
        return stmt
    return stmt.where(owned(model, tenant(db, p)))
