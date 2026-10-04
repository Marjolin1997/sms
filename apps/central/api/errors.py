from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from apps.central.api.registration_public import PublicError
from apps.central.core.errors import Conflict, Invalid, NotFound
from apps.central.services.sync_feed import SyncApiError

_MAP = {NotFound: (404, "not_found"), Conflict: (409, "conflict"), Invalid: (422, "invalid")}


def install(app: FastAPI) -> None:
    async def sync_handler(_request: Request, e: SyncApiError):
        detail = {"code": e.code, "message": str(e), "action": e.action}
        return JSONResponse({"detail": detail}, status_code=e.status)

    async def public_handler(_request: Request, e: PublicError):
        detail = {"code": e.code, "message": e.message}
        return JSONResponse({"detail": detail}, status_code=e.status)

    app.add_exception_handler(SyncApiError, sync_handler)
    app.add_exception_handler(PublicError, public_handler)
    for exc, (status, code) in _MAP.items():

        async def handler(_request: Request, e: Exception, status=status, code=code):
            return JSONResponse({"detail": {"code": code, "message": str(e)}}, status_code=status)

        app.add_exception_handler(exc, handler)
