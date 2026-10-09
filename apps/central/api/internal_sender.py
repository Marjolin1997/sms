"""Sipërfaqja e brendshme `cp.sender.v1` (M10-S2): VETËM LEXIM, skop i dedikuar `sender:read` (nuk pranon `sync:read`/`money:read`/…), pa mutacion, pa ack, pa gjendje konsumatori.
Autorizimi i enterprise-eve është ai ekzistues (`service_client_enterprises` + `auth_generation`): ngjarjet/rreshtat e regjistrit shihen vetëm për enterprise-et e autorizuara; politikat janë globale."""

import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from apps.central.api.deps import get_db
from apps.central.services import sender_sync, service_auth

router = APIRouter(prefix="/internal/sender")
log = logging.getLogger("central.sender_auth")
SCOPE = "sender:read"


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def service_context(
    request: Request, authorization: str = Header(default="")
) -> service_auth.ServiceContext:
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    try:
        return service_auth.authenticate(request.app.state.sessionmaker, token, SCOPE)
    except service_auth.ScopeDenied as e:
        log.warning(
            "sender auth denied reason=%s client=%s kid=%s ip=%s",
            e.reason,
            e.client_id,
            e.kid,
            _ip(request),
        )
        raise HTTPException(403, {"code": "forbidden", "message": "insufficient scope"}) from None
    except service_auth.ServiceAuthError as e:
        log.warning(
            "sender auth failed reason=%s client=%s kid=%s ip=%s",
            e.reason,
            e.client_id,
            e.kid,
            _ip(request),
        )
        raise HTTPException(
            401,
            {"code": "unauthorized", "message": "invalid or missing credentials"},
            headers={"WWW-Authenticate": "Bearer"},
        ) from None


@router.get("/state")
def get_state(
    ctx: service_auth.ServiceContext = Depends(service_context), db: Session = Depends(get_db)
):
    return sender_sync.state(db, ctx.client_pk)


@router.get("/changes")
def get_changes(
    after_seq: int = Query(ge=0),
    epoch: uuid.UUID = Query(),
    generation: int = Query(ge=1),
    limit: int = Query(100, ge=1, le=sender_sync.MAX_LIMIT),
    ctx: service_auth.ServiceContext = Depends(service_context),
    db: Session = Depends(get_db),
):
    return sender_sync.changes(db, ctx.client_pk, after_seq, limit, epoch, generation)


@router.get("/snapshot")
def get_snapshot(request: Request, ctx: service_auth.ServiceContext = Depends(service_context)):
    with sender_sync.snapshot_session(request.app.state.engine) as db:
        return sender_sync.snapshot(db, ctx.client_pk)
