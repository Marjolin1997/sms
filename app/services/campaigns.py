"""Motori i campaigns. Parimet:

- Audienca materializohet një herë (snapshot) në pjesa, pastaj dërgimi lexon nga ai snapshot.
- Consent-i rikontrollohet në çastin e dërgimit (një STOP pas planifikimit respektohet).
- Çdo marrës dërgohet me idempotency key `camp:<id>:<contact>`: një crash/retry nuk dërgon
  dy herë dhe nuk faturon dy herë.
- Kufij: ritëm për minutë, buxhet maksimal, dritare orare, kill switch global.
- Mbarimi i parave ose buxheti e pauzon campaign-in (nuk dështon marrës pas marrësi).
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, object_session

from app.core.context import worker_owner
from app.core.scope import Owner, owned, ref
from app.core.timeutil import as_utc
from app.models.campaigns import (
    ACTIVE,
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    RecipientStatus,
)
from app.models.contacts import Contact
from app.models.email import Email
from app.models.messaging import ApprovalStatus, SenderId
from app.models.sending import AccountPlan, Message, MessageStatus
from app.services import (
    consent,
    contacts,
    email_domains,
    emails,
    events,
    rates,
    switches,
    templates,
)
from app.services import messages as msg
from app.services import wallet as wallets
from app.services.wallet import Conflict, NotFound, WalletError

log = logging.getLogger("sms.campaigns")

PREP_BATCH = 500
SEND_BATCH = 100
ESTIMATE_CAP = 50_000
CONTACT_FIELDS = {"first_name", "last_name", "phone", "email"}


class InvalidCampaign(WalletError):
    code = "invalid_campaign"


def _get(db: Session, owner: Owner, campaign_id: int, lock: bool = False) -> Campaign:
    q = select(Campaign).where(Campaign.id == campaign_id, owned(Campaign, owner))
    c = db.scalar(q.with_for_update() if lock else q)
    if c is None:
        raise NotFound("campaign not found")
    return c


def create(
    db: Session,
    owner: Owner,
    name: str,
    list_id: int,
    sender: str,
    created_by: str,
    text: str | None = None,
    template_id: int | None = None,
    category: str = "marketing",
    max_cost=None,
    rate_per_minute: int = 300,
    window_start_hour: int | None = None,
    window_end_hour: int | None = None,
    utc_offset_minutes: int = 0,
    channel: str = "sms",
    subject: str | None = None,
    html: str | None = None,
    from_email: str | None = None,
    from_name: str | None = None,
) -> Campaign:
    if channel not in ("sms", "email"):
        raise InvalidCampaign("channel must be 'sms' or 'email'")
    if channel == "sms" and not sender:
        raise InvalidCampaign("sender is required for SMS campaigns")
    if channel == "email":
        if template_id or not text or not subject or not from_email:
            raise InvalidCampaign("email campaigns need from_email, subject and text (no template)")
        if len(subject) > 200 or len(text) > 100_000 or (html and len(html) > 200_000):
            raise InvalidCampaign("subject/text/html too long")
        try:
            from_email = consent.normalize("email", from_email)
        except consent.InvalidAddress as ex:
            raise InvalidCampaign(str(ex)) from ex
        if max_cost is not None:
            raise InvalidCampaign("max_cost applies to SMS only (email is not charged per message)")
        sender = "email"
    elif subject or html or from_email or from_name:
        raise InvalidCampaign("subject/html/from_email are for email campaigns only")
    if bool(text) == bool(template_id):
        raise InvalidCampaign("provide exactly one of text or template_id")
    if category not in consent.CATEGORIES:
        raise InvalidCampaign("bad category")
    if not 1 <= rate_per_minute <= 10_000:
        raise InvalidCampaign("rate_per_minute must be 1..10000")
    if (window_start_hour is None) != (window_end_hour is None):
        raise InvalidCampaign("window needs both start and end hour")
    for h in (window_start_hour, window_end_hour):
        if h is not None and not 0 <= h <= 23:
            raise InvalidCampaign("window hours must be 0..23")
    if window_start_hour is not None and window_start_hour == window_end_hour:
        raise InvalidCampaign("window start and end must differ")
    if not -840 <= utc_offset_minutes <= 840:
        raise InvalidCampaign("utc_offset_minutes out of range")
    if text and channel == "sms":
        if len(text) > 1600:
            raise InvalidCampaign("text too long")
        try:
            from app.services.sms_text import count_segments

            count_segments(text)
        except ValueError as e:
            raise InvalidCampaign(str(e)) from e
    contacts._list(db, owner, list_id)
    if template_id:
        version = templates.usable_version(db, owner, template_id)
        templates.variables(version.body)
    if max_cost is not None:
        max_cost = wallets.positive(max_cost)
    if db.scalar(select(Campaign).where(owned(Campaign, owner), Campaign.name == name)):
        raise Conflict("campaign name already exists")
    c = Campaign(
        owner_ref=ref(owner), name=name, list_id=list_id, category=category, sender=sender,
        channel=channel, subject=subject, html_body=html, from_email=from_email,
        from_name=from_name, text=text, template_id=template_id, max_cost=max_cost,
        rate_per_minute=rate_per_minute,
        window_start_hour=window_start_hour, window_end_hour=window_end_hour,
        utc_offset_minutes=utc_offset_minutes, created_by=created_by,
    )  # fmt: skip
    db.add(c)
    db.flush()
    return c


def _touch(c: Campaign, status: CampaignStatus | None = None, now: datetime | None = None):
    if status is not None:
        c.status = status
    c.updated_at = now or datetime.now(UTC)
    if status in (CampaignStatus.RUNNING, CampaignStatus.PAUSED, CampaignStatus.COMPLETED,
                  CampaignStatus.CANCELLED):  # fmt: skip
        db = object_session(c)
        if db is not None:
            events.emit(db, worker_owner(db, c), f"campaign.{status.value}", "campaign", c.id,
                        {"campaign_id": c.id, "name": c.name, "status": status.value,
                         "pause_reason": c.pause_reason})  # fmt: skip


def schedule(
    db: Session,
    owner: Owner,
    campaign_id: int,
    when: datetime | None,
    now: datetime | None = None,
) -> Campaign:
    c = _get(db, owner, campaign_id, lock=True)
    if c.status != CampaignStatus.DRAFT:
        raise Conflict(f"cannot schedule from status {c.status.value}")
    if c.channel == "email":
        if email_domains.verified_domain_for(db, owner, c.from_email) is None:
            raise InvalidCampaign("from_email is not on a verified domain of this account")
    else:
        approved = db.scalar(
            select(func.count())
            .select_from(SenderId)
            .where(
                owned(SenderId, owner),
                SenderId.value
                == (c.sender.lstrip("+") if c.sender.lstrip("+").isdigit() else c.sender),
                SenderId.status == ApprovalStatus.APPROVED,
            )
        )
        if not approved:
            raise InvalidCampaign("sender id is not approved for this account")
    now = as_utc(now or datetime.now(UTC))
    when = as_utc(when) if when else now
    if when < now - timedelta(minutes=5):
        raise InvalidCampaign("scheduled_at is in the past")
    c.scheduled_at = when
    _touch(c, CampaignStatus.SCHEDULED, now)
    db.flush()
    return c


def pause(db: Session, owner: Owner, campaign_id: int, reason: str = "manual") -> Campaign:
    c = _get(db, owner, campaign_id, lock=True)
    if c.status != CampaignStatus.RUNNING:
        raise Conflict(f"cannot pause from status {c.status.value}")
    c.pause_reason = reason
    _touch(c, CampaignStatus.PAUSED)
    db.flush()
    return c


def resume(db: Session, owner: Owner, campaign_id: int) -> Campaign:
    c = _get(db, owner, campaign_id, lock=True)
    if c.status != CampaignStatus.PAUSED:
        raise Conflict(f"cannot resume from status {c.status.value}")
    c.pause_reason = None
    _touch(c, CampaignStatus.RUNNING)
    db.flush()
    return c


def cancel(db: Session, owner: Owner, campaign_id: int) -> Campaign:
    """Ndalon marrësit e pa-dërguar dhe anulon mesazhet që s'i ka marrë ende worker-i."""
    c = _get(db, owner, campaign_id, lock=True)
    if c.status in (CampaignStatus.COMPLETED, CampaignStatus.CANCELLED):
        raise Conflict(f"campaign already {c.status.value}")
    db.execute(
        update(CampaignRecipient)
        .where(
            CampaignRecipient.campaign_id == c.id,
            CampaignRecipient.status == RecipientStatus.PENDING,
        )
        .values(status=RecipientStatus.CANCELLED, reason="campaign_cancelled")
    )
    ids = db.scalars(
        select(CampaignRecipient.message_id).where(
            CampaignRecipient.campaign_id == c.id,
            CampaignRecipient.status == RecipientStatus.QUEUED,
            CampaignRecipient.message_id.is_not(None),
        )
    ).all()
    for mid in ids:
        msg.cancel_if_queued(db, mid)
    for eid in db.scalars(
        select(CampaignRecipient.email_id).where(
            CampaignRecipient.campaign_id == c.id,
            CampaignRecipient.status == RecipientStatus.QUEUED,
            CampaignRecipient.email_id.is_not(None),
        )
    ).all():
        emails.cancel_if_queued(db, eid)
    _touch(c, CampaignStatus.CANCELLED)
    c.completed_at = datetime.now(UTC)
    db.flush()
    return c


