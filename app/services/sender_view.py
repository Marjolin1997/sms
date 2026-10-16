"""M10-S5: modeli i leximit të sender-ave për API (klient/admin) — status EFEKTIV i derivuar, kurrë shkrim në `SenderId`.

- `local`/`shadow`: `effective_status` = statusi lokal (autoriteti lokal vendos); klienti s'sheh diagnostikë Central.
- `central`: `effective_status` vjen nga projeksioni i sinkronizuar dhe politika (`approved` + politikë `allowed=false` ⇒ `policy_denied`). Mungesa e projeksionit ⇒ `not_synchronized`
  (kurrë "approved"), përveç kur lokali është tashmë `rejected|revoked` (mohim edhe pa Central). Fusha ekzistuese `status` mbetet statusi LOKAL (përputhshmëri).
- Admin (leja `sender:review`) shton: statusin Central, kategorinë e driftit, rishikimet, freskinë, çështjet e bootstrap-it. Askush s'sheh enterprise tjetër (projeksioni lexohet vetëm për enterprise-in e rreshtit).
- Lista = SQL i kufizuar (pa N+1): rreshtat + 1 projeksion (IN) + 1 politika + (admin) 1 çështje; freskia nga cache-i i procesit."""

from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.messaging import ApprovalStatus, SenderId
from app.models.sender_authority import SenderBootstrapIssue
from app.models.sender_sync import SyncedSenderAuthorization, SyncedSenderPolicy
from app.services import sender_authority as sau


@dataclass(frozen=True, slots=True)
class SenderView:
    local_status: str
    authority_mode: str
    effective_status: str
    central_status: str | None
    sync_status: str  # not_applicable | synchronized | stale | not_synchronized
    can_resubmit: bool
    can_review_locally: bool
    decided_at: str | None = None
    # vetëm admin:
    drift: str | None = None
    cp_revision: int | None = None
    policy_revision: int | None = None
    bootstrap_issues: tuple[str, ...] | None = None

    def public(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None or k in ("central_status",)}


def api_fields(v: SenderView) -> dict:
    """Fushat shtesë të API (pa `local_status`/`decided_at`; tuple → list)."""
    skip = ("local_status", "decided_at")
    return {
        k: (list(x) if isinstance(x, tuple) else x) for k, x in v.public().items() if k not in skip
    }


def _rank(r) -> tuple:
    return (r.status != "approved", r.id)


def build_views(db: Session, rows: list[SenderId], *, admin: bool = False) -> dict[int, SenderView]:
    mode = settings.sender_authority
    need = mode == "central" or admin
    proj: dict[tuple, SyncedSenderAuthorization] = {}
    pol: dict[tuple, SyncedSenderPolicy] = {}
    issues: dict[int, list[str]] = {}
    stale = False
    if need and rows:
        eids = {r.enterprise_id for r in rows if r.enterprise_id is not None}
        norms = {r.norm_value for r in rows}
        if eids:
            for a in sorted(
                db.scalars(
                    select(SyncedSenderAuthorization).where(
                        SyncedSenderAuthorization.enterprise_id.in_(eids),
                        SyncedSenderAuthorization.norm_value.in_(norms),
                        SyncedSenderAuthorization.projection_state == "active",
                    )
                ),
                key=_rank,
                reverse=True,
            ):
                proj[(a.enterprise_id, a.country, a.norm_value)] = (
                    a  # më i miri fiton (i fundit në rend rritës)
                )
        pol = {
            (p.country, p.sender_kind): p
            for p in db.scalars(
                select(SyncedSenderPolicy).where(SyncedSenderPolicy.projection_state == "active")
            )
        }
        stale = sau.projection_stale(db)
        if admin:
            for i in db.scalars(
                select(SenderBootstrapIssue).where(
                    SenderBootstrapIssue.sender_id.in_([r.id for r in rows]),
                    SenderBootstrapIssue.resolved_at.is_(None),
                )
            ):
                issues.setdefault(i.sender_id, []).append(i.category)
    out: dict[int, SenderView] = {}
    for r in rows:
        local = r.status.value
        p = (
            proj.get((r.enterprise_id, r.country, r.norm_value))
            if r.enterprise_id is not None
            else None
        )
        policy_ok = True
        if p is not None:
            pp = pol.get((p.country, p.sender_kind))
            policy_ok = pp.allowed if pp is not None else True
        if mode == "central":
            if p is None:
                eff = (
                    local
                    if r.status in (ApprovalStatus.REJECTED, ApprovalStatus.REVOKED)
                    else "not_synchronized"
                )
            else:
                eff = (
                    ("approved" if policy_ok else "policy_denied")
                    if p.status == "approved"
                    else p.status
                )
        else:
            eff = local
        if not need:
            sync = "not_applicable"
        elif p is None:
            sync = "not_synchronized"
        else:
            sync = "stale" if stale else "synchronized"
        if mode == "central":
            resub = eff in ("rejected", "revoked")
        else:
            resub = r.status in (ApprovalStatus.REJECTED, ApprovalStatus.REVOKED)
        show_central = p is not None and (admin or mode == "central")
        drift = cp = prev = None
        if admin:
            c_allowed = p is not None and p.status == "approved" and policy_ok
            reason = (
                "missing"
                if p is None
                else ("policy_denied" if (p.status == "approved" and not policy_ok) else p.status)
            )
            ce = sau.CentralEvaluation(c_allowed, reason, r.country, "", stale=stale)
            drift = sau.classify_comparison(r.status == ApprovalStatus.APPROVED, ce)
            if p is not None:
                cp, prev = p.cp_revision, p.policy_revision
        out[r.id] = SenderView(
            local_status=local, authority_mode=mode, effective_status=eff,
            central_status=p.status if show_central else None, sync_status=sync, can_resubmit=resub,
            can_review_locally=mode != "central", decided_at=(p.decided_at.isoformat() if (p is not None and show_central) else None),
            drift=drift, cp_revision=cp, policy_revision=prev,
            bootstrap_issues=tuple(sorted(issues.get(r.id, []))) if admin else None,
        )  # fmt: skip
    return out
