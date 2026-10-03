"""M7-d: aplikuesi lokal i gjendjes së Control Plane (cp.v1). VETËM aplikim lokal: pa HTTP, pa
polling, pa çelës shërbimi, pa enforcement në rrugën SMS/Email (M7-e/g).

Kontrata e konsumuar është `packages.contracts.control_plane.v1` (jo e kopjuar).

Rregullat (të miratuara në M7-a/M7-c):
  * Kursori: epoch/generation NULL = kërkohet snapshot i plotë; feed pa snapshot refuzohet.
  * `seq` është VETËM kursor i feed-it; freskia e entitetit vendoset nga `revision`
    (> lokal: aplikohet; == : no-op; < : e injoruar, e raportuar `stale`).
  * `next_seq` merret nga mbështjellësi i përgjigjes (mund të kalojë seq-in e fundit të
    ngjarjeve; boshllëqet 100,107,129 pranohen: s'ka kërkesë për vijueshmëri).
  * Çdo funksion punon në transaksionin e thirrësit dhe NUK bën commit; ndryshimet dhe kursori
    janë në të njëjtën njësi (savepoint): dështim ⇒ asgjë nuk aplikohet, kursori s'lëviz.
  * Rendi i kyçjeve: kursori → enterprise → entitlement (kursori serializon çdo aplikim).
  * Fushat e Central-it: `status`, `short_name` ← `name`, `cp_revision`. KURRË `legal_name`,
    `owner_ref`, identiteti tenant, profili i faturimit, AccountPlan.
  * Ngjarje/snapshot për enterprise që s'ekziston lokalisht: kalohet (`unknown_enterprise`);
    nuk krijohet tenant pa `owner_ref`; snapshot-i i ardhshëm e rimerr.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.core.timeutil import as_utc, utcnow
from app.models.control_plane import (
    ENTITLEMENT_WITHDRAWN,
    CpCursor,
    Entitlement,
)
from app.models.enterprise import Enterprise
from app.services import audit
from packages.contracts.control_plane import v1
from packages.contracts.control_plane.v1 import (
    ContractError,
    ControlPlaneEventV1,
    EnterpriseProductStateV1,
    EnterpriseStateV1,
)

SYSTEM_ACTOR = "system:control_plane_sync"
ACTION_ENTERPRISE = "control_plane.enterprise.apply"
ACTION_ENTITLEMENT = "control_plane.entitlement.apply"
ACTION_SNAPSHOT = "control_plane.snapshot.apply"

ALERT_AGE_S = 300  # ~5 min: alarm i vjetërsisë (vetëm monitorim)
SLO_AGE_S = 900  # 15 min: SLO për gjendjen që ndikon pezullimin

APPLIED, NOOP, STALE, UNKNOWN_ENTERPRISE = "applied", "noop", "stale", "unknown_enterprise"


class SnapshotRequired(Exception):
    """Feed-i s'mund të aplikohet: duhet snapshot i plotë (s'ka reset automatik)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason  # no_snapshot | epoch_mismatch | generation_mismatch


class StaleSnapshot(Exception):
    """Snapshot-i është më i vjetër se kursori lokal i së njëjtës epokë."""


class ApplyError(Exception):
    """Batch/snapshot jo i aplikueshëm (kursor i pavlefshëm, konflikt identiteti, ...)."""


# --- parsimi (cp.v1 + mbështjellësi i snapshot-it) ---------------------------------------------


@dataclass(frozen=True, slots=True)
class SnapshotV1:
    epoch: uuid.UUID
    authorization_generation: int
    snapshot_seq: int
    enterprises: tuple[tuple[int, EnterpriseStateV1], ...]
    assignments: tuple[tuple[int, EnterpriseProductStateV1], ...]


