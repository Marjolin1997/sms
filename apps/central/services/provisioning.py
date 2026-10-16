"""Provisioning i një kërkese regjistrimi të miratuar (M8-c). Vetëm Central: asnjë thirrje drejt
Enterprise (M7 është transporti i vetëm).

Modeli i transaksioneve (i miratuar):
  Tx1  miratimi (M8-a/b)                       → status=approved, provisioning_status=pending
  Tx2  `provision()` — ATOMIK, commit nga thirrësi: kyç i kërkesës → rivlerësim → Enterprise (i ri ose
       i lidhur eksplicit) → auto-grant (vetëm për Enterprise të ri) → assign_product për çdo produkt
       → `registration_products.assignment_id` → provisioned + attempts+1 → audit sistemi. Çdo dështim
       e rikthen TË GJITHË Tx2 (asnjë ndryshim i pjesshëm).
  Tx3  `record_failure()` — transaksion i veçantë pas rollback-ut të Tx2: failed + kod i qëndrueshëm +
       attempts+1 (+ audit `registration.provision_failed`).
`run()` orkestron Tx2→Tx3. Kërkesa mbetet `approved` (kurrë `rejected` nga dështimi).

Attempts: çdo përpjekje e nisur (parakushtet kaluan) rritet saktësisht NJË herë — në Tx2 kur ka
sukses (commit-ohet bashkë), ose në Tx3 kur dështon (Tx2 u rikthye, pra s'ka dyfishim). Thirrje që
s'plotësojnë parakushtet (submitted/rejected) ose kërkesë e provisioned (no-op) nuk numërohen.
Pa retry në sfond: retry është thirrje eksplicite (admin/CLI).
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from apps.central.core.errors import CentralError, Conflict, Invalid, NotFound
from apps.central.core.timeutil import utcnow
from apps.central.models.enterprise import Enterprise, EnterpriseStatus
from apps.central.models.enterprise_product import AssignmentStatus, EnterpriseProduct
from apps.central.models.registration import (
    APPROVED,
    FAILED,
    PENDING,
    PROVISIONED,
    RegistrationProduct,
    RegistrationRequest,
)
from apps.central.models.user import CentralUser
from apps.central.services import audit, service_auth
from apps.central.services import enterprise_products as asg
from apps.central.services import enterprises as enterprise_svc
from apps.central.services import registration_policy as policy
from apps.central.services import registrations as reg

log = logging.getLogger("central.provisioning")

LABEL = "system:registration_provisioning"
ACTION_PROVISION = "registration.provision"
ACTION_FAILED = "registration.provision_failed"
ACTION_LINK = "registration.link_enterprise"
RESOURCE = reg.RESOURCE

# Kode të qëndrueshme (kurrë tekst i papërpunuar i përjashtimit)
PRODUCT_RETIRED = "product_retired"
POLICY_DISABLED = "policy_disabled"
ENTERPRISE_NOT_FOUND = "enterprise_not_found"
ENTERPRISE_SUSPENDED = "enterprise_suspended"
ASSIGNMENT_EXISTS_SUSPENDED = "assignment_exists_suspended"
ASSIGNMENT_CONFLICT = "assignment_conflict"
PROVISIONING_CONFLICT = "provisioning_conflict"
DATABASE_ERROR = "database_error"
UNEXPECTED_ERROR = "unexpected_error"
ERROR_CODES = frozenset({
    PRODUCT_RETIRED, POLICY_DISABLED, ENTERPRISE_NOT_FOUND, ENTERPRISE_SUSPENDED,
    ASSIGNMENT_EXISTS_SUSPENDED, ASSIGNMENT_CONFLICT, PROVISIONING_CONFLICT, DATABASE_ERROR,
    UNEXPECTED_ERROR,
})  # fmt: skip


class ProvisioningError(CentralError):
    """Dështim i provisioning-ut me kod të qëndrueshëm (shkon te Tx3 pas rollback-ut)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class NotProvisionable(Conflict):
    """Parakushte të pakënaqura (submitted/rejected): asnjë përpjekje, asnjë ndryshim."""


@dataclass(slots=True)
class ProvisionResult:
    request_id: uuid.UUID
    status: str  # provisioned | failed
    already_provisioned: bool = False
    enterprise_id: uuid.UUID | None = None
    new_enterprise: bool = False
    assignments: list[dict] = field(default_factory=list)
    auto_grants: list[str] = field(default_factory=list)
    error_code: str | None = None
    attempts: int = 0


# --- lidhja e Enterprise ekzistues -----------------------------------------------------------------------


