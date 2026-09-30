"""Endpoint-e leximit për konsolën: historik, radhë miratimi, tarifa, llogari, nisja e klientit."""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.contacts import owner_for
from app.core.db import get_db
from app.core.security import Principal, require, require_any
from app.models.billing import BillingProfile, Subscription
from app.models.contacts import Contact, ContactStatus
from app.models.email import DomainStatus, Email, EmailDomain, EmailStatus
from app.models.events import EndpointStatus, WebhookEndpoint
from app.models.messaging import ApprovalStatus, SenderId, Template, TemplateVersion
from app.models.rates import Rate, RateCard, RateCardVersion
from app.models.sending import AccountPlan, Message, MessageStatus
from app.models.wallet import EntryType, LedgerEntry, Topup, TopupStatus, Wallet
from app.services import messages as msg_svc
from app.services import rates as rates_svc
from app.services import templates as tpl
from app.services import wallet as wallets
from app.services.audit import audit
from app.services.wallet import WalletError

router = APIRouter(prefix="/v1")
_STATUS = {"not_found": 404, "no_rate": 422, "no_route": 422, "account_disabled": 403}


def _page(limit: int) -> int:
    return max(1, min(limit, 200))


def _like(text: str) -> str:
    """Kërkim nënvarg pa lejuar përdoruesin të fusë wildcards (% _)."""
    esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _owner_or_all(p: Principal, owner_ref: str | None, perm: str) -> str | None:
    """Klienti: llogaria e vet. Stafi: llogaria e zgjedhur, ose (me leje) të gjitha."""
    if p.owner_ref:
        p.check_owner(owner_ref or p.owner_ref)
        return p.owner_ref
    if owner_ref:
        return owner_ref
    if not p.has(perm):
        raise HTTPException(403, {"code": "forbidden", "message": f"missing {perm}"})
    return None


# --- Historik mesazhesh --------------------------------------------------------------


@router.get("/messages")
def list_messages(
    status: MessageStatus | None = None,
    q: str | None = None,
    before_id: int | None = None,
    limit: int = 50,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("messages:read")),
):
    """Më të rejat së pari; `next_before_id` për faqen tjetër."""
    owner = owner_for(p, owner_ref)
    lim = _page(limit)
    stmt = select(Message).where(Message.owner_ref == owner)
    if status:
        stmt = stmt.where(Message.status == status)
    if q:
        stmt = stmt.where(Message.destination.like(_like(q.lstrip("+")), escape="\\"))
    if before_id:
        stmt = stmt.where(Message.id < before_id)
    rows = db.scalars(stmt.order_by(Message.id.desc()).limit(lim + 1)).all()
    more = len(rows) > lim
    rows = rows[:lim]
    return {
        "items": [
            {"id": m.public_id, "to": m.destination, "sender": m.sender, "status": m.status.value,
             "category": m.category, "segments": m.segments, "total_price": str(m.total_price),
             "currency": m.currency, "error_code": m.error_code, "created_at": m.created_at}
            for m in rows
        ],
        "next_before_id": rows[-1].id if more else None,
    }  # fmt: skip


@router.get("/email/messages")
def list_emails(
    status: EmailStatus | None = None,
    q: str | None = None,
    before_id: int | None = None,
    limit: int = 50,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("email:read")),
):
    owner = owner_for(p, owner_ref)
    lim = _page(limit)
    stmt = select(Email).where(Email.owner_ref == owner)
    if status:
        stmt = stmt.where(Email.status == status)
    if q:
        stmt = stmt.where(
            Email.to_email.like(_like(q.lower()), escape="\\")
            | Email.subject.ilike(_like(q), escape="\\")
        )
    if before_id:
        stmt = stmt.where(Email.id < before_id)
    rows = db.scalars(stmt.order_by(Email.id.desc()).limit(lim + 1)).all()
    more = len(rows) > lim
    rows = rows[:lim]
    return {
        "items": [
            {"id": e.public_id, "to": e.to_email, "from": e.from_email, "subject": e.subject,
             "status": e.status.value, "category": e.category, "error_code": e.error_code,
             "created_at": e.created_at}
            for e in rows
        ],
        "next_before_id": rows[-1].id if more else None,
    }  # fmt: skip


