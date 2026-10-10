from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from apps.central.core import readiness

router = APIRouter()


@router.get("/healthz")
def healthz():
    """Proces i gjallë; nuk prek DB."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz(request: Request):
    problem = readiness.check(request.app.state.engine)
    if problem:
        return JSONResponse({"status": "not_ready", "reason": problem}, status_code=503)
    return {"status": "ready"}
