"""Sipërfaqja e brendshme e çmimeve (M9-e): vetëm lexim, scope i dedikuar `pricing:read` (nuk pranon `sync:read`/`money:*`).
`GET /internal/pricing/snapshot?known_epoch&known_revision&known_generation` → `{changed:false,…}` ose snapshot-i i plotë
`cp.pricing.v1` për enterprise-et e autorizuara të klientit. Asnjë mutacion çmimesh këtu (administrimi = shërbim i brendshëm)."""

import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from apps.central.api.deps import get_db
from apps.central.services import pricing_feed, service_auth, sync_feed

router = APIRouter(prefix="/internal/pricing")
log = logging.getLogger("central.pricing_auth")
SCOPE = "pricing:read"


def service_context(
    request: Request, authorization: str = Header(default="")
) -> service_auth.ServiceContext:
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    ip = request.client.host if request.client else "unknown"
    try:
        return service_auth.authenticate(request.app.state.sessionmaker, token, SCOPE)
    except service_auth.ScopeDenied as e:
        log.warning(
            "pricing auth denied reason=%s client=%s kid=%s ip=%s", e.reason, e.client_id, e.kid, ip
        )
        raise HTTPException(403, {"code": "forbidden", "message": "insufficient scope"}) from None
    except service_auth.ServiceAuthError as e:
        log.warning(
            "pricing auth failed reason=%s client=%s kid=%s ip=%s", e.reason, e.client_id, e.kid, ip
        )
        raise HTTPException(401, {"code": "unauthorized", "message": "invalid or missing credentials"},
                            headers={"WWW-Authenticate": "Bearer"}) from None  # fmt: skip


@router.get("/state")
def get_state(
    ctx: service_auth.ServiceContext = Depends(service_context), db: Session = Depends(get_db)
):
    return pricing_feed.state(db, ctx.client_pk)


@router.get("/snapshot")
def get_snapshot(
    request: Request,
    known_epoch: uuid.UUID | None = Query(default=None),
    known_revision: int | None = Query(default=None, ge=0),
    known_generation: int | None = Query(default=None, ge=1),
    ctx: service_auth.ServiceContext = Depends(service_context),
):
    with sync_feed.snapshot_session(request.app.state.engine) as db:  # REPEATABLE READ vetëm-lexim
        return pricing_feed.changes(db, ctx.client_pk, None if known_epoch is None else str(known_epoch),
                                    known_revision, known_generation)  # fmt: skip
