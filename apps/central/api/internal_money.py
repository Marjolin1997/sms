"""Sipërfaqja e brendshme e parave (M9-c/M9-d). Feed-i `cp.money.v1`: vetëm lexim, scope `money:read`.
Raportet e përdorimit (M9-d): `POST /usage-reports` + `GET /reconciliation`, scope i dedikuar `money:report`
(NUK pranon `money:read`/`sync:read`; enterprise-i i raportit duhet të jetë i autorizuar për klientin).
Asnjë mutacion parash: raportet vetëm ruhen (immutable) dhe krahasohen; s'ka korrigjim automatik."""

import logging
import uuid

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from apps.central.api.deps import get_db
from apps.central.services import money_feed, money_reconciliation, service_auth, usage_reports

router = APIRouter(prefix="/internal/money")
log = logging.getLogger("central.money_auth")
SCOPE = "money:read"
REPORT_SCOPE = "money:report"


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _context(request: Request, authorization: str, scope: str) -> service_auth.ServiceContext:
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    try:
        return service_auth.authenticate(request.app.state.sessionmaker, token, scope)
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


def service_context(
    request: Request, authorization: str = Header(default="")
) -> service_auth.ServiceContext:
    return _context(request, authorization, SCOPE)


def report_context(
    request: Request, authorization: str = Header(default="")
) -> service_auth.ServiceContext:
    return _context(request, authorization, REPORT_SCOPE)


def _authorize_enterprise(
    db: Session, ctx: service_auth.ServiceContext, enterprise_id: uuid.UUID
) -> None:
    _gen, allowed = service_auth.allowed_enterprises(db, ctx.client_pk)
    if enterprise_id not in allowed:
        raise HTTPException(403, {"code": "forbidden", "message": "enterprise not authorized"})


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


@router.post("/usage-reports")
def post_usage_report(
    request: Request,
    body: dict = Body(...),
    ctx: service_auth.ServiceContext = Depends(report_context),
    db: Session = Depends(get_db),
):
    """Raport kumulativ i përdorimit (idempotent). 201 = ruajtur · 200 = dublikat identik · 409 = konflikt
    (i njëjti report_id/seq me përmbajtje tjetër, ose watermark i prapambetur) · 422 = i pavlefshëm · 403 = enterprise
    i paautorizuar. Asnjë efekt financiar."""
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > usage_reports.MAX_BODY_BYTES:
        raise HTTPException(413, {"code": "too_large", "message": "report is too large"})
    report = usage_reports.parse(body)
    _authorize_enterprise(db, ctx, uuid.UUID(report.enterprise_id))
    res = usage_reports.ingest(db, report)
    db.commit()
    from fastapi.responses import JSONResponse

    return JSONResponse(
        {
            "status": "stored" if res.created else "duplicate",
            "report_id": str(res.row.report_id),
            "report_seq": res.row.report_seq,
            "latest": res.latest,
        },
        status_code=201 if res.created else 200,
    )


@router.get("/reconciliation")
def get_reconciliation(
    enterprise_id: uuid.UUID = Query(),
    ctx: service_auth.ServiceContext = Depends(report_context),
    db: Session = Depends(get_db),
):
    """Verdikti i rakordimit për NJË enterprise të autorizuar (vetëm-lexim; përdoret nga readiness-i i Enterprise)."""
    _authorize_enterprise(db, ctx, enterprise_id)
    res = money_reconciliation.reconcile(db, enterprise_id=enterprise_id)
    d = res.to_dict()
    d["discrepancies"] = [x for x in d["discrepancies"] if x["severity"] != "INFO"][:100]
    return d
