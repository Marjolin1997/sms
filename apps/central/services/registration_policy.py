"""Politika e regjistrimit për produkt (M8-b). Pa commit (transaksioni i thirrësit); pa HTTP.

`get_policy` → rreshti ose None (None = vetë-regjistrimi i çaktivizuar: fail-closed). `set_policy`
krijon/përditëson me aktor njeri dhe audit `registration_policy.create|update` (before/after të
fushave të ndryshuara; no-op ⇒ zero audit). `approval_mode=automatic` ruhet VETËM kur gate-i
`CENTRAL_ALLOW_UNVERIFIED_AUTO_REGISTRATION` është true (s'ka verifikim kontakti): varianti "mos
lejo ta vendosësh" është më i pastri sepse s'lë politika automatic të fjetura që aktivizohen nga
një ndryshim konfigurimi; submit-i e rikontrollon gate-in vetë (mbrojtje në thellësi).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.errors import Conflict, Invalid, NotFound
from apps.central.core.timeutil import utcnow
from apps.central.models.product import Product, ProductStatus
from apps.central.models.registration_policy import (
    APPROVAL_MODES,
    AUTOMATIC,
    MANUAL,
    ProductRegistrationPolicy,
)
from apps.central.models.user import CentralUser
from apps.central.services import audit

RESOURCE = "product_registration_policy"
ACTION_CREATE, ACTION_UPDATE = "registration_policy.create", "registration_policy.update"
_UNSET = object()


@dataclass(frozen=True, slots=True)
class PolicyView:
    """Çfarë dihet për një produkt në momentin e vendimit (live, jo e ngrirë)."""

    product_id: uuid.UUID
    code: str
    product_active: bool
    has_policy: bool
    self_registration_enabled: bool
    approval_mode: str | None

    @property
    def eligible(self) -> bool:
        """Kërkohet/miratohet nga vetë-regjistrimi: produkt active + politikë + e aktivizuar."""
        return self.product_active and self.has_policy and self.self_registration_enabled

    def snapshot(self) -> dict:
        return {
            "code": self.code, "approval_mode": self.approval_mode,
            "self_registration_enabled": self.self_registration_enabled,
        }  # fmt: skip


def automatic_allowed() -> bool:
    return bool(settings.allow_unverified_auto_registration)


def get_policy(db: Session, product_id: uuid.UUID | str) -> ProductRegistrationPolicy | None:
    pid = product_id if isinstance(product_id, uuid.UUID) else _uuid(product_id)
    return db.get(ProductRegistrationPolicy, pid, populate_existing=True)


def _uuid(value) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise Invalid("invalid product id") from None


def views(db: Session, product_ids: list[uuid.UUID], *, lock: bool = False) -> list[PolicyView]:
    """Pamja live për produktet e dhëna (në rendin e dhënë). Produkt që mungon ⇒ i paplotësuar.
    `lock=True` (provisioning): kyç produktet (FOR SHARE, sipas id) dhe rilexon gjendjen e
    commit-uar — retire/politikë paralele pret ose ka ndodhur PARA; asnjë gjendje e vjetër."""
    q = select(Product).where(Product.id.in_(product_ids))
    if lock:
        q = (
            q.order_by(Product.id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    prods = {p.id: p for p in db.scalars(q)}
    pq = (
        select(ProductRegistrationPolicy)
        .where(ProductRegistrationPolicy.product_id.in_(product_ids))
        .order_by(ProductRegistrationPolicy.product_id)
        .execution_options(populate_existing=True)
    )
    if lock:
        pq = pq.with_for_update(read=True)
    pols = {x.product_id: x for x in db.scalars(pq)}
    out = []
    for pid in product_ids:
        p, pol = prods.get(pid), pols.get(pid)
        out.append(PolicyView(
            pid, p.code if p else "?", bool(p and p.status == ProductStatus.ACTIVE.value),
            pol is not None, bool(pol and pol.self_registration_enabled),
            pol.approval_mode if pol else None,
        ))  # fmt: skip
    return out


def set_policy(
    db: Session,
    product_id: uuid.UUID | str,
    actor: CentralUser,
    *,
    self_registration_enabled=_UNSET,
    approval_mode=_UNSET,
    now: datetime | None = None,
) -> tuple[ProductRegistrationPolicy, dict]:
    """Krijon (parazgjedhje: çaktivizuar + manual) ose përditëson. → (politika, changes);
    changes == {} = no-op (pa audit). Njeri vetëm. Aktivizimi kërkon produkt `active`;
    `automatic` kërkon gate-in e konfigurimit."""
    if not isinstance(actor, CentralUser):
        raise Invalid("a human Central user is required to change registration policies")
    pid = _uuid(product_id)
    product = db.get(Product, pid)
    if product is None:
        raise NotFound("product not found")
    if self_registration_enabled is not _UNSET and not isinstance(self_registration_enabled, bool):
        raise Invalid("self_registration_enabled must be a boolean")
    if approval_mode is not _UNSET and approval_mode not in APPROVAL_MODES:
        raise Invalid(f"approval_mode must be one of {list(APPROVAL_MODES)}")
    row = db.scalar(
        select(ProductRegistrationPolicy)
        .where(ProductRegistrationPolicy.product_id == pid)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    creating = row is None
    cur_enabled = False if creating else row.self_registration_enabled
    cur_mode = MANUAL if creating else row.approval_mode
    new_enabled = cur_enabled if self_registration_enabled is _UNSET else self_registration_enabled
    new_mode = cur_mode if approval_mode is _UNSET else approval_mode
    if new_enabled and not cur_enabled and product.status != ProductStatus.ACTIVE.value:
        raise Conflict("a retired product cannot be opened for self-registration")
    if new_mode == AUTOMATIC and cur_mode != AUTOMATIC and not automatic_allowed():
        raise Conflict(
            "automatic approval is disabled: public registration has no contact verification"
        )
    changes = {}
    if new_enabled != cur_enabled:
        changes["self_registration_enabled"] = (cur_enabled, new_enabled)
    if new_mode != cur_mode:
        changes["approval_mode"] = (cur_mode, new_mode)
    if not creating and not changes:
        return row, {}
    now = now or utcnow()
    if creating:
        row = ProductRegistrationPolicy(
            product_id=pid, self_registration_enabled=new_enabled, approval_mode=new_mode,
            created_at=now, updated_at=now,
        )  # fmt: skip
        db.add(row)
    else:
        row.self_registration_enabled, row.approval_mode, row.updated_at = (
            new_enabled,
            new_mode,
            now,
        )
    db.flush()
    detail = {"product_id": str(pid), "product_code": product.code,
              "before": {k: v[0] for k, v in changes.items()} if not creating else None,
              "after": {"self_registration_enabled": new_enabled, "approval_mode": new_mode}
              if creating else {k: v[1] for k, v in changes.items()}}  # fmt: skip
    audit.record(
        db, actor, ACTION_CREATE if creating else ACTION_UPDATE, RESOURCE, pid, detail, now=now
    )
    return row, {k: {"before": v[0], "after": v[1]} for k, v in changes.items()}