def _int(v: Any, field_: str, *, minimum: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
        raise ContractError(f"{field_} must be an integer >= {minimum}")
    return v


def _item(raw: Any, entity_type: str, state_cls) -> tuple[int, Any]:
    if not isinstance(raw, dict):
        raise ContractError("snapshot item must be an object")
    entity = raw.get("entity")
    if not isinstance(entity, dict) or entity.get("type") != entity_type:
        raise ContractError(f"snapshot item entity.type must be {entity_type!r}")
    state = state_cls.from_dict(raw.get("data"))
    entity_id = state.id if entity_type == v1.ENTITY_ENTERPRISE else state.assignment_id
    if entity.get("id") != entity_id or raw.get("enterprise_id") != (
        state.id if entity_type == v1.ENTITY_ENTERPRISE else state.enterprise_id
    ):
        raise ContractError("snapshot item ids do not match its data")
    return _int(raw.get("revision"), "revision", minimum=1), state


def parse_snapshot(d: Any) -> SnapshotV1:
    """Valido përgjigjen e snapshot-it (dict i JSON-it). Çdo parregullsi ⇒ ContractError."""
    if not isinstance(d, dict):
        raise ContractError("snapshot must be an object")
    try:
        epoch = uuid.UUID(str(d["epoch"]))
        gen = _int(d["authorization_generation"], "authorization_generation", minimum=0)
        seq = _int(d["snapshot_seq"], "snapshot_seq", minimum=0)
        ents, asgs = d["enterprises"], d["assignments"]
    except (KeyError, ValueError) as e:
        raise ContractError(f"snapshot envelope invalid: {e!r}") from e
    if not isinstance(ents, list) or not isinstance(asgs, list):
        raise ContractError("snapshot enterprises/assignments must be arrays")
    enterprises = tuple(_item(x, v1.ENTITY_ENTERPRISE, EnterpriseStateV1) for x in ents)
    assignments = tuple(
        _item(x, v1.ENTITY_ENTERPRISE_PRODUCT, EnterpriseProductStateV1) for x in asgs
    )
    ids = [s.id for _, s in enterprises]
    if len(set(ids)) != len(ids):
        raise ContractError("duplicate enterprise in snapshot")
    aids = [s.assignment_id for _, s in assignments]
    keys = [(s.enterprise_id, s.product_code) for _, s in assignments]
    if len(set(aids)) != len(aids) or len(set(keys)) != len(keys):
        raise ContractError("duplicate assignment in snapshot")
    if {s.enterprise_id for _, s in assignments} - set(ids):
        raise ContractError("assignment references an enterprise outside the snapshot")
    return SnapshotV1(epoch, gen, seq, enterprises, assignments)


def parse_events(raw_events: Sequence[Any]) -> list[ControlPlaneEventV1]:
    """Parso TË GJITHA ngjarjet para çdo aplikimi: një e keqe (skemë/tip i panjohur, fushë e
    pavlefshme) ⇒ ContractError dhe asgjë nuk aplikohet; asnjë ngjarje nuk anashkalohet."""
    return [ControlPlaneEventV1.from_dict(r) for r in raw_events]


# --- kursori -----------------------------------------------------------------------------------


def _insert_cursor_if_missing(db: Session) -> None:
    dialect = db.get_bind().dialect.name
    ins = (postgresql.insert if dialect == "postgresql" else sqlite.insert)(CpCursor.__table__)
    db.execute(ins.values(id=1, last_seq=0).on_conflict_do_nothing())


def _lock_cursor(db: Session) -> CpCursor:
    q = (
        select(CpCursor)
        .where(CpCursor.id == 1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = db.scalar(q)
    if row is None:  # migrimi e krijon; create_all (teste/dev) jo
        _insert_cursor_if_missing(db)
        row = db.scalar(q)
    assert row is not None
    return row


def get_cursor(db: Session) -> CpCursor:
    """Lexim i kursorit (pa kyçje)."""
    row = db.get(CpCursor, 1, populate_existing=True)
    if row is None:
        _insert_cursor_if_missing(db)
        row = db.get(CpCursor, 1, populate_existing=True)
    assert row is not None
    return row


# --- aplikimi i një entiteti -------------------------------------------------------------------


def _lock_enterprise(db: Session, eid: str) -> Enterprise | None:
    return db.scalar(
        select(Enterprise)
        .where(Enterprise.id == uuid.UUID(eid))
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _apply_enterprise(db, rev: int, st: EnterpriseStateV1, *, force: bool, ref: dict) -> str:
    ent = _lock_enterprise(db, st.id)
    if ent is None:
        return UNKNOWN_ENTERPRISE
    if not force:
        if rev == ent.cp_revision:
            return NOOP
        if rev < ent.cp_revision:
            return STALE
    before = {"status": ent.status, "short_name": ent.short_name, "cp_revision": ent.cp_revision}
    ent.status, ent.short_name, ent.cp_revision = st.status, st.name, rev
    ent.updated_at = utcnow()
    audit.system_event(
        db, SYSTEM_ACTOR, ACTION_ENTERPRISE, "enterprise", ent.id,
        {"from": before, "to": {"status": st.status, "cp_revision": rev}, **ref},
    )  # fmt: skip
    return APPLIED


def _apply_entitlement(
    db, rev: int, st: EnterpriseProductStateV1, *, force: bool, ref: dict
) -> str:
    eid = uuid.UUID(st.enterprise_id)
    if _lock_enterprise(db, st.enterprise_id) is None:
        return UNKNOWN_ENTERPRISE
    aid = uuid.UUID(st.assignment_id)
    row = db.scalar(
        select(Entitlement)
        .where(Entitlement.assignment_id == aid)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        clash = db.scalar(
            select(Entitlement.assignment_id).where(
                Entitlement.enterprise_id == eid, Entitlement.product_code == st.product_code
            )
        )
        if clash is not None:
            raise ApplyError(
                f"product {st.product_code!r} already has assignment {clash} for this enterprise"
            )
        db.add(
            Entitlement(
                enterprise_id=eid, assignment_id=aid, product_id=uuid.UUID(st.product_id),
                product_code=st.product_code, channel=st.channel, status=st.status, revision=rev,
                rate_limit_per_min=st.rate_limit_per_min,
            )
        )  # fmt: skip
        before = None
    else:
        if row.enterprise_id != eid:
            raise ApplyError("assignment moved to a different enterprise")
        withdrawn = row.status == ENTITLEMENT_WITHDRAWN
        if not force:
            if rev < row.revision or (rev == row.revision and not withdrawn):
                return STALE if rev < row.revision else NOOP
        before = {"status": row.status, "revision": row.revision,
                  "rate_limit_per_min": row.rate_limit_per_min}  # fmt: skip
        row.product_id, row.product_code = uuid.UUID(st.product_id), st.product_code
        row.channel, row.status, row.revision = st.channel, st.status, rev
        row.rate_limit_per_min = st.rate_limit_per_min
        row.updated_at = utcnow()
    audit.system_event(
        db, SYSTEM_ACTOR, ACTION_ENTITLEMENT, "entitlement", aid,
        {"from": before,
         "to": {"status": st.status, "revision": rev, "rate_limit_per_min": st.rate_limit_per_min},
         "enterprise_id": st.enterprise_id, "product_code": st.product_code, **ref},
    )  # fmt: skip
    return APPLIED


def _apply_event(db: Session, ev: ControlPlaneEventV1, *, force: bool = False) -> str:
    ref = {"seq": ev.seq, "event_id": ev.event_id}
    if isinstance(ev.data, EnterpriseStateV1):
        return _apply_enterprise(db, ev.revision, ev.data, force=force, ref=ref)
    return _apply_entitlement(db, ev.revision, ev.data, force=force, ref=ref)


def apply_event(db: Session, event: ControlPlaneEventV1) -> str:
    """Primitiv: aplikon NJË ngjarje sipas `revision` dhe NUK prek kursorin (rruga e prodhimit
    është `apply_feed_batch`). → applied | noop | stale | unknown_enterprise."""
    with db.begin_nested():
        _lock_cursor(db)
        return _apply_event(db, event)


# --- snapshot ----------------------------------------------------------------------------------


@dataclass(slots=True)
class SnapshotResult:
    enterprises_applied: int = 0
    enterprises_unchanged: int = 0
    entitlements_applied: int = 0
    entitlements_unchanged: int = 0
    entitlements_withdrawn: int = 0
    skipped_unknown_enterprise: int = 0
    reset: bool = False  # epokë e re: gjendja u zëvendësua pa kufizim nga revision
    full_scope: bool = True
    entitlements_out_of_scope: int = 0  # withdrawn sepse enterprise-i s'është më i autorizuar
    enterprises_out_of_scope: int = 0


def apply_snapshot(
    db: Session, snap: SnapshotV1, *, now: datetime | None = None, full_scope: bool = True
) -> SnapshotResult:
    """Aplikon një snapshot, atomikisht.

    `full_scope=True` (parazgjedhja): snapshot pa `?enterprise_id=` = TËRË bashkësia e autorizuar e
    kredencialit. Vetëm ai mund të (a) lëvizë kursorin dhe (b) rakordojë bashkësinë e synuar.
    `full_scope=False` (snapshot i pjesshëm `?enterprise_id=`): aplikon vetëm entitetet e tij,
    NUK prek kursorin dhe NUK nxjerr përfundime për enterprise-et e tjera; kërkon epokë dhe
    generation të njëjta me kursorin (përndryshe `SnapshotRequired`).

    * E njëjta epokë: çdo entitet kalon nga rregulli i `revision` (përsëritje = no-op; më i ri
      përditëson; `snapshot_seq` < kursor ⇒ `StaleSnapshot`, vetëm full).
    * Epokë e re (Central u rivendos): zëvendësim pa kontroll revision; kursori rinis.
    * Entitlement lokal i enterprise-it në snapshot por pa assignment në të ⇒ `withdrawn`.
    * FULL: enterprise me `cp_revision` > 0 (dikur i menaxhuar nga Central) që MUNGON nga snapshot-i
      ka dalë nga fusha e autorizimit të kredencialit ⇒ entitlement-et e tij bëhen `withdrawn`
      (arsye `out_of_authorization_scope`): nuk konsiderohen më autoritet aktual i Central.
      `sms_enterprises` dhe historiku i entitlement-eve NUK fshihen; `status`/`cp_revision` të
      enterprise-it mbeten si gjendja e fundit e njohur. Rikthimi në fushë (snapshot ose
      ngjarje me revision ≥) i rikthen entitlement-et. Enterprise-et që s'kanë qenë kurrë të
      menaxhuar (`cp_revision` = 0) nuk preken.
    """
    now = now or utcnow()
    res = SnapshotResult(full_scope=full_scope)
    with db.begin_nested():
        cur = _lock_cursor(db)
        if full_scope:
            if cur.epoch == snap.epoch and snap.snapshot_seq < cur.last_seq:
                raise StaleSnapshot(
                    f"snapshot_seq {snap.snapshot_seq} < local cursor {cur.last_seq}"
                )
            force = res.reset = cur.epoch is not None and cur.epoch != snap.epoch
        else:
            if cur.epoch is None:
                raise SnapshotRequired("no_snapshot")
            if cur.epoch != snap.epoch:
                raise SnapshotRequired("epoch_mismatch")
            if cur.authorization_generation != snap.authorization_generation:
                raise SnapshotRequired("generation_mismatch")
            force = False
        ref = {"snapshot_seq": snap.snapshot_seq, "epoch": str(snap.epoch)}
        present: set[uuid.UUID] = set()
        for rev, st in snap.enterprises:
            out = _apply_enterprise(db, rev, st, force=force, ref=ref)
            if out == UNKNOWN_ENTERPRISE:
                res.skipped_unknown_enterprise += 1
                continue
            present.add(uuid.UUID(st.id))
            if out == APPLIED:
                res.enterprises_applied += 1
            else:
                res.enterprises_unchanged += 1
        kept: set[uuid.UUID] = set()
        for rev, st in snap.assignments:
            if uuid.UUID(st.enterprise_id) not in present:
                continue
            kept.add(uuid.UUID(st.assignment_id))
            if _apply_entitlement(db, rev, st, force=force, ref=ref) == APPLIED:
                res.entitlements_applied += 1
            else:
                res.entitlements_unchanged += 1
        if present:
            gone = db.scalars(
                select(Entitlement)
                .where(
                    Entitlement.enterprise_id.in_(present),
                    Entitlement.status != ENTITLEMENT_WITHDRAWN,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            ).all()
            for row in gone:
                if row.assignment_id in kept:
                    continue
                _withdraw(db, row, "absent_from_snapshot", ref, now)
                res.entitlements_withdrawn += 1
        if full_scope:
            snap_ids = {uuid.UUID(st.id) for _, st in snap.enterprises}
            orphans = db.scalars(
                select(Entitlement)
                .join(Enterprise, Enterprise.id == Entitlement.enterprise_id)
                .where(
                    Enterprise.cp_revision > 0,
                    Entitlement.enterprise_id.not_in(snap_ids),
                    Entitlement.status != ENTITLEMENT_WITHDRAWN,
                )
                .with_for_update(of=Entitlement)
                .execution_options(populate_existing=True)
            ).all()
            for row in orphans:
                _withdraw(db, row, "out_of_authorization_scope", ref, now)
            res.entitlements_out_of_scope = len(orphans)
            res.enterprises_out_of_scope = len({r.enterprise_id for r in orphans})
            cur.epoch, cur.authorization_generation = snap.epoch, snap.authorization_generation
            cur.last_seq, cur.last_snapshot_at, cur.last_success_at = snap.snapshot_seq, now, now
        audit.system_event(
            db, SYSTEM_ACTOR, ACTION_SNAPSHOT, "control_plane", "snapshot",
            {**ref, "authorization_generation": snap.authorization_generation,
             "full_scope": full_scope,
             "enterprises": len(snap.enterprises), "assignments": len(snap.assignments),
             "reset": res.reset, "withdrawn": res.entitlements_withdrawn,
             "out_of_scope_entitlements": res.entitlements_out_of_scope,
             "skipped_unknown_enterprise": res.skipped_unknown_enterprise},
        )  # fmt: skip
    return res


def _withdraw(db: Session, row: Entitlement, reason: str, ref: dict, now: datetime) -> None:
    audit.system_event(
        db, SYSTEM_ACTOR, ACTION_ENTITLEMENT, "entitlement", row.assignment_id,
        {"from": {"status": row.status, "revision": row.revision},
         "to": {"status": ENTITLEMENT_WITHDRAWN, "revision": row.revision},
         "enterprise_id": str(row.enterprise_id), "reason": reason, **ref},
    )  # fmt: skip
    row.status, row.updated_at = ENTITLEMENT_WITHDRAWN, now


def sync_age_seconds(cursor: CpCursor, now: datetime | None = None) -> float | None:
    """Mosha e sinkronizimit të suksesshëm të fundit (None = kurrë). Vetëm për monitorim:
    NUK shkakton asnjë çaktivizim automatik (fail-static)."""
    if cursor.last_success_at is None:
        return None
    return (as_utc(now or utcnow()) - as_utc(cursor.last_success_at)).total_seconds()


# --- feed --------------------------------------------------------------------------------------


@dataclass(slots=True)
class BatchResult:
    applied: int = 0
    noop: int = 0
    stale: int = 0
    skipped_unknown_enterprise: int = 0
    last_seq: int = 0
    outcomes: list[tuple[int, str]] = field(default_factory=list)


def apply_feed_batch(
    db: Session,
    *,
    epoch: uuid.UUID,
    authorization_generation: int,
    events: Sequence[ControlPlaneEventV1],
    next_seq: int,
    now: datetime | None = None,
) -> BatchResult:
    """Aplikon një faqe të feed-it dhe e çon kursorin te `next_seq` të mbështjellësit, atomikisht.

    `next_seq` mund të kalojë seq-in e fundit të ngjarjeve (ngjarje të filtruara) ose faqja mund
    të jetë bosh. Kërkesa: seq-et rriten rreptësisht, janë > kursor dhe ≤ `next_seq`, dhe
    `next_seq` ≥ kursor (kurrë prapa). Epoka/generation ndryshe nga lokalet ⇒ SnapshotRequired
    (asgjë s'aplikohet, kursori s'lëviz)."""
    now = now or utcnow()
    res = BatchResult()
    with db.begin_nested():
        cur = _lock_cursor(db)
        if cur.epoch is None or cur.authorization_generation is None:
            raise SnapshotRequired("no_snapshot")
        if cur.epoch != epoch:
            raise SnapshotRequired("epoch_mismatch")
        if cur.authorization_generation != authorization_generation:
            raise SnapshotRequired("generation_mismatch")
        if isinstance(next_seq, bool) or not isinstance(next_seq, int) or next_seq < cur.last_seq:
            raise ApplyError(f"next_seq {next_seq!r} is behind the local cursor {cur.last_seq}")
        prev = cur.last_seq
        for ev in events:
            if ev.seq <= prev or ev.seq > next_seq:
                raise ApplyError(
                    f"event seq {ev.seq} out of order/range (after {prev}, next {next_seq})"
                )
            prev = ev.seq
        for ev in events:
            out = _apply_event(db, ev)
            res.outcomes.append((ev.seq, out))
            if out == APPLIED:
                res.applied += 1
            elif out == NOOP:
                res.noop += 1
            elif out == STALE:
                res.stale += 1
            else:
                res.skipped_unknown_enterprise += 1
        cur.last_seq, cur.last_success_at = next_seq, now
        res.last_seq = next_seq
    return res