# --- Çmim live para dërgimit ------------------------------------------------------------


class QuoteIn(BaseModel):
    owner_ref: str | None = None
    to: str = Field(max_length=20)
    text: str = Field(min_length=1, max_length=1600)


@router.post("/messages/quote")
def quote_message(
    body: QuoteIn, db: Session = Depends(get_db), p: Principal = Depends(require("messages:send"))
):
    """Sa kushton ky mesazh dhe në sa segmente ndahet, para se ta dërgosh."""
    owner = owner_for(p, body.owner_ref)
    try:
        plan = db.scalar(select(AccountPlan).where(AccountPlan.owner_ref == owner))
        if plan is None or not plan.enabled:
            raise msg_svc.AccountDisabled("your account cannot send yet; contact support")
        if not rates_svc.E164.match(body.to):
            raise rates_svc.InvalidNumber(
                "enter the number in international format, e.g. +355691234567"
            )
        dest = body.to.lstrip("+")
        route = msg_svc.find_route(db, dest)
        q = rates_svc.quote(db, plan.rate_card_id, dest, body.text)
    except WalletError as e:
        raise HTTPException(_STATUS.get(e.code, 422), {"code": e.code, "message": str(e)}) from e
    return {
        "country": route.country, "encoding": q.encoding, "segments": q.segments,
        "unit_price": str(q.unit_price), "total": str(q.total), "currency": q.currency,
    }  # fmt: skip


# --- Sender ID dhe template: listë + radhë miratimi ------------------------------------


@router.get("/sender-ids")
def list_sender_ids(
    status: ApprovalStatus | None = None,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require_any("sender:request", "sender:review")),
):
    owner = _owner_or_all(p, owner_ref, "sender:review")
    stmt = select(SenderId)
    if owner:
        stmt = stmt.where(SenderId.owner_ref == owner)
    if status:
        stmt = stmt.where(SenderId.status == status)
    rows = db.scalars(stmt.order_by(SenderId.id.desc()).limit(300))
    return [
        {"id": s.id, "owner_ref": s.owner_ref, "country": s.country, "value": s.value,
         "kind": s.kind.value, "status": s.status.value, "reason": s.reason,
         "created_at": s.created_at, "reviewed_by": s.reviewed_by}
        for s in rows
    ]  # fmt: skip


def _version_out(v: TemplateVersion) -> dict:
    return {"id": v.id, "version": v.version, "status": v.status.value, "body": v.body,
            "variables": tpl.variables(v.body), "reason": v.reason}  # fmt: skip


@router.get("/templates")
def list_templates(
    status: ApprovalStatus | None = None,
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require_any("template:write", "template:review")),
):
    """Klienti: template-t e veta me versionet. Stafi pa llogari: vetëm versionet që kërkojnë
    shqyrtim (ose sipas `status`)."""
    owner = _owner_or_all(p, owner_ref, "template:review")
    stmt = select(Template)
    if owner:
        stmt = stmt.where(Template.owner_ref == owner)
    templates = db.scalars(stmt.order_by(Template.id.desc()).limit(300)).all()
    out = []
    for t in templates:
        vq = select(TemplateVersion).where(TemplateVersion.template_id == t.id)
        if status:
            vq = vq.where(TemplateVersion.status == status)
        versions = db.scalars(vq.order_by(TemplateVersion.version.desc())).all()
        if status and not versions:
            continue
        out.append({"id": t.id, "owner_ref": t.owner_ref, "name": t.name,
                    "versions": [_version_out(v) for v in versions]})  # fmt: skip
    return out


# --- Tarifa ---------------------------------------------------------------------------


@router.get("/rate-cards")
def list_rate_cards(db: Session = Depends(get_db), _: Principal = Depends(require("rates:read"))):
    out = []
    for c in db.scalars(select(RateCard).order_by(RateCard.id)):
        versions = db.scalars(
            select(RateCardVersion)
            .where(RateCardVersion.rate_card_id == c.id)
            .order_by(RateCardVersion.version.desc())
        ).all()
        counts = dict(
            db.execute(
                select(Rate.version_id, func.count())
                .where(Rate.version_id.in_([v.id for v in versions] or [0]))
                .group_by(Rate.version_id)
            ).all()
        )
        out.append({
            "id": c.id, "name": c.name, "currency": c.currency,
            "versions": [{"id": v.id, "version": v.version, "status": v.status.value,
                          "effective_from": v.effective_from, "rates": counts.get(v.id, 0)}
                         for v in versions],
        })  # fmt: skip
    return out


