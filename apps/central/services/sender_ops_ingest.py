"""M11-D2: marrja e snapshot-it operacional `sender.ops.v1` nga Enterprise. Transport i pastër: ruan VETËM gjendjen e fundit për klientin e autentikuar.

- Identiteti i raportuesit = klienti i shërbimit i autentikuar (kurrë nga trupi); duhet të ketë të paktën një enterprise të autorizuar (përndryshe 403 `enterprise_not_authorized`).
- Idempotencë: i njëjti `snapshot_id` + e njëjta përmbajtje ⇒ `duplicate` (pa shkrim); i njëjti `snapshot_id` + përmbajtje tjetër ⇒ Conflict (409).
- Rend: snapshot me `generated_at` ≤ atë të ruajturin (me id tjetër) ⇒ `ignored_stale` (pranuar, pa mbishkrim); vetëm më i riu mbishkruan. Dërgimi jashtë rendit është i sigurt.
- Konkurrencë: rreshti i klientit kyçet FOR UPDATE; krijimi i parë paralel zgjidhet nga PK (IntegrityError ⇒ rilexim)."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.central.core.errors import Conflict, Invalid
from apps.central.core.timeutil import utcnow
from apps.central.models.sender import SenderOpsSnapshot
from packages.contracts.control_plane.sender import ops_v1 as v1

MAX_BODY_BYTES = v1.MAX_BODY_BYTES


@dataclass(frozen=True, slots=True)
class Ingested:
    status: str  # accepted | duplicate | ignored_stale
    snapshot_id: str


def parse(raw: object) -> v1.SenderOpsV1:
    try:
        return v1.SenderOpsV1.parse(raw)
    except v1.ContractError as e:
        raise Invalid(f"invalid sender ops snapshot: {e}") from e


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo else d.replace(tzinfo=v1.UTC)


def ingest(db: Session, client_pk, snap: v1.SenderOpsV1, now: datetime | None = None) -> Ingested:
    now = now or utcnow()
    sid = uuid.UUID(snap.snapshot_id)
    chash = snap.content_hash()
    row = db.scalar(
        select(SenderOpsSnapshot).where(SenderOpsSnapshot.client_pk == client_pk).with_for_update()
    )
    if row is None:
        try:
            with db.begin_nested():
                db.add(_new(client_pk, sid, snap, chash, now))
                db.flush()
            return Ingested("accepted", snap.snapshot_id)
        except IntegrityError:  # raportues paralel krijoi rreshtin e parë
            row = db.scalar(
                select(SenderOpsSnapshot)
                .where(SenderOpsSnapshot.client_pk == client_pk)
                .with_for_update()
            )
            if row is None:
                raise
    if row.snapshot_id == sid:
        if row.content_hash != chash:
            raise Conflict("snapshot_id was already used with different content")
        return Ingested("duplicate", snap.snapshot_id)
    if snap.generated_at <= _aware(row.generated_at):
        return Ingested("ignored_stale", snap.snapshot_id)
    row.snapshot_id, row.schema_version, row.generated_at, row.received_at = (
        sid,
        v1.SCHEMA,
        snap.generated_at,
        now,
    )
    row.source_revision, row.content_hash, row.payload = snap.source_revision, chash, snap.to_dict()
    db.flush()
    return Ingested("accepted", snap.snapshot_id)


def _new(client_pk, sid, snap, chash, now) -> SenderOpsSnapshot:
    return SenderOpsSnapshot(
        client_pk=client_pk, snapshot_id=sid, schema_version=v1.SCHEMA, generated_at=snap.generated_at, received_at=now,
        source_revision=snap.source_revision, content_hash=chash, payload=snap.to_dict(),
    )  # fmt: skip