# --- Personalizimi ------------------------------------------------------------


def _values(contact: Contact, needed: list[str]) -> dict[str, str]:
    out = {}
    for name in needed:
        v = (
            getattr(contact, name)
            if name in CONTACT_FIELDS
            else (contact.attributes or {}).get(name)
        )
        if v is None or str(v) == "":
            raise InvalidCampaign(f"missing_variable:{name}")
        out[name] = str(v)
    return out


def _needed_vars(db: Session, c: Campaign) -> list[str]:
    if c.channel == "email":
        found: dict[str, None] = {}
        for part in (c.subject, c.text, c.html_body):
            for name in templates.VAR.findall(part or ""):
                found[name] = None
        return list(found)
    if not c.template_id:  # SMS me tekst të lirë: variabla {{emri}} direkt në tekst
        return list(dict.fromkeys(templates.VAR.findall(c.text or "")))
    return templates.variables(
        templates.usable_version(db, worker_owner(db, c), c.template_id).body
    )


# --- Përgatitja e audiencës -----------------------------------------------------


def _prepare_step(db: Session, c: Campaign, now: datetime) -> None:
    rows = contacts.audience_batch(
        db,
        worker_owner(db, c),
        c.list_id,
        c.channel,
        c.category,
        after_id=c.prep_cursor,
        limit=PREP_BATCH,
    )
    if not rows:
        _touch(c, CampaignStatus.RUNNING, now)
        return
    for r in rows:
        db.add(
            CampaignRecipient(
                campaign_id=c.id, contact_id=r.contact_id,
                address=r.address if r.allowed else None,
                status=RecipientStatus.PENDING if r.allowed else RecipientStatus.SKIPPED,
                reason=None if r.allowed else r.reason,
            )
        )  # fmt: skip
    c.prep_cursor = rows[-1].contact_id
    _touch(c, now=now)


