"""Kërkesat e regjistrimit (M8-a): submit idempotent + vendim manual (approve/reject). Vetëm Central.

Pa provisioning, pa auto-miratim, pa policy, pa HTTP, pa thirrje drejt Enterprise. Asnjë commit këtu
(transaksioni i thirrësit): ndryshimi i gjendjes dhe audit-i dalin ose zhduken bashkë.

SUBMIT. `submission_key` (opsional, çelësi i idempotencës i klientit) ≠ `access_token` (sekret i
gjeneruar nga serveri për leximin e statusit më vonë). Idempotenca është e skopuar: UNIQUE
(contact_email, submission_key) në DB është burimi i së vërtetës për garat.
  * kërkesë e re            → `SubmitResult(created=True, access_token=<sekreti, VETËM tani>)`
  * replay (e njëjta përmbajtje) → `SubmitResult(created=False, access_token=None)`: tokeni origjinal
    nuk rikuperohet (ruhet vetëm hash-i) dhe NUK lëshohet i ri;
  * i njëjti çelës me përmbajtje tjetër → Conflict.
Pa çelës: çdo submit krijon kërkesë të re (s'ka dedupe sipas emailit: i njëjti kontakt mund të ketë
kërkesa të ligjshme më vonë; kufijtë e abuzimit = M8-e).

Produktet: 1..MAX_PRODUCTS ID unike; secili duhet `active`, me politikë dhe `self_registration_enabled`
(M8-b; mungesa e politikës = i mbyllur). Çdo mospërputhje ⇒ i njëjti gabim i përgjithshëm
`products_unavailable`. Auto-miratim (M8-b): nëse TË GJITHA politikat janë `automatic` DHE gate-i
`CENTRAL_ALLOW_UNVERIFIED_AUTO_REGISTRATION` është true, kërkesa krijohet drejtpërdrejt si
approved/pending/automatic me audit `system:registration_auto_approval` në të njëjtin transaksion
(pa provisioning). Replay nuk auto-miraton dhe nuk audit-on për herë të dytë.
"""

import hashlib
import json
import re
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.central.core.errors import Conflict, Invalid, NotFound
from apps.central.core.timeutil import utcnow
from apps.central.models.product import Product
from apps.central.models.registration import (
    APPROVED,
    AUTOMATIC,
    FAILED,
    MANUAL,
    PENDING,
    PROVISIONED,
    REJECTED,
    SUBMITTED,
    RegistrationProduct,
    RegistrationRequest,
)
from apps.central.models.user import CentralUser
from apps.central.services import audit
from apps.central.services import registration_policy as policy
from apps.central.services.enterprises import normalize_name

MAX_PRODUCTS = 5
EMAIL_MAX = 254
CONTACT_NAME_MAX = 120
REASON_MAX = 500
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_EMAIL = re.compile(r"^[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+\.[^@\s\x00-\x1f\x7f]+$")
_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")
ACTION_APPROVE = "registration.approve"
ACTION_REJECT = "registration.reject"
RESOURCE = "registration_request"
UNAVAILABLE = "products_unavailable"
AUTO_LABEL = "system:registration_auto_approval"


@dataclass(frozen=True, slots=True)
class SubmitResult:
    request: RegistrationRequest
    created: bool
    access_token: str | None = field(default=None, repr=False)  # kurrë në repr/log


# --- normalizim ------------------------------------------------------------------------------------


def normalize_contact_email(value) -> str:
    """strip + lowercase; max 254; pa hapësira/kontroll; formë `a@b.c`. (E njëjta rregull si stafi i
    Central, e përsëritur këtu që regjistrimi të mos varet nga shërbimi i auth.)"""
    if not isinstance(value, str):
        raise Invalid("contact email must be a string")
    email = value.strip().lower()
    if len(email) > EMAIL_MAX or not _EMAIL.match(email):
        raise Invalid("invalid contact email")
    return email