@router.get("/rate-card-versions/{version_id}/rates")
def list_version_rates(
    version_id: int, db: Session = Depends(get_db), _: Principal = Depends(require("rates:read"))
):
    if db.get(RateCardVersion, version_id) is None:
        raise HTTPException(404, {"code": "not_found", "message": "version not found"})
    rows = db.scalars(
        select(Rate).where(Rate.version_id == version_id).order_by(Rate.prefix, Rate.operator)
    )
    return [{"id": r.id, "prefix": r.prefix, "operator": r.operator,
             "price_per_segment": str(r.price_per_segment)} for r in rows]  # fmt: skip


# --- Wallet ---------------------------------------------------------------------------


@router.get("/wallets")
def list_wallets(
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("wallet:read")),
):
    owner = owner_for(p, owner_ref)
    out = []
    for w in db.scalars(select(Wallet).where(Wallet.owner_ref == owner).order_by(Wallet.id)):
        avail, held = wallets.balances(db, w.id)
        out.append({
            "id": w.id, "currency": w.currency, "available": str(avail), "held": str(held),
            "low_balance_threshold": str(w.low_balance_threshold)
            if w.low_balance_threshold is not None else None,
            "low_balance": bool(w.low_balance_notified),
        })  # fmt: skip
    return out


def _topup_out(t: Topup, owner: str | None = None) -> dict:
    return {"id": t.id, "wallet_id": t.wallet_id, "amount": str(t.amount), "method": t.method.value,
            "status": t.status.value, "external_ref": t.external_ref, "created_by": t.created_by,
            "created_at": t.created_at, "confirmed_at": t.confirmed_at,
            "owner_ref": owner}  # fmt: skip


@router.get("/wallets/{wallet_id}/topups")
def wallet_topups(
    wallet_id: int, db: Session = Depends(get_db), p: Principal = Depends(require("wallet:read"))
):
    w = db.get(Wallet, wallet_id)
    if w is None:
        raise HTTPException(404, {"code": "not_found", "message": "wallet not found"})
    p.check_owner(w.owner_ref)
    rows = db.scalars(
        select(Topup).where(Topup.wallet_id == w.id).order_by(Topup.id.desc()).limit(100)
    )
    return [_topup_out(t, w.owner_ref) for t in rows]


@router.get("/topups")
def pending_topups(
    status: TopupStatus = TopupStatus.PENDING,
    db: Session = Depends(get_db),
    _: Principal = Depends(require("topup:confirm")),
):
    """Radha e financës: top-up-et që presin konfirmim (cash / transfertë)."""
    rows = db.execute(
        select(Topup, Wallet.owner_ref, Wallet.currency)
        .join(Wallet, Wallet.id == Topup.wallet_id)
        .where(Topup.status == status)
        .order_by(Topup.id.desc())
        .limit(200)
    )
    return [{**_topup_out(t, owner), "currency": cur} for t, owner, cur in rows]


# --- Llogari (staf) -----------------------------------------------------------------------


@router.get("/admin/accounts")
def accounts(db: Session = Depends(get_db), _: Principal = Depends(require("monitor:read"))):
    """Lista e llogarive që stafi të zgjedhë në vend që ta shkruajë owner_ref."""
    owners: set[str] = set()
    for model in (Wallet, AccountPlan, Subscription):
        owners.update(db.scalars(select(model.owner_ref)))
    out = []
    for o in sorted(owners)[:300]:
        w = [{"id": x.id, "currency": x.currency,
              "available": str(wallets.balances(db, x.id)[0])}
             for x in db.scalars(select(Wallet).where(Wallet.owner_ref == o))]  # fmt: skip
        plan = db.scalar(select(AccountPlan).where(AccountPlan.owner_ref == o))
        sub = db.scalar(select(Subscription).where(Subscription.owner_ref == o))
        out.append({
            "owner_ref": o, "wallets": w, "sending_enabled": plan.enabled if plan else False,
            "has_rate_card": plan is not None, "subscription": sub.status.value if sub else None,
            "rate_card_id": plan.rate_card_id if plan else None,
            "rate_limit_per_min": plan.rate_limit_per_min if plan else None,
            "sms_last_24h": db.scalar(
                select(func.count()).select_from(Message).where(Message.owner_ref == o)
            ),
        })  # fmt: skip
    return out


