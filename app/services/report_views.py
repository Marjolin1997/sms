"""Pamje raportimi VETËM-LEXIM (M11-G): agregate të serverit për Reports/Audit të klientit.

Asnjë semantikë e re biznesi: numërues të papërpunuar nga tabelat ekzistuese, gjithmonë të skopuar me
`owned()`; statuset janë ato PUBLIKE (UNKNOWN → `sending`). Numri i pyetjeve nuk varet nga numri i rreshtave.
Moduli NUK importohet nga hot-path (submit/dispatch/process_one/sync/reporter/money) — pin në test.
"""

import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.scope import owned
from app.models.admin import AuditLog
from app.models.campaigns import Campaign, CampaignRecipient
from app.models.email import Email
from app.models.sending import Message
from app.services import campaign_views as views

MAX_FAILURE_ROWS = 200
FAIL = ("failed", "bounced")
# Fushat e `detail` që mund t'i shfaqen klientit (lista e lejuar; çdo gjë tjetër — url, owner, role, shuma, sekrete — hiqet).
AUDIT_SAFE_DETAIL = ("at", "version", "list", "fields", "from", "to", "reason", "purpose")
HIDDEN_AUDIT_ACTIONS = "cross_tenant."  # prefiks: qasje e stafit, e brendshme


def public(status) -> str:
    v = getattr(status, "value", status)
    return "sending" if v == "unknown" else v


def delivery_rate(counts: dict[str, int]) -> float | None:
    """delivered ÷ (delivered + failed/bounced), në %. Mesazhet ende në rrugë nuk hyjnë; pa terminal ⇒ None."""
    d = counts.get("delivered", 0)
    f = sum(counts.get(k, 0) for k in FAIL)
    return None if d + f == 0 else round(d * 100 / (d + f), 1)


def message_status_counts(db: Session, owner, lo: datetime, hi: datetime) -> dict[str, int]:
    out: dict[str, int] = {}
    for st, n in db.execute(
        select(Message.status, func.count())
        .where(owned(Message, owner), Message.created_at >= lo, Message.created_at < hi)
        .group_by(Message.status)
    ):
        out[public(st)] = out.get(public(st), 0) + n
    return out


def email_status_counts(db: Session, owner, lo: datetime, hi: datetime) -> dict[str, int]:
    out: dict[str, int] = {}
    for st, n in db.execute(
        select(Email.status, func.count())
        .where(owned(Email, owner), Email.created_at >= lo, Email.created_at < hi)
        .group_by(Email.status)
    ):
        out[public(st)] = out.get(public(st), 0) + n
    return out


def campaign_status_counts(db: Session, owner, lo: datetime, hi: datetime) -> dict[str, int]:
    return {
        st.value: n
        for st, n in db.execute(
            select(Campaign.status, func.count())
            .where(owned(Campaign, owner), Campaign.created_at >= lo, Campaign.created_at < hi)
            .group_by(Campaign.status)
        )
    }


def failure_rows(
    db: Session, owner, lo: datetime, hi: datetime, campaign_id: int | None = None
) -> tuple[list[dict], bool, dict[str, int]]:
    """→ (rreshta {status, error_code, count} të renditura, truncated, numërues sipas statusit)."""
    base = select(Message.status, Message.error_code, func.count()).where(
        owned(Message, owner), Message.created_at >= lo, Message.created_at < hi
    )
    if campaign_id is not None:
        base = base.join(CampaignRecipient, CampaignRecipient.message_id == Message.id).where(
            CampaignRecipient.campaign_id == campaign_id
        )
    merged: dict[tuple[str, str | None], int] = {}
    by_status: dict[str, int] = {}
    for st, code, n in db.execute(base.group_by(Message.status, Message.error_code)):
        s = public(st)
        by_status[s] = by_status.get(s, 0) + n
        if code is not None or s == "failed":
            merged[(s, code)] = merged.get((s, code), 0) + n
    rows = sorted(
        ({"status": s, "error_code": c, "count": n} for (s, c), n in merged.items()),
        key=lambda r: (-r["count"], r["error_code"] or ""),
    )
    return rows[:MAX_FAILURE_ROWS], len(rows) > MAX_FAILURE_ROWS, by_status


def campaign_stmt(owner, lo: datetime, hi: datetime, status=None, q: str | None = None):
    stmt = select(Campaign).where(
        owned(Campaign, owner), Campaign.created_at >= lo, Campaign.created_at < hi
    )
    if status:
        stmt = stmt.where(Campaign.status == status)
    if q:
        esc = q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(func.lower(Campaign.name).like(f"%{esc}%", escape="\\"))
    return stmt


def campaign_report_rows(db: Session, campaigns: list[Campaign]) -> list[dict]:
    sums = views.page_summaries(db, campaigns)
    out = []
    for c in campaigns:
        s = sums.get(c.id, {"recipients_total": 0, "recipient_counts": {}, "message_counts": {}})
        out.append({
            "id": c.id, "name": c.name, "channel": c.channel, "status": c.status.value,
            "created_at": c.created_at, "scheduled_at": c.scheduled_at,
            "started_at": c.started_at, "completed_at": c.completed_at,
            "recipients_total": s["recipients_total"], "message_counts": s["message_counts"],
            "delivery_rate": delivery_rate(s["message_counts"]),
        })  # fmt: skip
    return out


def audit_stmt(
    owner, lo: datetime, hi: datetime, action: str | None = None, target_type: str | None = None
):
    stmt = select(AuditLog).where(
        AuditLog.owner_ref == owner.owner_ref,
        AuditLog.created_at >= lo,
        AuditLog.created_at < hi,
        ~AuditLog.action.like(HIDDEN_AUDIT_ACTIONS + "%"),
    )
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if target_type:
        stmt = stmt.where(AuditLog.target_type == target_type)
    return stmt


def audit_item(a: AuditLog) -> dict:
    try:
        d = json.loads(a.detail) if a.detail else {}
    except ValueError:
        d = {}
    if not isinstance(d, dict):
        d = {}
    by = "you" if a.role == "client" else "system" if a.role == "system" else "staff"
    return {
        "id": a.id, "at": a.created_at, "by": by, "action": a.action,
        "target_type": a.target_type, "target_id": a.target_id,
        "detail": {k: d[k] for k in AUDIT_SAFE_DETAIL if k in d},
    }  # fmt: skip


def audit_facets(db: Session, owner) -> dict:
    base = AuditLog.owner_ref == owner.owner_ref
    hide = ~AuditLog.action.like(HIDDEN_AUDIT_ACTIONS + "%")
    actions = db.scalars(select(AuditLog.action).where(base, hide).distinct().limit(200))
    types = db.scalars(select(AuditLog.target_type).where(base, hide).distinct().limit(100))
    return {"actions": sorted(actions), "target_types": sorted(types)}


def usage_csv_rows(series: list[dict]):
    for d in series:
        s, e = d["sms"], d["email"]
        yield [d["date"], s["count"], s["delivered"], s["failed"], s["segments"], s["cost"],
               e["count"], e["delivered"], e["bounced"], e["failed"]]  # fmt: skip
