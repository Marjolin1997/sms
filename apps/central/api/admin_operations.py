"""M11-D: pamje operacionale READ-ONLY për konsolën Central (admin + operator).

Pa shkrime, pa audit për çdo rifreskim, pa thirrje drejt Enterprise."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from apps.central.api.deps import get_db, require_role
from apps.central.models.user import CentralUser, Role
from apps.central.services import sender_operations as ops

router = APIRouter(prefix="/admin/operations")
READ = require_role(Role.ADMIN, Role.OPERATOR)


@router.get("/sender")
def sender_operations(db: Session = Depends(get_db), _: CentralUser = Depends(READ)):
    out = ops.snapshot(db)
    db.rollback()  # vetëm lexim: asnjë transaksion i hapur
    return out