class VatIn(BaseModel):
    vat_rate: Decimal = Field(ge=0, le=1, max_digits=6, decimal_places=4)


@router.put("/admin/billing/{owner}/vat")
def set_vat(
    owner: str, body: VatIn, db: Session = Depends(get_db),
    p: Principal = Depends(require("billing:admin")),
):  # fmt: skip
    prof = db.scalar(select(BillingProfile).where(BillingProfile.owner_ref == owner))
    if prof is None:
        raise HTTPException(
            422,
            {
                "code": "invalid_billing",
                "message": "the customer has not filled in billing details yet",
            },
        )
    prof.vat_rate = body.vat_rate
    audit(db, p, "billing.vat", "billing_profile", owner, {"vat_rate": str(body.vat_rate)})
    db.commit()
    return {"vat_rate": str(prof.vat_rate)}


# --- Nisja e klientit (checklist) -----------------------------------------------------------


@router.get("/portal/onboarding")
def onboarding(
    owner_ref: str | None = None,
    db: Session = Depends(get_db),
    p: Principal = Depends(require("portal:read")),
):
    owner = owner_for(p, owner_ref)

    def has(stmt) -> bool:
        return bool(db.scalar(stmt))

    funded = has(
        select(func.count())
        .select_from(LedgerEntry)
        .join(Wallet, Wallet.id == LedgerEntry.wallet_id)
        .where(Wallet.owner_ref == owner, LedgerEntry.entry_type == EntryType.TOPUP)
    )  # fmt: skip
    sent_any = has(
        select(func.count()).select_from(Message).where(Message.owner_ref == owner)
    ) or has(select(func.count()).select_from(Email).where(Email.owner_ref == owner))
    steps = [
        {"id": "wallet", "title": "Add funds to your wallet", "done": funded, "link": "wallet",
         "hint": "SMS is prepaid. Top up online or ask us for a bank transfer."},
        {"id": "sender", "title": "Get a sender ID approved", "link": "compose", "done": has(
            select(func.count()).select_from(SenderId).where(
                SenderId.owner_ref == owner, SenderId.status == ApprovalStatus.APPROVED)),
         "hint": "The name recipients see, e.g. your brand. We review it before you can use it."},
        {"id": "contacts", "title": "Import your contacts", "link": "contacts", "done": has(
            select(func.count()).select_from(Contact).where(
                Contact.owner_ref == owner, Contact.status == ContactStatus.ACTIVE)),
         "hint": "Upload a list, or add people one by one."},
        {"id": "message", "title": "Send your first message", "link": "send", "done": sent_any,
         "hint": "Try a single SMS to yourself first."},
        {"id": "domain", "title": "Verify an email domain", "link": "email", "optional": True,
         "done": has(select(func.count()).select_from(EmailDomain).where(
             EmailDomain.owner_ref == owner, EmailDomain.status == DomainStatus.VERIFIED)),
         "hint": "Only needed if you send email."},
        {"id": "webhook", "title": "Connect a webhook", "link": "webhooks", "optional": True,
         "done": has(select(func.count()).select_from(WebhookEndpoint).where(
             WebhookEndpoint.owner_ref == owner, WebhookEndpoint.status == EndpointStatus.ACTIVE)),
         "hint": "Get delivery updates pushed to your own system."},
        {"id": "billing", "title": "Add your billing details", "link": "billing", "optional": True,
         "done": has(select(func.count()).select_from(BillingProfile).where(
             BillingProfile.owner_ref == owner)),
         "hint": "Needed for invoices."},
    ]  # fmt: skip
    required = [s for s in steps if not s.get("optional")]
    return {"steps": steps, "required_done": sum(s["done"] for s in required),
            "required_total": len(required)}  # fmt: skip