# --- Dërgimi --------------------------------------------------------------------


def _in_window(c: Campaign, now: datetime) -> bool:
    if c.window_start_hour is None:
        return True
    hour = (now + timedelta(minutes=c.utc_offset_minutes)).hour
    s, e = c.window_start_hour, c.window_end_hour
    return s <= hour < e if s < e else (hour >= s or hour < e)


def _reserved_cost(db: Session, campaign_id: int) -> Decimal:
    total = db.scalar(
        select(func.coalesce(func.sum(Message.total_price), 0))
        .join(CampaignRecipient, CampaignRecipient.message_id == Message.id)
        .where(CampaignRecipient.campaign_id == campaign_id, Message.status != MessageStatus.FAILED)
    )
    return Decimal(total)


def _skip(r: CampaignRecipient, reason: str) -> None:
    r.status, r.reason = RecipientStatus.SKIPPED, reason[:48]


def _subst(template: str, values: dict[str, str], escape: bool = False) -> str:
    """Zëvendësim në një kalim (vlerat nuk rizgjerohen); HTML-escape për trupin html."""
    import html as html_lib

    def rep(m):
        v = values[m.group(1)]
        return html_lib.escape(v) if escape else v

    return templates.VAR.sub(rep, template)


def _submit_sms(db, c, r, values, plan, reserved, now):
    """→ kostoja e mesazhit ose ngre përjashtim; kontrollon buxhetin para dërgimit."""
    text = _subst(c.text, values) if c.text else None  # tekst i lirë: personalizim {{emri}}
    if c.max_cost is not None and plan is not None:
        quote_text = text or templates.render(db, worker_owner(db, c), c.template_id, values).text
        q = rates.quote(db, plan.rate_card_id, r.address, quote_text, now)
        if reserved + q.total > c.max_cost:
            raise _BudgetExhausted
    with db.begin_nested():
        m = msg.submit(
            db, worker_owner(db, c), f"camp:{c.id}:{r.contact_id}", r.address, c.sender,
            text=text, template_id=c.template_id,
            values=(values or None) if c.template_id else None,
            category=c.category, now=now,
        )  # fmt: skip
    r.status, r.message_id, r.queued_at = RecipientStatus.QUEUED, m.id, now
    return m.total_price


