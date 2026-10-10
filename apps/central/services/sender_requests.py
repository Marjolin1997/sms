"""M10-S3: marrja e kërkesave të sender-ave nga Enterprise (`sender.request.v1`). Transport i pastër mbi autoritetin ekzistues (S1 `request_sender`/`resubmit`).

Semantika (e miratuar):
- **Dedupe sipas `operation_id`** (UNIQUE, append-only): i njëjti veprim + e njëjta ngarkesë ⇒ rezultati i ruajtur, ZERO efekt të ri (as vendim, as ngjarje `cp.sender.v1`).
  Ndryshim në ngarkesë me të njëjtin `operation_id` ⇒ Conflict. Ridërgimi i përdoruesit ka `operation_id` të ri ⇒ tranzicion i ri.
- **Identiteti**: (enterprise_id, external_ref) i pandryshueshëm. `request` me identitet tjetër nën të njëjtin external_ref ⇒ Conflict (S1). `resubmit` kërkon rreshtin ekzistues dhe të njëjtin
  (shtet, lloj, vlerë); mungesa ⇒ Conflict `sender_not_registered`, jo krijim i heshtur.
- **Rezultati i biznesit nuk është dështim transporti**: ridërgim mbi sender që është tashmë pending/approved në Central ⇒ `noop_*` (pranuar); politikë që refuzon/miraton automatikisht
  raportohet te `auto`. Gjendja autoritative udhëton te Enterprise VETËM nga `cp.sender.v1`.
- Serializim: kyç advisory sipas `operation_id` (PG) para leximit të dedupe-it ⇒ dy dërgime paralele të të njëjtit veprim s'prodhojnë efekt të dyfishtë; pastaj kyçet e S1 (fusha shared → rreshti FOR UPDATE)."""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.central.core.errors import Conflict, Invalid
from apps.central.models.sender import SenderRegistry, SenderRequestOperation
from apps.central.services import sender_identity as ident
from apps.central.services import senders
from packages.contracts.control_plane.sender import request_v1 as rv

ACTOR = "system:enterprise-request"
MAX_BODY_BYTES = rv.MAX_BODY_BYTES


@dataclass(frozen=True, slots=True)
class Processed:
    op: SenderRequestOperation
    duplicate: bool


def parse(raw: object) -> rv.SenderRequestV1:
    try:
        return rv.SenderRequestV1.parse(raw)
    except rv.ContractError as e:
        raise Invalid(f"invalid sender request: {e}") from e


def _op_lock(db: Session, operation_id: uuid.UUID) -> None:
    if db.get_bind().dialect.name != "postgresql":
        return
    key = int.from_bytes(
        hashlib.sha256(f"sender-request-op:{operation_id}".encode()).digest()[:8],
        "big",
        signed=True,
    )
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


def _prior(db: Session, req: rv.SenderRequestV1) -> SenderRequestOperation | None:
    prior = db.get(SenderRequestOperation, uuid.UUID(req.operation_id), populate_existing=True)
    if prior is None:
        return None
    if prior.request_hash != req.request_hash() or str(prior.enterprise_id) != req.enterprise_id:
        raise Conflict("operation_id was already used with a different request")
    return prior


def process(db: Session, req: rv.SenderRequestV1, *, now: datetime | None = None) -> Processed:
    """Pa commit (transaksioni i thirrësit). Kthen veprimin e ruajtur; `duplicate=True` nëse tashmë ishte pranuar."""
    oid, eid = uuid.UUID(req.operation_id), uuid.UUID(req.enterprise_id)
    _op_lock(db, oid)
    prior = _prior(db, req)
    if prior is not None:
        return Processed(prior, True)
    i = ident.identity(req.country, req.display_value)
    if i.kind != req.sender_kind or i.country != req.country:
        raise Invalid("sender_kind does not match the value (parity check failed)")
    if req.operation == "request":
        res = senders.request_sender(
            db, ACTOR, eid, req.external_ref, req.country, req.display_value, req.evidence_ref,
            source="enterprise", now=now,
        )  # fmt: skip
        row = res.sender
        outcome, auto = ("created", res.auto) if res.created else ("existing", "not_applicable")
    else:
        row = db.scalar(
            select(SenderRegistry).where(
                SenderRegistry.enterprise_id == eid, SenderRegistry.external_ref == req.external_ref
            )
        )
        if row is None:
            raise Conflict("sender_not_registered: resubmit refers to an unknown external_ref")
        row = senders._load(db, row.id)
        if (row.country, row.sender_kind, row.display_value) != (i.country, i.kind, i.display):
            raise Conflict("external_ref was already used with a different sender request")
        if row.current_status == "pending":
            outcome, auto = "noop_pending", "not_applicable"
        elif row.current_status == "approved":
            outcome, auto = "noop_approved", "not_applicable"
        else:
            res = senders.resubmit(db, ACTOR, row.id, req.evidence_ref, now=now)
            outcome, auto = "resubmitted", res.auto
    op = SenderRequestOperation(
        operation_id=oid, enterprise_id=eid, registry_id=row.id, external_ref=req.external_ref,
        operation=req.operation, request_hash=req.request_hash(), outcome=outcome, auto=auto,
        status_after=row.current_status, decision_id=row.current_decision_id,
    )  # fmt: skip
    try:
        with db.begin_nested():
            db.add(op)
            db.flush()
    except IntegrityError:
        again = _prior(db, req)
        if again is None:
            raise
        return Processed(again, True)
    return Processed(op, False)


def response(p: Processed) -> dict:
    o = p.op
    return {
        "status": "duplicate" if p.duplicate else "accepted",
        "operation_id": str(o.operation_id),
        "operation": o.operation,
        "outcome": o.outcome,
        "auto": o.auto,
        "registry_ref": str(o.registry_id),
        "external_ref": o.external_ref,
        "current_status": o.status_after,
        "decision_ref": None if o.decision_id is None else str(o.decision_id),
    }
