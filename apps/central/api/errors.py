from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from apps.central.core.errors import Conflict, Invalid, NotFound

_MAP = {NotFound: (404, "not_found"), Conflict: (409, "conflict"), Invalid: (422, "invalid")}


def install(app: FastAPI) -> None:
    for exc, (status, code) in _MAP.items():

        async def handler(_request: Request, e: Exception, status=status, code=code):
            return JSONResponse({"detail": {"code": code, "message": str(e)}}, status_code=status)

        app.add_exception_handler(exc, handler)
