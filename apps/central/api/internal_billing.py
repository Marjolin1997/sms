"""Sipërfaqja e brendshme e faturimit (M9-g2): `POST /internal/billing/usage-reports`, scope i dedikuar `billing:report`
(NUK pranon `money:report`/`money:read`/`sync:read`). Enterprise-i i raportit duhet të jetë i autorizuar për klientin.
Raportet vetëm ruhen (immutable); faturimi i lexon më vonë. Central nuk thërret kurrë Enterprise sinkronisht."""

import logging
import uuid

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from apps.central.api.deps import get_db
from apps.central.services import billing_usage, service_auth

router = APIRouter(prefix="/internal/billing")
log = logging.getLogger("central.billing_auth")
REPORT_SCOPE = "billing:report"


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def report_context(
    request: Request, authorization: str = Header(default="")
) -> service_auth.ServiceContext:
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    try:
        return service_auth.authenticate(request.app.state.sessionmaker, token, REPORT_SCOPE)
    except service_auth.ScopeDenied as e:
        log.warning(
            "billing auth denied reason=%s client=%s kid=%s ip=%s",
            e.reason,
            e.client_id,
            e.kid,
            _ip(request),
        )
        raise HTTPException(403, {"code": "forbidden", "message": "insufficient scope"}) from None
    except service_auth.ServiceAuthError as e:
        log.warning(
            "billing auth failed reason=%s client=%s kid=%s ip=%s",
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


@router.post("/usage-reports")
def post_usage_report(
    request: Request,
    body: dict = Body(...),
    ctx: service_auth.ServiceContext = Depends(report_context),
    db: Session = Depends(get_db),
):
    """Raport kumulativ (idempotent). 201 = ruajtur · 200 = dublikat identik · 409 = konflikt/rikthim · 422 = i pavlefshëm ·
    403 = enterprise i paautorizuar · 413 = shumë i madh. Asnjë efekt financiar."""
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > billing_usage.MAX_BODY_BYTES:
        raise HTTPException(413, {"code": "too_large", "message": "report is too large"})
    report = billing_usage.parse(body)
    _gen, allowed = service_auth.allowed_enterprises(db, ctx.client_pk)
    if uuid.UUID(report.enterprise_id) not in allowed:
        raise HTTPException(403, {"code": "forbidden", "message": "enterprise not authorized"})
    res = billing_usage.ingest(db, report)
    db.commit()
    return JSONResponse(
        {
            "status": "stored" if res.created else "duplicate",
            "report_id": str(res.row.report_id),
            "report_seq": res.row.report_seq,
            "latest": res.latest,
        },
        status_code=201 if res.created else 200,
    )
