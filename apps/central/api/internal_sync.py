"""Sipërfaqja e brendshme e sync-ut (vetëm lexim; pa mutacion, pa ack, pa gjendje konsumatori)."""

import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from apps.central.api.deps import get_db
from apps.central.services import service_auth, sync_feed

router = APIRouter(prefix="/internal/sync")
log = logging.getLogger("central.sync_auth")
SCOPE = "sync:read"


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
            "sync auth denied reason=%s client=%s kid=%s ip=%s",
            e.reason,
            e.client_id,
            e.kid,
            _ip(request),
        )
        raise HTTPException(403, {"code": "forbidden", "message": "insufficient scope"}) from None
    except service_auth.ServiceAuthError as e:  # 401 gjenerik; arsyeja vetëm në log
        log.warning(
            "sync auth failed reason=%s client=%s kid=%s ip=%s",
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


@router.get("/changes")
def get_changes(
    after_seq: int = Query(ge=0),
    epoch: uuid.UUID = Query(),
    generation: int = Query(ge=1),
    limit: int = Query(100, ge=1, le=sync_feed.MAX_LIMIT),
    ctx: service_auth.ServiceContext = Depends(service_context),
    db: Session = Depends(get_db),
):
    return sync_feed.changes(db, ctx.client_pk, after_seq, limit, epoch, generation)


@router.get("/snapshot")
def get_snapshot(
    request: Request,
    enterprise_id: uuid.UUID | None = None,
    ctx: service_auth.ServiceContext = Depends(service_context),
):
    with sync_feed.snapshot_session(request.app.state.engine) as db:
        generation, allowed = service_auth.allowed_enterprises(db, ctx.client_pk)
        if enterprise_id is not None and enterprise_id not in allowed:
            raise HTTPException(403, {"code": "forbidden", "message": "enterprise not authorized"})
        return sync_feed.snapshot(db, ctx.client_pk, enterprise_id)
