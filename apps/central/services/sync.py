"""Alokimi transaksional i `seq` dhe shkrimi i outbox-it (M7-b1). Pa rrjet, pa dërgim.

Rendi i kyçjeve (parandalon deadlock-un): numëruesi global `seq` FILLON, pastaj rreshti i entitetit.
Ndryshim real: kyç `sync_sequence` → kyç/rilexo entitetin → rishiko (no-op → dil pa alokuar `seq`)
→ ndrysho + `revision += 1` → rrit numëruesin → shto rresht outbox. Gjithçka në transaksionin e
thirrësit; rollback i tij heq ndryshimin, `revision`, rritjen e numëruesit dhe outbox-in bashkë.
Transaksioni tjetër që do `seq` pret kyçjen deri në commit, ndaj `seq` N është i dukshëm para N+1
(kursor `after_seq` i sigurt). Volumi i shkrimeve administrative është i ulët: serializimi pranohet.
Rregull: çdo shkrues i entiteteve të sync-ut kalon nga këto funksione, jo SQL/ORM i drejtpërdrejtë.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.core.timeutil import utcnow
from apps.central.models.enterprise import Enterprise
from apps.central.models.enterprise_product import EnterpriseProduct
from apps.central.models.product import Product
from apps.central.models.sync import SyncOutbox, SyncSequence

ENTITY_ENTERPRISE = "enterprise"
ENTITY_ASSIGNMENT = "enterprise_product"
# Emra të përkohshëm: finalizohen te kontrata `cp.v1` (M7-b2).
EVENT_ENTERPRISE = "enterprise.upserted"
EVENT_ASSIGNMENT = "enterprise_product.upserted"

_SEQ = SyncSequence.__table__


def lock_sequence(db: Session) -> None:
    """Kyç numëruesin global (deri në fund të tx); thirret PARA kyçjes së entitetit."""
    row = db.execute(select(_SEQ.c.last_seq).where(_SEQ.c.id == 1).with_for_update()).first()
    if row is None:
        raise RuntimeError("sync_sequence singleton row is missing (run migrations)")


def lock_entity(db: Session, obj) -> None:
    """Kyç numëruesin, pastaj kyç dhe rilexon rreshtin e entitetit."""
    lock_sequence(db)
    db.refresh(obj, with_for_update=True)


def _next_seq(db: Session) -> int:
    stmt = (
        _SEQ.update()
        .where(_SEQ.c.id == 1)
        .values(last_seq=_SEQ.c.last_seq + 1)
        .returning(_SEQ.c.last_seq)
    )
    return int(db.execute(stmt).scalar_one())


def enterprise_payload(e: Enterprise) -> dict:
    return {"enterprise_id": str(e.id), "name": e.name, "status": e.status}


def assignment_payload(ep: EnterpriseProduct, product: Product) -> dict:
    """Snapshot i ngrirë; `code`/`channel` të produktit janë të pandryshueshme (të sigurta)."""
    return {
        "assignment_id": str(ep.id), "enterprise_id": str(ep.enterprise_id),
        "product": {"id": str(product.id), "code": product.code, "channel": product.channel},
        "status": ep.status,
    }  # fmt: skip


def emit(
    db: Session,
    *,
    entity_type: str,
    entity_id,
    enterprise_id,
    revision: int,
    event_type: str,
    payload: dict,
    now: datetime | None = None,
) -> SyncOutbox:
    """Shton një rresht outbox me `seq` të ri; VETËM pas `lock_sequence` në të njëjtin tx."""
    row = SyncOutbox(
        seq=_next_seq(db), enterprise_id=enterprise_id, entity_type=entity_type,
        entity_id=entity_id, revision=revision, event_type=event_type, payload=payload,
        created_at=now or utcnow(),
    )  # fmt: skip
    db.add(row)
    db.flush()
    return row