def _link(db: Session, row: RegistrationRequest, enterprise_id, actor: CentralUser, now) -> None:
    """Lidhje eksplicite (vendim njeriu) e kërkesës së miratuar me Enterprise EKZISTUES. E ndaluar
    pas provisioned (enterprise_id i pandryshueshëm); ndryshimi i një lidhjeje ekzistuese ⇒ Conflict."""
    if not isinstance(actor, CentralUser):
        raise Invalid("a human Central user is required to link an enterprise")
    actor_id = actor.id
    eid = enterprise_id if isinstance(enterprise_id, uuid.UUID) else _uuid(enterprise_id)
    if row.provisioning_status == PROVISIONED:
        raise NotProvisionable("enterprise is immutable after provisioning")
    if row.enterprise_id is not None and row.enterprise_id != eid:
        raise NotProvisionable("registration is already linked to a different enterprise")
    if db.get(Enterprise, eid) is None:
        raise NotFound("enterprise not found")
    if row.enterprise_id == eid:
        return
    row.enterprise_id, row.updated_at = eid, now
    db.flush()
    audit.record(
        db, actor, ACTION_LINK, RESOURCE, row.id,
        {"enterprise_id": str(eid), "actor": str(actor_id)}, now=now,
    )  # fmt: skip


def link_enterprise(
    db: Session, request_id, enterprise_id, actor: CentralUser, *, now: datetime | None = None
) -> RegistrationRequest:
    """Helper i vetëm për lidhjen e një kërkese approved/pending|failed me Enterprise ekzistues."""
    row = reg.get(db, request_id, for_update=True)
    if row.status != APPROVED:
        raise Conflict("only an approved registration can be linked to an enterprise")
    _link(db, row, enterprise_id, actor, now or utcnow())
    return row


def _uuid(value) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise Invalid("invalid enterprise id") from None


# --- Tx2 -----------------------------------------------------------------------------------------------------


def provision(
    db: Session,
    request_id,
    *,
    enterprise_id=None,
    actor: CentralUser | None = None,
    now: datetime | None = None,
) -> ProvisionResult:
    """Tx2 atomik (pa commit). Hedh `NotProvisionable` pa efekt, ose `ProvisioningError(code)` /
    përjashtim tjetër për të cilin thirrësi bën rollback dhe thërret `record_failure`."""
    now = now or utcnow()
    row = reg.get(db, request_id, for_update=True)
    if row.status != APPROVED:
        raise NotProvisionable(
            f"registration is {row.status}; only approved requests are provisioned"
        )
    if row.provisioning_status == PROVISIONED:  # idempotent: s'ka punë, s'ka audit, s'ka attempt
        return ProvisionResult(
            row.id, PROVISIONED, True, row.enterprise_id, attempts=row.provisioning_attempts
        )
    if row.provisioning_status not in (PENDING, FAILED):
        raise NotProvisionable("registration has no pending provisioning")
    if enterprise_id is not None:
        _link(db, row, enterprise_id, actor, now)  # njeri eksplicit; i rikthyeshëm me Tx2
    pids = list(db.scalars(select(RegistrationProduct.product_id).where(
        RegistrationProduct.request_id == row.id)))  # fmt: skip
    vs = policy.views(
        db, pids, lock=True
    )  # FOR SHARE: retire/policy paralel pret ose ka ndodhur para
    if not all(v.product_active for v in vs):
        raise ProvisioningError(PRODUCT_RETIRED)
    if not all(v.has_policy and v.self_registration_enabled for v in vs):
        raise ProvisioningError(POLICY_DISABLED)
    result = ProvisionResult(row.id, PROVISIONED)
    if row.enterprise_id is not None:  # i lidhur eksplicit nga admini
        ent = db.get(Enterprise, row.enterprise_id)
        if ent is None:
            raise ProvisioningError(ENTERPRISE_NOT_FOUND)
        if ent.status != EnterpriseStatus.ACTIVE.value:
            raise ProvisioningError(ENTERPRISE_SUSPENDED)
    else:  # Enterprise i ri: UUID kanonik nga Central; pa përputhje të paqarta me emër/email
        ent = enterprise_svc.create(db, row.enterprise_name, now=now)
        result.new_enterprise = True
        row.enterprise_id = ent.id
    result.enterprise_id = ent.id
    if result.new_enterprise:  # vetëm për Enterprise të sapokrijuar; një bump për klient
        result.auto_grants = service_auth.auto_grant_new_enterprise(db, ent.id, now=now)
    links = {
        rp.product_id: rp
        for rp in db.scalars(
            select(RegistrationProduct).where(RegistrationProduct.request_id == row.id)
        )
    }
    for v in sorted(vs, key=lambda x: x.code):
        existing = db.scalar(select(EnterpriseProduct).where(
            EnterpriseProduct.enterprise_id == ent.id, EnterpriseProduct.product_id == v.product_id))  # fmt: skip
        created = False
        if existing is None:
            try:
                existing, _ = asg.assign_product(db, ent.id, v.product_id, now=now)
                created = True
            except Conflict as e:
                existing = db.scalar(select(EnterpriseProduct).where(
                    EnterpriseProduct.enterprise_id == ent.id,
                    EnterpriseProduct.product_id == v.product_id))  # fmt: skip
                if existing is None:  # s'është garë mbi çiftin: enterprise/produkt jo i lejuar
                    msg = str(e)
                    raise ProvisioningError(
                        ENTERPRISE_SUSPENDED
                        if "enterprise is suspended" in msg
                        else PRODUCT_RETIRED
                        if "product is retired" in msg
                        else ASSIGNMENT_CONFLICT
                    ) from None
        if existing.status != AssignmentStatus.ACTIVE.value:
            raise ProvisioningError(ASSIGNMENT_EXISTS_SUSPENDED)  # kurrë aktivizim i fshehur
        links[v.product_id].assignment_id = existing.id
        result.assignments.append(
            {"product_code": v.code, "assignment_id": str(existing.id), "created": created}
        )
    row.provisioning_status, row.provisioning_error_code = PROVISIONED, None
    row.provisioning_attempts += 1
    row.updated_at = now
    db.flush()
    result.attempts = row.provisioning_attempts
    audit.record_system(
        db, label=LABEL, action=ACTION_PROVISION, resource_type=RESOURCE, resource_id=row.id,
        detail={"enterprise_id": str(ent.id), "new_enterprise": result.new_enterprise,
                "assignments": result.assignments, "auto_grants": result.auto_grants,
                "attempt": result.attempts}, now=now,
    )  # fmt: skip
    return result


