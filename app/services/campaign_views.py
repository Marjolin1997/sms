"""Pamje vetëm-lexim për UI-n e campaign-eve (M11-F): agregate të faqes me një pyetje të grupuar (jo N+1).

Asnjë semantikë e re: numërues të papërpunuar nga serveri (statuset e marrësve dhe statuset PUBLIKE të mesazheve; UNKNOWN del `sending`).
"""

import json
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.admin import AuditLog
from app.models.campaigns import Campaign, CampaignRecipient
from app.models.contacts import Contact, ContactList, ListMember
from app.models.email import Email
from app.models.sending import Message


def _public(status) -> str:
    v = getattr(status, "value", status)
    return "sending" if v == "unknown" else v


def page_summaries(db: Session, campaigns: list[Campaign]) -> dict[int, dict]:
    """→ {campaign_id: {recipients_total, recipient_counts, message_counts}} me ≤ 3 pyetje për gjithë faqen."""
    ids = [c.id for c in campaigns]
    out = {i: {"recipient_counts": {}, "message_counts": {}} for i in ids}
    if not ids:
        return {}
    for cid, st, n in db.execute(
        select(CampaignRecipient.campaign_id, CampaignRecipient.status, func.count())
        .where(CampaignRecipient.campaign_id.in_(ids))
        .group_by(CampaignRecipient.campaign_id, CampaignRecipient.status)
    ):
        out[cid]["recipient_counts"][st.value] = n
    sms_ids = [c.id for c in campaigns if c.channel == "sms"]
    email_ids = [c.id for c in campaigns if c.channel == "email"]
    agg: dict[int, dict[str, int]] = defaultdict(dict)
    if sms_ids:
        for cid, st, n in db.execute(
            select(CampaignRecipient.campaign_id, Message.status, func.count())
            .join(Message, Message.id == CampaignRecipient.message_id)
            .where(CampaignRecipient.campaign_id.in_(sms_ids))
            .group_by(CampaignRecipient.campaign_id, Message.status)
        ):
            agg[cid][_public(st)] = agg[cid].get(_public(st), 0) + n
    if email_ids:
        for cid, st, n in db.execute(
            select(CampaignRecipient.campaign_id, Email.status, func.count())
            .join(Email, Email.id == CampaignRecipient.email_id)
            .where(CampaignRecipient.campaign_id.in_(email_ids))
            .group_by(CampaignRecipient.campaign_id, Email.status)
        ):
            agg[cid][_public(st)] = agg[cid].get(_public(st), 0) + n
    for cid, counts in agg.items():
        out[cid]["message_counts"] = counts
    for v in out.values():
        v["recipients_total"] = sum(v["recipient_counts"].values())
    return out


def recipient_rows(db: Session, rows: list[CampaignRecipient]) -> list[dict]:
    """Marrësit me kontaktin dhe mesazhin e tyre — dy pyetje për faqen (jo një për rresht)."""
    cids = [r.contact_id for r in rows]
    contacts = (
        {c.id: c for c in db.scalars(select(Contact).where(Contact.id.in_(cids)))} if cids else {}
    )
    mids = [r.message_id for r in rows if r.message_id]
    msgs = (
        {m.id: m for m in db.scalars(select(Message).where(Message.id.in_(mids)))} if mids else {}
    )
    out = []
    for r in rows:
        c, m = contacts.get(r.contact_id), msgs.get(r.message_id)
        out.append({
            "id": r.id, "contact_id": r.contact_id, "status": r.status.value, "reason": r.reason,
            "message_id": r.message_id,
            "address": r.address, "name": " ".join(x for x in (c.first_name, c.last_name) if x) if c else "",
            "message_public_id": m.public_id if m else None,
            "message_status": _public(m.status) if m else None,
            "error_code": m.error_code if m else None,
            "updated_at": (m.updated_at if m and hasattr(m, "updated_at") else r.queued_at),
        })  # fmt: skip
    return out


_SAFE_DETAIL = ("at",)


def activity(db: Session, campaign_id: int, limit: int = 100) -> list[dict]:
    """Evenimentet e audituara të campaign-it (vetëm ato që ekzistojnë); aktori s'ekspozohet — vetëm lloji (ju/staf)."""
    rows = db.scalars(
        select(AuditLog)
        .where(AuditLog.target_type == "campaign", AuditLog.target_id == str(campaign_id))
        .order_by(AuditLog.id.desc())
        .limit(max(1, min(limit, 200)))
    )
    out = []
    for a in rows:
        try:
            d = json.loads(a.detail) if a.detail else {}
        except ValueError:
            d = {}
        out.append({
            "action": a.action, "at": a.created_at, "by": "you" if a.role == "client" else "staff",
            "detail": {k: d[k] for k in _SAFE_DETAIL if k in d},
        })  # fmt: skip
    return out


def list_counts(db: Session, list_ids: list[int]) -> dict[int, int]:
    if not list_ids:
        return {}
    return dict(
        db.execute(
            select(ListMember.list_id, func.count())
            .where(ListMember.list_id.in_(list_ids))
            .group_by(ListMember.list_id)
        ).all()
    )


def list_name(db: Session, list_id: int) -> str | None:
    return db.scalar(select(ContactList.name).where(ContactList.id == list_id))
