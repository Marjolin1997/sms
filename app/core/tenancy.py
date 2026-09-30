"""M1b: dual-write i CENTRALIZUAR i identitetit të tenant-it.

Një listener `before_flush` mbi çdo Session: për çdo objekt të ri (ose me `owner_ref` të ndryshuar)
të një modeli `TenantOwned`, `enterprise_id` përcaktohet nga `owner_ref` përmes një resolveri të
vetëm (`enterprises.resolve_id`). Shërbimet NUK shkruajnë `enterprise_id` vetë. Invariant në shkrim:
nëse `enterprise_id` jepet eksplicitisht, duhet të përputhet me `owner_ref` (përndryshe TenantMismatch:
gabim programimi, jo anomali e të dhënave).

Sjellja e sistemit nuk ndryshon: asnjë përjashtim për anomali `owner_ref` (rreshti ruhet me
`enterprise_id` NULL, raportohet nga `enterprises.check_consistency`), përveç nëse
SMS_ENTERPRISE_DUAL_WRITE_STRICT=true. Çaktivizohet plotësisht me SMS_ENTERPRISE_DUAL_WRITE=false."""

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.tenant import TenantOwned


class TenantMismatch(RuntimeError):
    """`enterprise_id` i dhënë nuk i përket `owner_ref` të të njëjtit rresht."""


def _apply(session: Session, obj: TenantOwned, *, only_if_changed: bool) -> None:
    from app.services import enterprises

    owner = obj.owner_ref
    if owner is None:
        return  # p.sh. çelës stafi: pa tenant
    if only_if_changed and not inspect(obj).attrs.owner_ref.history.has_changes():
        return
    resolved = enterprises.resolve_id(session, owner)
    explicit = obj.enterprise_id
    if resolved is None:
        if settings.enterprise_dual_write_strict:
            raise TenantMismatch(f"cannot resolve an enterprise for owner_ref {owner!r}")
        if explicit is not None and not only_if_changed:
            raise TenantMismatch(f"enterprise_id set for an unresolvable owner_ref {owner!r}")
        obj.enterprise_id = None
        return
    if explicit is not None and explicit != resolved and not only_if_changed:
        raise TenantMismatch(f"enterprise_id {explicit} does not belong to owner_ref {owner!r}")
    obj.enterprise_id = resolved


@event.listens_for(Session, "before_flush")
def _dual_write(session: Session, flush_context, instances) -> None:
    if not settings.enterprise_dual_write:
        return
    for obj in list(session.new):
        if isinstance(obj, TenantOwned):
            _apply(session, obj, only_if_changed=False)
    for obj in list(session.dirty):
        if isinstance(obj, TenantOwned):
            _apply(session, obj, only_if_changed=True)