def _contact_name(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise Invalid("contact name must be a string")
    name = value.strip()
    if not name:
        return None
    if len(name) > CONTACT_NAME_MAX or _CONTROL.search(name):
        raise Invalid(f"contact name must be at most {CONTACT_NAME_MAX} characters, no controls")
    return name


def _key(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _KEY.match(value):
        raise Invalid("submission key must be 8..64 characters of [A-Za-z0-9._:-]")
    return value


def _product_ids(values: Sequence) -> list[uuid.UUID]:
    if isinstance(values, str | bytes) or not isinstance(values, Sequence):
        raise Invalid("products must be a list of product ids")
    ids = []
    for v in values:
        if isinstance(v, uuid.UUID):
            ids.append(v)
            continue
        try:
            ids.append(uuid.UUID(str(v)))
        except ValueError:
            raise Invalid("invalid product id") from None
    if not 1 <= len(ids) <= MAX_PRODUCTS:
        raise Invalid(f"select between 1 and {MAX_PRODUCTS} products")
    if len(set(ids)) != len(ids):
        raise Invalid("duplicate products in the request")
    return ids


# --- token -------------------------------------------------------------------------------------------


def new_access_token() -> tuple[str, str]:
    """→ (token, sha256-hex). 256 bit rastësi; SHA-256 mjafton (jo fjalëkalim njeriu)."""
    token = secrets.token_urlsafe(32)
    return token, hash_access_token(token)


def hash_access_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_access_token(request: RegistrationRequest | None, token) -> bool:
    """Krahasim në kohë të qëndrueshme; asnjë përjashtim për hyrje të keqe (mungon/jo-string/bosh)."""
    candidate = hash_access_token(token) if isinstance(token, str) and token else "0" * 64
    stored = request.access_token_hash if request is not None else "0" * 64
    ok = secrets.compare_digest(candidate, stored)
    return ok and request is not None and isinstance(token, str) and bool(token)


# --- submit --------------------------------------------------------------------------------------------


def _fingerprint(name: str, contact: str | None, product_ids: list[uuid.UUID]) -> str:
    blob = json.dumps(
        {"enterprise_name": name, "contact_name": contact, "products": sorted(map(str, product_ids))},
        sort_keys=True, separators=(",", ":"),
    )  # fmt: skip
    return hashlib.sha256(blob.encode()).hexdigest()


def _eligible_views(db: Session, ids: list[uuid.UUID]) -> list[policy.PolicyView]:
    vs = policy.views(db, ids)
    if not all(v.eligible for v in vs):
        raise Invalid(
            UNAVAILABLE
        )  # mungon/retired/pa politikë/çaktivizuar: asnjë dallim te klienti
    return vs


def _existing(db: Session, email: str, key: str) -> RegistrationRequest | None:
    return db.scalar(
        select(RegistrationRequest).where(
            RegistrationRequest.contact_email == email, RegistrationRequest.submission_key == key
        )
    )


def _replay(row: RegistrationRequest, fingerprint: str) -> SubmitResult:
    if row.request_hash != fingerprint:
        raise Conflict("submission key was already used with a different request")
    return SubmitResult(row, created=False, access_token=None)


def submit(
    db: Session,
    *,
    enterprise_name: str,
    contact_email: str,
    product_ids: Sequence,
    contact_name: str | None = None,
    submission_key: str | None = None,
    now: datetime | None = None,
) -> SubmitResult:
    name = normalize_name(enterprise_name)
    email = normalize_contact_email(contact_email)
    contact = _contact_name(contact_name)
    key = _key(submission_key)
    ids = _product_ids(product_ids)
    fingerprint = _fingerprint(name, contact, ids)
    if key is not None and (row := _existing(db, email, key)) is not None:
        return _replay(row, fingerprint)  # replay: pa token, pa dublikim
    vs = _eligible_views(db, ids)
    automatic = policy.automatic_allowed() and all(v.approval_mode == AUTOMATIC for v in vs)
    token, token_hash = new_access_token()
    now = now or utcnow()
    row = RegistrationRequest(
        contact_email=email, contact_name=contact, enterprise_name=name, submission_key=key,
        request_hash=fingerprint, access_token_hash=token_hash, status=SUBMITTED,
        created_at=now, updated_at=now,
    )  # fmt: skip
    if automatic:  # vendimi automatik: gjendja e kërkesës llindet e miratuar (pa provisioning)
        row.status, row.decision_mode = APPROVED, AUTOMATIC
        row.decided_at, row.decided_by_id, row.decided_by_label = now, None, AUTO_LABEL
        row.provisioning_status = PENDING

    def insert() -> None:
        db.add(row)
        db.flush()
        for pid in ids:
            db.add(RegistrationProduct(request_id=row.id, product_id=pid, created_at=now))
        db.flush()
        if automatic:  # audit sistemi në të njëjtin transaksion: dështim ⇒ rollback i gjithçkaje
            audit.record_system(
                db, label=AUTO_LABEL, action=ACTION_APPROVE, resource_type=RESOURCE,
                resource_id=row.id, now=now,
                detail={"decision_mode": AUTOMATIC, "products": [v.snapshot() for v in vs],
                        "unverified_auto_registration_gate": True},
            )  # fmt: skip

    if key is None:  # pa çelës s'ka garë idempotence: pa savepoint
        insert()
        return SubmitResult(row, created=True, access_token=token)
    try:
        with db.begin_nested():  # gara: UNIQUE(contact_email, submission_key) vendos
            insert()
    except IntegrityError:
        if (winner := _existing(db, email, key)) is None:
            raise
        return _replay(winner, fingerprint)
    return SubmitResult(row, created=True, access_token=token)


# --- leximi ---------------------------------------------------------------------------------------------


def get(
    db: Session, request_id: uuid.UUID | str, *, for_update: bool = False
) -> RegistrationRequest:
    try:
        rid = request_id if isinstance(request_id, uuid.UUID) else uuid.UUID(str(request_id))
    except ValueError:
        raise NotFound("registration request not found") from None
    q = select(RegistrationRequest).where(RegistrationRequest.id == rid)
    if for_update:
        q = q.with_for_update().execution_options(populate_existing=True)
    row = db.scalar(q)
    if row is None:
        raise NotFound("registration request not found")
    return row


def requested_products(db: Session, request_id: uuid.UUID) -> list[Product]:
    return list(
        db.scalars(
            select(Product)
            .join(RegistrationProduct, RegistrationProduct.product_id == Product.id)
            .where(RegistrationProduct.request_id == request_id)
            .order_by(Product.code)
        )
    )


# --- vendimi manual ---------------------------------------------------------------------------------------


def _human(actor) -> CentralUser:
    if not isinstance(actor, CentralUser):
        raise Invalid("a human Central user is required for manual decisions")
    return actor


def approve(
    db: Session, request_id: uuid.UUID | str, actor: CentralUser, *, now: datetime | None = None
) -> RegistrationRequest:
    """submitted → approved (manual; provisioning_status=pending). approved ⇒ no-op; rejected ⇒
    Conflict. Produktet rivlerësohen: një produkt retired pas submit-it bllokon miratimin. Pa
    provisioning këtu (M8-c)."""
    actor = _human(actor)
    actor_id = (
        actor.id
    )  # lexo para ndryshimit: një actor i skaduar do shkaktonte autoflush të pjesshëm
    row = get(db, request_id, for_update=True)
    if row.status == APPROVED:
        return row
    if row.status == REJECTED:
        raise Conflict("registration request was rejected")
    ids = list(db.scalars(select(RegistrationProduct.product_id).where(
        RegistrationProduct.request_id == row.id)))  # fmt: skip
    vs = policy.views(db, ids)  # politika LIVE në momentin e vendimit (jo e ngrirë në submit)
    gone = sorted(v.code for v in vs if not v.eligible)
    if gone:
        raise Conflict(f"requested product no longer available: {', '.join(gone)}")
    now = now or utcnow()
    row.status, row.decision_mode = APPROVED, MANUAL
    row.decided_at, row.decided_by_id, row.decided_by_label = now, actor_id, None
    row.decision_reason = None
    row.provisioning_status, row.updated_at = PENDING, now
    db.flush()
    audit.record(
        db, actor, ACTION_APPROVE, RESOURCE, row.id,
        {"decision_mode": MANUAL, "products": [v.snapshot() for v in sorted(vs, key=lambda v: v.code)]},
        now=now,
    )  # fmt: skip
    return row


def _reason(value) -> str:
    if not isinstance(value, str):
        raise Invalid("a rejection reason is required")
    reason = value.strip()
    if not reason or len(reason) > REASON_MAX or _CONTROL.search(reason):
        raise Invalid(f"reason must be 1..{REASON_MAX} characters without control characters")
    return reason


def reject(
    db: Session,
    request_id: uuid.UUID | str,
    actor: CentralUser,
    reason: str,
    *,
    now: datetime | None = None,
) -> RegistrationRequest:
    """submitted → rejected. rejected ⇒ no-op. approved lejohet VETËM kur provisioning_status=failed
    (vendim njerëzor për ta mbyllur; asgjë s'është krijuar). approved pending / provisioned ⇒ Conflict
    (asnjë rollback shkatërrues). Dështimi i provisioning-ut kurrë s'e kthen vetë kërkesën në rejected."""
    actor = _human(actor)
    actor_id = actor.id  # (shih approve)
    reason = _reason(reason)
    row = get(db, request_id, for_update=True)
    if row.status == REJECTED:
        return row
    if row.status == APPROVED and row.provisioning_status != FAILED:
        state = "provisioned" if row.provisioning_status == PROVISIONED else "awaiting provisioning"
        raise Conflict(f"approved request is {state}; it cannot be rejected")
    before = {"status": row.status, "provisioning_status": row.provisioning_status}
    now = now or utcnow()
    row.status, row.decision_mode = REJECTED, MANUAL
    row.decided_at, row.decided_by_id, row.decided_by_label = now, actor_id, None
    row.decision_reason, row.updated_at = reason, now
    db.flush()
    audit.record(
        db, actor, ACTION_REJECT, RESOURCE, row.id, {"reason": reason, "from": before}, now=now
    )
    return row
