from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import Principal, require
from app.models.contacts import Contact, ContactList, ContactStatus, ListMember
from app.services import consent
from app.services import contacts as svc
from app.services.audit import audit
from app.services.wallet import WalletError

router = APIRouter(prefix="/v1")
_STATUS = {"not_found": 404, "conflict": 409}


def _run(db: Session, fn):
    try:
        out = fn()
        db.commit()
        return out
    except WalletError as e:
        db.rollback()
        raise HTTPException(_STATUS.get(e.code, 422), {"code": e.code, "message": str(e)}) from e


def owner_for(p: Principal, owner_ref: str | None) -> str:
    """Klienti punon gjithmonë në llogarinë e vet; stafi duhet ta specifikojë."""
    if p.owner_ref:
        p.check_owner(owner_ref or p.owner_ref)
        return p.owner_ref
    if not owner_ref:
        raise HTTPException(422, {"code": "invalid", "message": "owner_ref is required"})
    return owner_ref


class ContactIn(BaseModel):
    owner_ref: str | None = None
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=254)
    first_name: str | None = Field(default=None, max_length=64)
    last_name: str | None = Field(default=None, max_length=64)
    external_id: str | None = Field(default=None, max_length=64)
    attributes: dict | None = None


class ContactPatch(BaseModel):
    first_name: str | None = Field(default=None, max_length=64)
    last_name: str | None = Field(default=None, max_length=64)
    attributes: dict | None = None


class ContactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    phone: str | None
    email: str | None
    first_name: str | None
    last_name: str | None
    external_id: str | None
    attributes: dict | None
    created_at: datetime


class ImportIn(BaseModel):
    owner_ref: str | None = None
    contacts: list[ContactIn] = Field(max_length=svc.MAX_IMPORT)


@router.post("/contacts", response_model=ContactOut, status_code=201)
def create_contact(
    body: ContactIn,
    response: Response,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("contacts:write")),
):
    owner = owner_for(p, body.owner_ref)
    fields = body.model_dump(exclude={"owner_ref"})
    c, created = _run(db, lambda: svc.upsert(db, owner, **fields))
    response.status_code = 201 if created else 200
    return c


@router.post("/contacts/import")
def import_contacts(
    body: ImportIn, db: Session = Depends(get_db), p: Principal = Depends(require("contacts:write"))
):
    owner = owner_for(p, body.owner_ref)
    rows = [c.model_dump(exclude={"owner_ref"}) for c in body.contacts]
    r = _run(db, lambda: svc.import_contacts(db, owner, rows))
    return {"created": r.created, "updated": r.updated, "errors": r.errors}


@router.get("/contacts", response_model=list[ContactOut])
def list_contacts(
    owner_ref: str | None = None,
    list_id: int | None = None,
    q: str | None = None,
    after_id: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("contacts:read")),
):
    owner = owner_for(p, owner_ref)
    stmt = select(Contact).where(
        Contact.owner_ref == owner, Contact.id > after_id, Contact.status == ContactStatus.ACTIVE
    )
    if list_id is not None:
        stmt = stmt.join(ListMember, ListMember.contact_id == Contact.id).where(
            ListMember.list_id == list_id
        )
    if q:  # kërkim nënvarg në emër, telefon, email (pa wildcards nga përdoruesi)
        esc = q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pat = f"%{esc}%"
        stmt = stmt.where(
            func.lower(func.coalesce(Contact.first_name, "")).like(pat, escape="\\")
            | func.lower(func.coalesce(Contact.last_name, "")).like(pat, escape="\\")
            | func.coalesce(Contact.phone, "").like(f"%{esc.lstrip('+')}%", escape="\\")
            | func.coalesce(Contact.email, "").like(pat, escape="\\")
        )
    return db.scalars(stmt.order_by(Contact.id).limit(max(1, min(limit, 500)))).all()


@router.get("/contacts/{contact_id}", response_model=ContactOut)
def get_contact(
    contact_id: int,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("contacts:read")),
):
    owner = owner_for(p, owner_ref)
    return _run(db, lambda: svc._get(db, owner, contact_id))


@router.patch("/contacts/{contact_id}", response_model=ContactOut)
def patch_contact(
    contact_id: int,
    body: ContactPatch,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("contacts:write")),
):
    owner = owner_for(p, owner_ref)
    fields = body.model_dump(exclude_unset=True)
    return _run(db, lambda: svc.update(db, owner, contact_id, **fields))


