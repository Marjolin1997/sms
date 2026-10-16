"""Operacione financiare (M9-f): pamje, alarme, reversal-et e pazgjidhura, gatishmëria e pjesës Central. VETËM LEXIM.
Pa buton "shëno si zgjidhur": korrigjimi është një veprim financiar real (reversal/rregullim/korrigjim i miratuar)."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from apps.central.api.deps import get_db, require_role
from apps.central.core.timeutil import utcnow
from apps.central.models.user import CentralUser, Role
from apps.central.services import financial_ops
from apps.central.services import money_reconciliation as mr

router = APIRouter(prefix="/admin/financial")
READ = require_role(Role.ADMIN, Role.OPERATOR)


@router.get("/overview")
def overview(db: Session = Depends(get_db), _: CentralUser = Depends(READ)):
    snap = financial_ops.snapshot(db)
    db.rollback()
    return snap


@router.get("/alerts")
def alerts(db: Session = Depends(get_db), _: CentralUser = Depends(READ)):
    snap = financial_ops.snapshot(db)
    db.rollback()
    return {"generated_at": snap["generated_at"], "alerts": snap["alerts"]}


@router.get("/unresolved-reversals")
def unresolved_reversals(db: Session = Depends(get_db), _: CentralUser = Depends(READ)):
    items = financial_ops.unresolved_reversals(db, mr.reconcile(db))
    db.rollback()
    return {"items": items}


@router.get("/readiness")
def readiness(db: Session = Depends(get_db), _: CentralUser = Depends(READ)):
    now = utcnow()
    checks = financial_ops.readiness_checks(db, now)
    db.rollback()
    level = (
        "FAIL"
        if any(c.level == "FAIL" for c in checks)
        else "WARN"
        if any(c.level == "WARN" for c in checks)
        else "PASS"
    )
    return {
        "status": level,
        "checks": [{"name": c.name, "level": c.level, "reason": c.reason} for c in checks],
    }
