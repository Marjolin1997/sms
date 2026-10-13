"""Pamje READ-ONLY e audit-it të Central (M11-G): audit i stafit (`audit_log`) dhe historiku i vendimeve të sender-ave.

Asnjë shkrim, asnjë histori e shpikur: vetëm rreshtat që ekzistojnë. `detail` kalon nga një pastrues (çelësat që duken si
sekrete hiqen, vlerat e gjata presin). Filtrat e ruajtur "scope" janë prefikse veprimesh në një vend të vetëm (SCOPES).
"""

import re

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from apps.central.models.audit import AuditLog
from apps.central.models.enterprise import Enterprise
from apps.central.models.sender import SenderDecision, SenderRegistry
from apps.central.models.user import CentralUser

# scope → prefikse të `action` (një burim i vetëm i së vërtetës për kategoritë e audit-it)
SCOPES: dict[str, tuple[str, ...]] = {
    "sender": ("sender", "sender_policy"),
    "commercial": (
        "billing",
        "billing_profile",
        "billing_subscription",
        "commercial_plan",
        "plan_version",
        "price_",
        "payment",
        "invoice",
        "credit_",
        "debit_",
        "usage_report",
    ),  # fmt: skip
    "identity": ("user.", "service_", "enterprise_product", "product", "registration"),
}
_SECRET = re.compile(
    r"secret|token|password|passwd|hash|signature|api_?key|private|credential", re.I
)
MAX_DEPTH, MAX_STR = 4, 300


def scrub(v, depth: int = 0):
    """Heq çelësat që duken si sekrete; pret vargjet e gjata; kufizon thellësinë."""
    if depth > MAX_DEPTH:
        return None
    if isinstance(v, dict):
        return {k: scrub(x, depth + 1) for k, x in v.items() if not _SECRET.search(str(k))}
    if isinstance(v, list):
        return [scrub(x, depth + 1) for x in v[:50]]
    if isinstance(v, str) and len(v) > MAX_STR:
        return v[:MAX_STR] + "…"
    return v


def scope_clause(scope: str | None):
    if not scope or scope == "all":
        return None
    prefixes = SCOPES[scope]
    return or_(
        *[AuditLog.action.like(p + ("%" if p.endswith(("_", ".")) else ".%")) for p in prefixes]
    )


def audit_stmt(
    scope=None, action=None, resource_type=None, actor_kind=None, actor_id=None, lo=None, hi=None
):
    stmt = select(AuditLog, CentralUser.email).join(
        CentralUser, CentralUser.id == AuditLog.actor_id, isouter=True
    )
    c = scope_clause(scope)
    if c is not None:
        stmt = stmt.where(c)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    if actor_kind:
        stmt = stmt.where(AuditLog.actor_kind == actor_kind)
    if actor_id:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if lo is not None:
        stmt = stmt.where(AuditLog.created_at >= lo)
    if hi is not None:
        stmt = stmt.where(AuditLog.created_at < hi)
    return stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())


def audit_item(a: AuditLog, email: str | None) -> dict:
    return {
        "id": str(a.id), "at": a.created_at.isoformat(), "actor_kind": a.actor_kind,
        "actor": email if a.actor_kind == "user" else a.actor_label,
        "actor_id": None if a.actor_id is None else str(a.actor_id),
        "action": a.action, "resource_type": a.resource_type, "resource_id": a.resource_id,
        "detail": scrub(a.detail) if a.detail else None,
    }  # fmt: skip


def decision_stmt(
    decision=None, category=None, source=None, country=None, enterprise_id=None,
    registry_id=None, lo=None, hi=None,
):  # fmt: skip
    stmt = (
        select(SenderDecision, SenderRegistry, Enterprise.name, CentralUser.email)
        .join(SenderRegistry, SenderRegistry.id == SenderDecision.registry_id)
        .join(Enterprise, Enterprise.id == SenderRegistry.enterprise_id)
        .join(CentralUser, CentralUser.id == SenderDecision.decided_by_id, isouter=True)
    )
    for col, val in (
        (SenderDecision.decision, decision), (SenderDecision.category, category),
        (SenderDecision.source, source), (SenderRegistry.country, country and country.upper()),
        (SenderRegistry.enterprise_id, enterprise_id), (SenderDecision.registry_id, registry_id),
    ):  # fmt: skip
        if val:
            stmt = stmt.where(col == val)
    if lo is not None:
        stmt = stmt.where(SenderDecision.decided_at >= lo)
    if hi is not None:
        stmt = stmt.where(SenderDecision.decided_at < hi)
    return stmt.order_by(SenderDecision.decided_at.desc(), SenderDecision.id.desc())


def decision_item(d: SenderDecision, r: SenderRegistry, ent_name: str, email: str | None) -> dict:
    return {
        "id": str(d.id), "decided_at": d.decided_at.isoformat(), "decision": d.decision,
        "from_status": d.from_status, "to_status": d.to_status, "category": d.category,
        "source": d.source, "reason": d.reason, "evidence_ref": d.evidence_ref,
        "actor": email if d.decided_by_id else d.actor_label,
        "actor_kind": "user" if d.decided_by_id else "system",
        "policy_source": d.policy_source, "policy_revision": d.policy_revision,
        "policy_id": None if d.policy_id is None else str(d.policy_id),
        "sender": r.display_value, "sender_kind": r.sender_kind, "country": r.country,
        "registry_id": str(r.id), "enterprise_id": str(r.enterprise_id), "enterprise": ent_name,
    }  # fmt: skip


def facets(db: Session, scope: str | None = None) -> dict:
    c = scope_clause(scope)
    where = [] if c is None else [c]
    return {
        "actions": sorted(db.scalars(select(AuditLog.action).where(*where).distinct().limit(300))),
        "resource_types": sorted(
            db.scalars(select(AuditLog.resource_type).where(*where).distinct().limit(100))
        ),
        "scopes": ["all", *SCOPES],
    }