@router.delete("/contacts/{contact_id}", status_code=204)
def erase_contact(
    contact_id: int,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("contacts:write")),
):
    """Fshirje GDPR: PII hiqet; adresat mbeten të bllokuara vetëm si HMAC."""
    owner = owner_for(p, owner_ref)

    def go():
        c = svc.erase(db, owner, contact_id, p.actor)
        audit(db, p, "contact.erase", "contact", c.id, {"owner": owner})

    _run(db, go)
    return Response(status_code=204)


# --- Lista --------------------------------------------------------------------


class ListIn(BaseModel):
    owner_ref: str | None = None
    name: str = Field(min_length=1, max_length=64)


class MembersIn(BaseModel):
    owner_ref: str | None = None
    contact_ids: list[int] = Field(min_length=1, max_length=svc.MAX_IMPORT)


def _list_out(lst: ContactList) -> dict:
    return {"id": lst.id, "name": lst.name}


@router.post("/lists", status_code=201)
def create_list(
    body: ListIn, db: Session = Depends(get_db), p: Principal = Depends(require("contacts:write"))
):
    owner = owner_for(p, body.owner_ref)
    return _list_out(_run(db, lambda: svc.create_list(db, owner, body.name)))


@router.get("/lists")
def get_lists(
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("contacts:read")),
):
    owner = owner_for(p, owner_ref)
    rows = db.scalars(
        select(ContactList).where(ContactList.owner_ref == owner).order_by(ContactList.id)
    )
    return [_list_out(x) for x in rows]


@router.post("/lists/{list_id}/members")
def add_members(
    list_id: int,
    body: MembersIn,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("contacts:write")),
):
    owner = owner_for(p, body.owner_ref)
    return {"added": _run(db, lambda: svc.add_members(db, owner, list_id, body.contact_ids))}


@router.delete("/lists/{list_id}/members/{contact_id}", status_code=204)
def remove_member(
    list_id: int,
    contact_id: int,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("contacts:write")),
):
    owner = owner_for(p, owner_ref)
    _run(db, lambda: svc.remove_member(db, owner, list_id, contact_id))
    return Response(status_code=204)


@router.get("/lists/{list_id}/audience")
def audience(
    list_id: int,
    channel: str,
    category: str = "marketing",
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("contacts:read")),
):
    """Sa nga lista mund të kontaktohet dhe pse të tjerët jo (para se të nisë një campaign)."""
    owner = owner_for(p, owner_ref)
    if channel not in consent.CHANNELS or category not in consent.CATEGORIES:
        raise HTTPException(422, {"code": "invalid", "message": "bad channel or category"})
    counts = _run(db, lambda: svc.audience_counts(db, owner, list_id, channel, category))
    return {
        "eligible": counts.get("ok", 0),
        "excluded": {k: v for k, v in counts.items() if k != "ok"},
    }


# --- Consent ------------------------------------------------------------------


class ConsentIn(BaseModel):
    owner_ref: str | None = None
    channel: str
    address: str = Field(max_length=254)
    action: str = Field(pattern="^(opt_in|opt_out)$")
    reason: str = Field(default="unsubscribe", max_length=24)
    source: str = Field(min_length=1, max_length=48)
    evidence: str | None = Field(default=None, max_length=2000)


@router.post("/consent", status_code=201)
def record_consent(
    body: ConsentIn, db: Session = Depends(get_db), p: Principal = Depends(require("consent:write"))
):
    owner = owner_for(p, body.owner_ref)

    def go():
        st = consent.record(
            db, owner, body.channel, body.address, body.action, body.reason,
            body.source, p.actor, body.evidence,
        )  # fmt: skip
        audit(db, p, f"consent.{body.action}", "consent", st.id,
              {"owner": owner, "channel": body.channel, "reason": st.reason})  # fmt: skip
        return st

    st = _run(db, go)
    return {"channel": body.channel, "opted_in": st.opted_in, "hard": st.hard, "reason": st.reason}


@router.get("/consent/check")
def check_consent(
    channel: str,
    address: str,
    category: str = "marketing",
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("contacts:read")),
):
    owner = owner_for(p, owner_ref)
    d = _run(db, lambda: consent.check(db, owner, channel, address, category))
    return {"allowed": d.allowed, "reason": d.reason}