def _submit_email(db, c, r, values, now):
    with db.begin_nested():
        e = emails.submit(
            db, worker_owner(db, c), f"camp:{c.id}:{r.contact_id}", c.from_email, r.address,
            _subst(c.subject, values), _subst(c.text, values),
            _subst(c.html_body, values, escape=True) if c.html_body else None,
            c.from_name, c.category, now=now,
        )  # fmt: skip
    r.status, r.email_id, r.queued_at = RecipientStatus.QUEUED, e.id, now
    return Decimal(0)


class _BudgetExhausted(Exception):
    pass


def _dispatch_step(db: Session, c: Campaign, now: datetime) -> None:
    if not switches.is_enabled(db, switches.SUBMIT) or not _in_window(c, now):
        return
    recent = db.scalar(
        select(func.count())
        .select_from(CampaignRecipient)
        .where(
            CampaignRecipient.campaign_id == c.id,
            CampaignRecipient.queued_at > now - timedelta(minutes=1),
        )
    )
    n = min(SEND_BATCH, c.rate_per_minute - recent)
    if n <= 0:
        return
    pending = db.scalars(
        select(CampaignRecipient)
        .where(
            CampaignRecipient.campaign_id == c.id,
            CampaignRecipient.status == RecipientStatus.PENDING,
        )
        .order_by(CampaignRecipient.id)
        .limit(n)
    ).all()
    if not pending:
        _touch(c, CampaignStatus.COMPLETED, now)
        c.completed_at = now
        return
    needed = _needed_vars(db, c)
    plan = db.scalar(select(AccountPlan).where(owned(AccountPlan, worker_owner(db, c))))
    reserved = _reserved_cost(db, c.id) if c.max_cost is not None else Decimal(0)
    for r in pending:
        contact = db.get(Contact, r.contact_id)
        if contact is None or r.address is None:
            _skip(r, "contact_erased")
            continue
        try:
            values = _values(contact, needed)
            if c.channel == "email":
                _submit_email(db, c, r, values, now)
            else:
                reserved += _submit_sms(db, c, r, values, plan, reserved, now)
        except _BudgetExhausted:
            c.pause_reason = "budget_exhausted"
            _touch(c, CampaignStatus.PAUSED, now)
            return
        except (wallets.InsufficientFunds, msg.AccountDisabled) as e:
            c.pause_reason = e.code
            _touch(c, CampaignStatus.PAUSED, now)
            return
        except (msg.RateLimited, msg.SendingPaused):
            return  # provo sërish në ciklin tjetër; marrësi mbetet PENDING
        except WalletError as e:  # consent, route, sender, domen, tarifë, variabël e munguar...
            _skip(r, e.code if not str(e).startswith("missing_variable") else str(e))
    _touch(c, now=now)


def run_due(db: Session, now: datetime | None = None) -> int:
    """Thirret nga worker-i. Çdo campaign në transaksionin e vet (SKIP LOCKED)."""
    now = as_utc(now or datetime.now(UTC))
    ids = db.scalars(
        select(Campaign.id).where(Campaign.status.in_(ACTIVE)).order_by(Campaign.id).limit(50)
    ).all()
    db.rollback()  # mbyll transaksionin e leximit
    done = 0
    for cid in ids:
        try:
            c = db.scalar(
                select(Campaign).where(Campaign.id == cid).with_for_update(skip_locked=True)
            )
            if c is None or c.status not in ACTIVE:
                db.rollback()
                continue
            if c.status == CampaignStatus.SCHEDULED:
                if c.scheduled_at is not None and as_utc(c.scheduled_at) <= now:
                    c.started_at = now
                    _touch(c, CampaignStatus.PREPARING, now)
            elif c.status == CampaignStatus.PREPARING:
                _prepare_step(db, c, now)
            elif c.status == CampaignStatus.RUNNING:
                _dispatch_step(db, c, now)
            db.commit()
            done += 1
        except Exception:
            db.rollback()
            log.exception("campaign %s step failed", cid)
    return done


