"""Sipërfaqja e brendshme e sender-ave.

- `cp.sender.v1` (M10-S2): VETËM LEXIM, skop i dedikuar `sender:read` (nuk pranon `sync:read`/`money:read`/…), pa mutacion, pa ack, pa gjendje konsumatori.
  Autorizimi i enterprise-eve është ai ekzistues (`service_client_enterprises` + `auth_generation`): ngjarjet/rreshtat e regjistrit shihen vetëm për enterprise-et e autorizuara; politikat janë globale.
- `POST /internal/sender/requests` (M10-S3): kërkesat Enterprise → Central (`sender.request.v1`), skop i dedikuar `sender:report` (nuk lexon feed-in; `sender:read` nuk raporton).
  Enterprise-i i kërkesës duhet të jetë i autorizuar për klientin (kurrë i besuar vetëm nga trupi). Përgjigjja është ACK dorëzimi: Enterprise NUK e pasqyron në gjendjen lokale."""

import logging
import uuid

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from apps.central.api.deps import get_db
from apps.central.services import sender_requests, sender_sync, service_auth

router = APIRouter(prefix="/internal/sender")
log = logging.getLogger("central.sender_auth")
SCOPE = "sender:read"
REPORT_SCOPE = "sender:report"


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _authenticate(request: Request, authorization: str, scope: str) -> service_auth.ServiceContext:
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    try:
        return service_auth.authenticate(request.app.state.sessionmaker, token, scope)
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


def service_context(
    request: Request, authorization: str = Header(default="")
) -> service_auth.ServiceContext:
    return _authenticate(request, authorization, SCOPE)


def report_context(
    request: Request, authorization: str = Header(default="")
) -> service_auth.ServiceContext:
    return _authenticate(request, authorization, REPORT_SCOPE)


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


@router.post("/requests")
def post_request(
    request: Request,
    body: dict = Body(...),
    ctx: service_auth.ServiceContext = Depends(report_context),
    db: Session = Depends(get_db),
):
    """201 = pranuar (veprim i ri) · 200 = dublikat (i njëjti veprim, pa efekt të ri) · 409 = konflikt identiteti/veprimi · 422 = i pavlefshëm · 403 = enterprise i paautorizuar ·
    413 = shumë i madh. Rezultati i biznesit (miratim/refuzim automatik) është te `auto`; s'është dështim transporti."""
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > sender_requests.MAX_BODY_BYTES:
        raise HTTPException(413, {"code": "too_large", "message": "request is too large"})
    req = sender_requests.parse(body)
    _gen, allowed = service_auth.allowed_enterprises(db, ctx.client_pk)
    if uuid.UUID(req.enterprise_id) not in allowed:
        raise HTTPException(
            403, {"code": "enterprise_not_authorized", "message": "enterprise not authorized"}
        )
    res = sender_requests.process(db, req)
    out = sender_requests.response(res)
    db.commit()
    return JSONResponse(out, status_code=200 if res.duplicate else 201)
