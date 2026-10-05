"""Sipërfaqja e brendshme e parave `cp.money.v1` (M9-c): vetëm lexim, scope i dedikuar `money:read`
(NUK pranon `sync:read`; NUK është API klienti — s'ka asnjë mutacion parash këtu)."""

import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from apps.central.api.deps import get_db
from apps.central.services import money_feed, service_auth

router = APIRouter(prefix="/internal/money")
log = logging.getLogger("central.money_auth")
SCOPE = "money:read"


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def service_context(
    request: Request, authorization: str = Header(default="")
) -> service_auth.ServiceContext:
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    try:
        return service_auth.authenticate(request.app.state.sessionmaker, token, SCOPE)
    except service_auth.ScopeDenied as e:
        log.warning("money auth denied reason=%s client=%s kid=%s ip=%s",
                    e.reason, e.client_id, e.kid, _ip(request))  # fmt: skip
        raise HTTPException(403, {"code": "forbidden", "message": "insufficient scope"}) from None
    except service_auth.ServiceAuthError as e:
        log.warning("money auth failed reason=%s client=%s kid=%s ip=%s",
                    e.reason, e.client_id, e.kid, _ip(request))  # fmt: skip
        raise HTTPException(
            401,
            {"code": "unauthorized", "message": "invalid or missing credentials"},
            headers={"WWW-Authenticate": "Bearer"},
        ) from None


@router.get("/state")
def get_state(
    ctx: service_auth.ServiceContext = Depends(service_context), db: Session = Depends(get_db)
):
    return money_feed.state(db, ctx.client_pk)


@router.get("/changes")
def get_changes(
    after_seq: int = Query(ge=0),
    epoch: uuid.UUID = Query(),
    generation: int = Query(ge=1),
    limit: int = Query(100, ge=1, le=money_feed.MAX_LIMIT),
    ctx: service_auth.ServiceContext = Depends(service_context),
    db: Session = Depends(get_db),
):
    return money_feed.changes(db, ctx.client_pk, after_seq, limit, epoch, generation)