# --- Statistika dhe vlerësimi ---------------------------------------------------


def stats(db: Session, c: Campaign) -> dict:
    rec = dict(
        db.execute(
            select(CampaignRecipient.status, func.count())
            .where(CampaignRecipient.campaign_id == c.id)
            .group_by(CampaignRecipient.status)
        ).all()
    )
    skipped = dict(
        db.execute(
            select(CampaignRecipient.reason, func.count())
            .where(
                CampaignRecipient.campaign_id == c.id,
                CampaignRecipient.status.in_([RecipientStatus.SKIPPED, RecipientStatus.CANCELLED]),
            )
            .group_by(CampaignRecipient.reason)
        ).all()
    )
    by_msg: dict[str, int] = {}
    cost = {"delivered": Decimal(0), "in_flight": Decimal(0), "refunded": Decimal(0)}
    if c.channel == "email":
        q = (
            select(Email.status, func.count())
            .join(CampaignRecipient, CampaignRecipient.email_id == Email.id)
            .where(CampaignRecipient.campaign_id == c.id)
            .group_by(Email.status)
        )
        for st, n in db.execute(q):
            by_msg[st.value] = n
        good = by_msg.get("delivered", 0) + by_msg.get("complained", 0)  # complaint = u dorëzua
        bad = by_msg.get("bounced", 0) + by_msg.get("failed", 0)
        cost = {k: "0" for k in cost}
    else:
        q = (
            select(Message.status, func.count(), func.coalesce(func.sum(Message.total_price), 0))
            .join(CampaignRecipient, CampaignRecipient.message_id == Message.id)
            .where(CampaignRecipient.campaign_id == c.id)
            .group_by(Message.status)
        )
        for st, n, total in db.execute(q):
            by_msg[st.value] = n
            key = {"delivered": "delivered", "failed": "refunded"}.get(st.value, "in_flight")
            cost[key] += Decimal(total)
        good, bad = by_msg.get("delivered", 0), by_msg.get("failed", 0)
        cost = {k: str(v) for k, v in cost.items()}
    finished = good + bad
    return {
        "status": c.status.value,
        "pause_reason": c.pause_reason,
        "recipients": {k.value: v for k, v in rec.items()},
        "skipped_reasons": {k or "": v for k, v in skipped.items()},
        "channel": c.channel,
        "messages": by_msg,
        "delivery_rate": round(good / finished, 4) if finished else None,
        "cost": cost,
    }


@dataclass
class Estimate:
    recipients: int
    excluded: int
    segments: int
    total: Decimal
    currency: str | None


def estimate(db: Session, owner: Owner, campaign_id: int, now: datetime | None = None) -> Estimate:
    """Vlerësim i saktë i kostos (pa shkruar asgjë): kuotë për secilin marrës të lejuar."""
    c = _get(db, owner, campaign_id)
    if c.channel == "email":  # pa kosto për mesazh: numërojmë vetëm audiencën e lejuar
        counts = contacts.audience_counts(db, owner, c.list_id, "email", c.category)
        ok = counts.get("ok", 0)
        return Estimate(ok, sum(counts.values()) - ok, 0, Decimal(0), None)
    plan = db.scalar(select(AccountPlan).where(owned(AccountPlan, owner)))
    if plan is None:
        raise msg.AccountDisabled("account has no sending plan")
    now = as_utc(now or datetime.now(UTC))
    needed = _needed_vars(db, c)
    est = Estimate(0, 0, 0, Decimal(0), None)
    after, seen = 0, 0
    while True:
        rows = contacts.audience_batch(db, owner, c.list_id, "sms", c.category, after, 500)
        if not rows:
            return est
        seen += len(rows)
        if seen > ESTIMATE_CAP:
            raise InvalidCampaign(f"list too large to estimate (> {ESTIMATE_CAP})")
        after = rows[-1].contact_id
        for r in rows:
            if not r.allowed:
                est.excluded += 1
                continue
            try:
                contact = db.get(Contact, r.contact_id)
                values = _values(contact, needed)
                text = (
                    _subst(c.text, values)
                    if c.text
                    else templates.render(db, owner, c.template_id, values).text
                )
                q = rates.quote(db, plan.rate_card_id, r.address, text, now)
            except WalletError:
                est.excluded += 1
                continue
            est.recipients += 1
            est.segments += q.segments
            est.total += q.total
            est.currency = q.currency
