"""M1c-a: konteksti i ekzekutimit, i shprehur dhe i pandryshueshëm (jo global, jo ContextVar).

Tri kontekste të ndara:
  TENANT  — një Enterprise i identifikuar; çdo qasje resursi skopohet me `enterprise_id`.
  SYSTEM  — stafi/admin; qasja ndër-tenant është vetëm e shprehur (`SystemContext`), e audituar.
  WORKER  — identiteti vjen nga rreshti/job-i që procesohet (`for_row`), asnjëherë nga gjendje
            e përbashkët apo nga një kërkesë.

`TenantContext` krijohet në një vend të vetëm nga këto fabrika dhe kalohet si argument: nuk ka
gjendje globale, prandaj nuk mund të rrjedhë mes kërkesave ose job-eve. `owner_ref` mbetet fushë
përputhshmërie (skopim i dyfishtë: enterprise_id DHE owner_ref duhet të përputhen)."""

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session


class TenantUnresolved(Exception):
    """Ky owner_ref nuk ka identitet Enterprise (mungon ose është anomali): asnjë qasje tenant."""

    code = "tenant_unresolved"


@dataclass(frozen=True, slots=True)
class TenantContext:
    enterprise_id: uuid.UUID
    owner_ref: str  # përputhshmëri; nuk skopon vetëm
    origin: str = "unknown"  # principal | staff | worker | test (vetëm për diagnostikim)

    def __post_init__(self) -> None:
        if not isinstance(self.enterprise_id, uuid.UUID) or not self.owner_ref:
            raise TypeError("TenantContext requires a UUID enterprise_id and a non-empty owner_ref")


@dataclass(frozen=True, slots=True)
class SystemContext:
    """Qasje ndër-tenant e shprehur. Kush e krijon duhet të japë aktorin dhe arsyen; përdorimi mbi
    resurse sensitive auditohet nga `scope.cross_tenant`."""

    actor: str
    reason: str

    def __post_init__(self) -> None:
        if not self.actor or not self.reason:
            raise ValueError("SystemContext requires an actor and a reason")


def for_owner(db: Session, owner_ref: str, *, create: bool = False, origin: str = "staff"):
    """TenantContext për një `owner_ref` të dhënë shprehimisht (staf, worker legacy, skripte).
    `create=False`: vetëm lexim i regjistrit; `create=True`: krijon Enterprise-in nëse mungon."""
    from app.services import enterprises

    eid = enterprises.resolve_id(db, owner_ref) if create else enterprises.lookup_id(db, owner_ref)
    if eid is None:
        raise TenantUnresolved(f"no enterprise identity for owner_ref {owner_ref!r}")
    return TenantContext(eid, owner_ref, origin)


def for_row(db: Session, row, *, origin: str = "worker") -> TenantContext:
    """WORKER: identiteti vjen nga rreshti që procesohet (`enterprise_id`); rreshtat legacy pa të
    zgjidhen me `owner_ref` të vetë rreshtit (vetëm për përputhshmëri prapa)."""
    if row.enterprise_id is not None:
        return TenantContext(row.enterprise_id, row.owner_ref, origin)
    return for_owner(db, row.owner_ref, origin=origin)
