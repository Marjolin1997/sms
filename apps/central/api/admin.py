from fastapi import APIRouter, Depends

from apps.central.api.deps import require_role
from apps.central.models.user import CentralUser, Role

router = APIRouter(prefix="/admin")


@router.get("/ping")
def ping(user: CentralUser = Depends(require_role(Role.ADMIN))):
    """Probë e kufirit të sigurisë (vetëm admin); nuk është endpoint biznesi."""
    return {"status": "ok", "role": user.role}
