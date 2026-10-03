from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.tenant import scoped, tenant
from app.core.db import get_db
from app.core.errors import DomainError
from app.core.scope import owned
from app.core.security import Principal, require
from app.models.email import Email, EmailDomain, EmailEvent
from app.services import email_domains as domains
from app.services import emails as svc
from app.services.audit import audit

router = APIRouter(prefix="/v1/email")
_STATUS = {
    "not_found": 404, "conflict": 409, "sender_domain_not_verified": 403,
    "account_disabled": 403, "recipient_suppressed": 422, "sending_paused": 503,
    "rate_limited": 429, "product_not_entitled": 403, "enterprise_suspended": 403,
    "product_suspended": 403,
}  # fmt: skip


def _run(db: Session, fn):
    try:
        out = fn()
        db.commit()
        return out
    except DomainError as e:
        db.rollback()
        raise HTTPException(_STATUS.get(e.code, 422), {"code": e.code, "message": str(e)}) from e


class DomainIn(BaseModel):
    owner_ref: str | None = None
    domain: str = Field(min_length=4, max_length=190)


def _domain_out(d: EmailDomain, with_records: bool = False) -> dict:
    out = {
        "id": d.id, "domain": d.domain, "status": d.status.value, "spf_ok": d.spf_ok,
        "dkim_ok": d.dkim_ok, "dmarc_ok": d.dmarc_ok, "last_checked_at": d.last_checked_at,
    }  # fmt: skip
    if with_records:
        out["dns_records"] = domains.dns_records(d)
    return out


@router.post("/domains", status_code=201)
def add_domain(
    body: DomainIn, db: Session = Depends(get_db), p: Principal = Depends(require("email:write"))
):
    owner = tenant(db, p, body.owner_ref, write=True)

    def go():
        d = domains.create(db, owner, body.domain)
        audit(db, p, "email_domain.add", "email_domain", d.id, {"domain": d.domain})
        return d

    return _domain_out(_run(db, go), with_records=True)


@router.get("/domains")
def list_domains(
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("email:read")),
):
    owner = tenant(db, p, owner_ref)
    rows = db.scalars(select(EmailDomain).where(owned(EmailDomain, owner)).order_by(EmailDomain.id))
    return [_domain_out(d, with_records=d.status.value == "pending") for d in rows]


@router.post("/domains/{domain_id}/verify")
def verify_domain(
    domain_id: int,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("email:write")),
):
    owner = tenant(db, p, owner_ref)

    def go():
        d = domains.verify(db, owner, domain_id)
        audit(db, p, "email_domain.verify", "email_domain", d.id,
              {"status": d.status.value, "spf": d.spf_ok, "dkim": d.dkim_ok})  # fmt: skip
        return d

    return _domain_out(_run(db, go), with_records=True)


class EmailIn(BaseModel):
    owner_ref: str | None = None
    from_email: str = Field(max_length=254)
    from_name: str | None = Field(default=None, max_length=100)
    to: str = Field(max_length=254)
    subject: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=100_000)
    html: str | None = Field(default=None, max_length=200_000)
    category: str = Field(default="transactional", pattern="^(transactional|marketing)$")


def _email_out(e: Email) -> dict:
    return {
        "id": e.public_id, "status": e.status.value, "to": e.to_email,
        "from": e.from_email, "subject": e.subject, "category": e.category,
        "error_code": e.error_code,
    }  # fmt: skip


@router.post("/messages", status_code=202)
def send(
    body: EmailIn,
    idempotency_key: str = Header(default=""),
    db: Session = Depends(get_db),
    p: Principal = Depends(require("email:send")),
):
    owner = tenant(db, p, body.owner_ref, write=True)
    return _email_out(
        _run(
            db,
            lambda: svc.submit(
                db,
                owner,
                idempotency_key,
                body.from_email,
                body.to,
                body.subject,
                body.text,
                body.html,
                body.from_name,
                body.category,
            ),
        )
    )


def _own(db: Session, public_id: str, p: Principal) -> Email:
    e = db.scalar(scoped(db, p, Email, select(Email).where(Email.public_id == public_id)))
    if e is None:
        raise HTTPException(404, {"code": "not_found", "message": "email not found"})
    return e


@router.get("/messages/{public_id}")
def get_email(
    public_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("email:read"))
):
    return _email_out(_own(db, public_id, p))


@router.get("/messages/{public_id}/events")
def email_events(
    public_id: str, db: Session = Depends(get_db), p: Principal = Depends(require("email:read"))
):
    e = _own(db, public_id, p)
    rows = db.scalars(select(EmailEvent).where(EmailEvent.email_id == e.id).order_by(EmailEvent.id))
    return [{"from": x.from_status, "to": x.to_status, "detail": x.detail} for x in rows]