# --- Tx3 -----------------------------------------------------------------------------------------------------


def record_failure(
    db: Session, request_id, code: str, *, now: datetime | None = None
) -> ProvisionResult:
    """Tx3 (pa commit): shënon `failed` + kodin e qëndrueshëm + attempts+1 pas rollback-ut të Tx2.
    Nëse kërkesa u provisionua njëkohësisht nga një thirrje tjetër ⇒ asgjë (sukses i mëparshëm i mbrojtur)."""
    if code not in ERROR_CODES:
        code = UNEXPECTED_ERROR
    now = now or utcnow()
    row = reg.get(db, request_id, for_update=True)
    if row.status != APPROVED or row.provisioning_status == PROVISIONED:
        return ProvisionResult(row.id, row.provisioning_status or row.status, row.provisioning_status == PROVISIONED,
                               row.enterprise_id, attempts=row.provisioning_attempts)  # fmt: skip
    row.provisioning_status, row.provisioning_error_code = FAILED, code
    row.provisioning_attempts += 1
    row.updated_at = now
    db.flush()
    audit.record_system(
        db, label=LABEL, action=ACTION_FAILED, resource_type=RESOURCE, resource_id=row.id,
        detail={"error_code": code, "attempt": row.provisioning_attempts}, now=now,
    )  # fmt: skip
    return ProvisionResult(row.id, FAILED, False, row.enterprise_id, error_code=code,
                           attempts=row.provisioning_attempts)  # fmt: skip


def error_code_of(exc: BaseException) -> str:
    if isinstance(exc, ProvisioningError):
        return exc.code
    if isinstance(exc, SQLAlchemyError):
        return DATABASE_ERROR
    if isinstance(exc, Conflict | NotFound):
        return PROVISIONING_CONFLICT
    return UNEXPECTED_ERROR


# --- orkestrimi Tx2 → Tx3 -------------------------------------------------------------------------------------


def run(
    factory: sessionmaker | Session,
    request_id,
    *,
    enterprise_id=None,
    actor: CentralUser | None = None,
    now: datetime | None = None,
) -> ProvisionResult:
    """Një përpjekje e plotë: Tx2; nëse dështon, rollback + Tx3 në sesion të ri. `NotProvisionable` dhe
    gabimet e hyrjes (Invalid/NotFound për lidhjen) kalojnë pa Tx3. Pa retry të brendshëm."""
    make = factory if callable(factory) and not isinstance(factory, Session) else None
    if make is None:
        raise TypeError("run() needs a sessionmaker (it manages its own transactions)")
    try:
        with make() as db:
            try:
                res = provision(db, request_id, enterprise_id=enterprise_id, actor=actor, now=now)
                db.commit()
                return res
            except BaseException:
                db.rollback()
                raise
    except (NotProvisionable, Invalid, NotFound):  # gabime hyrjeje/parakushtesh: pa Tx3
        raise
    except Exception as e:  # noqa: BLE001  (ProvisioningError, SQLAlchemyError, ose i papritur)
        code = error_code_of(e)
        log.warning(
            "provisioning failed request=%s code=%s type=%s", request_id, code, type(e).__name__
        )
        with make() as db:
            res = record_failure(db, request_id, code, now=now)
            db.commit()
        return res
