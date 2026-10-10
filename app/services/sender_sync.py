"""M10-S2: aplikuesi lokal i `cp.sender.v1` — projeksion i NDARË nga `SenderId` (asnjë mirror i statusit lokal, asnjë ndryshim i vendimeve/API lokale, asnjë efekt në submit/dispatch).

- Faqe e feed-it: validim i plotë (seq rritës > kursor ≤ `next_seq`, grupe të plota, kontrata) → aplikim ATOMIK në një savepoint nën kyçin e kursorit → kursori ecën vetëm pas suksesit.
  Një ngjarje e pavlefshme/konfliktuale ⇒ asgjë s'aplikohet, kursori qëndron (gabimi shënohet).
- `revision` per entitet: më i madh → apliko · i barabartë me të njëjtën përmbajtje → no-op · i barabartë me përmbajtje tjetër → `EventConflict` (fail-closed) · më i vogël → i vjetruar (injorohet).
- Snapshot: atomik; elementet që mungojnë bëhen `withdrawn` (gjendje, jo fshirje); epokë e re ⇒ zëvendësim pa kontroll revision. Pa kurrë mbivendosje të `SenderId`.
- Kyçi i vetëm i aplikimit: rreshti i kursorit `FOR UPDATE` (dy poller-a ⇒ serializim)."""

import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.timeutil import as_utc, utcnow
from app.models.sender_sync import SenderSyncCursor, SyncedSenderAuthorization, SyncedSenderPolicy
from app.services import sender_authorization as sa
from packages.contracts.control_plane.sender import v1

log = logging.getLogger("sms.sender.sync")
SLO_AGE_S = 900  # raportim vetëm (healthy ≤ 5 min · warning ≤ 15 min · critical më shumë)
ALERT_AGE_S = 300


class SenderSyncError(Exception):
    pass


class SnapshotRequired(SenderSyncError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class StaleSnapshot(SenderSyncError):
    pass


class ApplyError(SenderSyncError):
    pass


class EventConflict(ApplyError):
    """E njëjta `revision`, përmbajtje tjetër (ose çelës i miratuar i dyfishtë): fail-closed."""


class IncompleteGroup(ApplyError):
    """Faqja përmban një grup jo të plotë: gjendje e përzier e mundshme ⇒ refuzim."""


@dataclass(slots=True)
class BatchResult:
    applied: int = 0
    noop: int = 0
    stale: int = 0
    last_seq: int = 0


@dataclass(slots=True)
class SnapshotResult:
    applied: int = 0
    noop: int = 0
    stale: int = 0
    withdrawn: int = 0
    reset: bool = False
    drift: int = 0


@dataclass(frozen=True, slots=True)
class SnapshotV1:
    epoch: uuid.UUID
    authorization_generation: int
    snapshot_seq: int
    policies: list = field(default_factory=list)
    senders: list = field(default_factory=list)


# --- parsimi (i pastër) ------------------------------------------------------------------------------------------------------------


def parse_events(raw: Sequence[Any]) -> list[v1.SenderEventV1]:
    """TË GJITHA ose asgjë: një ngjarje e keqe ⇒ ContractError dhe asnjë aplikim."""
    return [v1.SenderEventV1.from_dict(r) for r in raw]


def parse_snapshot(d: Any) -> SnapshotV1:
    if not isinstance(d, dict) or d.get("schema") != v1.SCHEMA:
        raise v1.UnsupportedSchemaError("snapshot schema is not cp.sender.v1")
    keys = {"schema", "epoch", "authorization_generation", "snapshot_seq", "policies", "senders"}
    if d.keys() != keys:
        raise v1.ContractError(
            f"snapshot fields: unexpected {sorted(d.keys() - keys)}, missing {sorted(keys - d.keys())}"
        )
    try:
        epoch = uuid.UUID(str(d["epoch"]))
    except ValueError:
        raise v1.ContractError("snapshot epoch is not a UUID") from None
    gen, seq = d["authorization_generation"], d["snapshot_seq"]
    for name, val, lo in (("authorization_generation", gen, 1), ("snapshot_seq", seq, 0)):
        if isinstance(val, bool) or not isinstance(val, int) or val < lo:
            raise v1.ContractError(f"snapshot {name} is invalid")
    if not isinstance(d["policies"], list) or not isinstance(d["senders"], list):
        raise v1.ContractError("snapshot policies/senders must be lists")
    pol = [v1.SnapshotItemV1.from_dict(x) for x in d["policies"]]
    sen = [v1.SnapshotItemV1.from_dict(x) for x in d["senders"]]
    if any(i.event_type != v1.EVENT_POLICY for i in pol) or any(
        i.event_type != v1.EVENT_REGISTRY for i in sen
    ):
        raise v1.ContractError("snapshot item in the wrong list")
    if len({i.entity_id for i in pol}) != len(pol) or len({i.entity_id for i in sen}) != len(sen):
        raise v1.ContractError("snapshot has duplicate entities")
    return SnapshotV1(epoch, gen, seq, pol, sen)


# --- kursori -----------------------------------------------------------------------------------------------------------------------


def _insert_cursor_if_missing(db: Session) -> None:
    ins = (postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert)(
        SenderSyncCursor.__table__
    )
    db.execute(
        ins.values(
            id=1, last_seq=0, failure_count=0, gap_recoveries=0, drift_repairs=0
        ).on_conflict_do_nothing()
    )


def _lock_cursor(db: Session) -> SenderSyncCursor:
    q = (
        select(SenderSyncCursor)
        .where(SenderSyncCursor.id == 1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = db.scalar(q)
    if row is None:
        _insert_cursor_if_missing(db)
        row = db.scalar(q)
    assert row is not None
    return row


def get_cursor(db: Session) -> SenderSyncCursor:
    row = db.get(SenderSyncCursor, 1, populate_existing=True)
    if row is None:
        _insert_cursor_if_missing(db)
        row = db.get(SenderSyncCursor, 1, populate_existing=True)
    assert row is not None
    return row


def record_error(db: Session, message: str, now: datetime | None = None) -> None:
    cur = _lock_cursor(db)
    cur.last_error, cur.last_error_at, cur.failure_count = (
        message[:1000],
        now or utcnow(),
        (cur.failure_count or 0) + 1,
    )
    db.flush()


def count_gap_recovery(db: Session) -> None:
    cur = _lock_cursor(db)
    cur.gap_recoveries = (cur.gap_recoveries or 0) + 1
    db.flush()


# --- aplikimi i një entiteti -------------------------------------------------------------------------------------------------------


def _policy_fields(st: v1.PolicyStateV1) -> dict:
    return {
        "allowed": st.allowed,
        "requires_approval": st.requires_approval,
        "policy_id": uuid.UUID(st.policy_id),
        "effective_from": as_utc(st.effective_from),
    }


def _same_policy(row: SyncedSenderPolicy, rev: int, st: v1.PolicyStateV1) -> bool:
    return (row.allowed, row.requires_approval, row.policy_id, as_utc(row.effective_from), row.policy_revision) == (
        st.allowed, st.requires_approval, uuid.UUID(st.policy_id), as_utc(st.effective_from), rev)  # fmt: skip


def _reg_fields(st: v1.RegistryStateV1) -> dict:
    return {
        "enterprise_id": uuid.UUID(st.enterprise_id), "external_ref": st.external_ref, "country": st.country, "sender_kind": st.sender_kind, "display_value": st.display_value,
        "norm_value": st.norm_value, "status": st.status, "approved_key": st.approved_key, "decision_id": uuid.UUID(st.decision_id), "decision": st.decision,
        "decided_at": as_utc(st.decided_at), "policy_source": st.policy_source, "policy_id": None if st.policy_id is None else uuid.UUID(st.policy_id), "policy_revision": st.policy_revision,
    }  # fmt: skip


def _same_reg(row: SyncedSenderAuthorization, rev: int, st: v1.RegistryStateV1) -> bool:
    f = _reg_fields(st)
    return row.cp_revision == rev and all(
        getattr(row, k) == v if k != "decided_at" else as_utc(row.decided_at) == v
        for k, v in f.items()
    )


def _apply_policy(
    db: Session, rev: int, st: v1.PolicyStateV1, seq: int, now: datetime, *, force: bool = False
) -> str:
    row = db.scalar(
        select(SyncedSenderPolicy).where(
            SyncedSenderPolicy.country == st.country,
            SyncedSenderPolicy.sender_kind == st.sender_kind,
        )
    )
    if row is None:
        db.add(
            SyncedSenderPolicy(
                country=st.country,
                sender_kind=st.sender_kind,
                policy_revision=rev,
                cp_seq=seq,
                updated_at=now,
                **_policy_fields(st),
            )
        )
        db.flush()
        return "applied"
    if not force:
        if rev < row.policy_revision:
            return "stale"
        if rev == row.policy_revision:
            if not _same_policy(row, rev, st):
                raise EventConflict(
                    f"policy {st.country}/{st.sender_kind} revision {rev} arrived with different content"
                )
            if row.projection_state == "active":
                return "noop"
    for k, v in _policy_fields(st).items():
        setattr(row, k, v)
    row.policy_revision, row.cp_seq, row.projection_state, row.withdrawn_at, row.updated_at = (
        rev,
        seq,
        "active",
        None,
        now,
    )
    db.flush()
    return "applied"


def _apply_registry(
    db: Session,
    entity_id: str,
    rev: int,
    st: v1.RegistryStateV1,
    seq: int,
    now: datetime,
    *,
    force: bool = False,
) -> str:
    rid = uuid.UUID(entity_id)
    row = db.scalar(
        select(SyncedSenderAuthorization).where(SyncedSenderAuthorization.registry_id == rid)
    )
    f = _reg_fields(st)
    if row is None:
        db.add(
            SyncedSenderAuthorization(
                registry_id=rid, cp_revision=rev, cp_seq=seq, updated_at=now, **f
            )
        )
        db.flush()
        return "applied"
    if not force:
        if rev < row.cp_revision:
            return "stale"
        if rev == row.cp_revision:
            if not _same_reg(row, rev, st):
                raise EventConflict(
                    f"sender {entity_id} revision {rev} arrived with different content"
                )
            if row.projection_state == "active":
                return "noop"
    key = f.pop("approved_key")
    if (
        row.approved_key is not None and key != row.approved_key
    ):  # liro çelësin para se tjetër ta marrë (rend deterministik brenda faqes)
        row.approved_key = None
        db.flush()
    for k, v in f.items():
        setattr(row, k, v)
    row.approved_key = key
    row.cp_revision, row.cp_seq, row.projection_state, row.withdrawn_at, row.updated_at = (
        rev,
        seq,
        "active",
        None,
        now,
    )
    db.flush()
    return "applied"


# --- faqja e feed-it ---------------------------------------------------------------------------------------------------------------


def apply_feed_batch(
    db: Session,
    *,
    epoch: uuid.UUID,
    authorization_generation: int,
    events: Sequence[v1.SenderEventV1],
    next_seq: int,
    latest_seq: int | None = None,
    now: datetime | None = None,
) -> BatchResult:
    now = now or utcnow()
    res = BatchResult()
    with db.begin_nested():
        cur = _lock_cursor(db)
        if cur.epoch is None or cur.authorization_generation is None:
            raise SnapshotRequired("no_snapshot")
        if cur.epoch != epoch:
            raise SnapshotRequired("epoch_mismatch")
        if cur.authorization_generation != authorization_generation:
            raise SnapshotRequired("authorization_changed")
        if next_seq < cur.last_seq:
            raise ApplyError(f"next_seq {next_seq} is behind the cursor {cur.last_seq}")
        prev = cur.last_seq
        sizes: dict = {}
        declared: dict = {}
        for ev in events:
            if not prev < ev.seq <= next_seq:
                raise ApplyError(
                    f"event seq {ev.seq} is not in ({prev}, {next_seq}] in strictly increasing order"
                )
            prev = ev.seq
            sizes[ev.group_id] = sizes.get(ev.group_id, 0) + 1
            declared[ev.group_id] = ev.group_size
        for gid, n in sizes.items():
            if declared[gid] != n:
                raise IncompleteGroup(f"group {gid} has {n} of {declared[gid]} events in this page")
        for ev in events:
            if ev.event_type == v1.EVENT_POLICY:
                r = _apply_policy(db, ev.revision, ev.data, ev.seq, now)
            else:
                try:
                    r = _apply_registry(db, ev.entity_id, ev.revision, ev.data, ev.seq, now)
                except IntegrityError as e:
                    raise EventConflict(
                        "approved sender key conflicts with another projected row"
                    ) from e
            if r == "applied":
                res.applied += 1
            elif r == "noop":
                res.noop += 1
            else:
                res.stale += 1
        cur.last_seq, cur.last_success_at, cur.last_error = next_seq, now, None
        if latest_seq is not None:
            cur.latest_central_seq = latest_seq
        res.last_seq = next_seq
        db.flush()
    return res


# --- snapshot ----------------------------------------------------------------------------------------------------------------------


def apply_snapshot(db: Session, snap: SnapshotV1, *, now: datetime | None = None) -> SnapshotResult:
    now = now or utcnow()
    res = SnapshotResult()
    with db.begin_nested():
        cur = _lock_cursor(db)
        had = cur.epoch is not None and cur.authorization_generation is not None
        same_epoch = had and cur.epoch == snap.epoch
        if same_epoch and snap.snapshot_seq < cur.last_seq:
            raise StaleSnapshot(
                f"snapshot_seq {snap.snapshot_seq} is older than the cursor {cur.last_seq}"
            )
        force = not same_epoch
        res.reset = had and not same_epoch
        check_drift = (
            same_epoch
            and cur.authorization_generation == snap.authorization_generation
            and snap.snapshot_seq == cur.last_seq
        )

        def tally(r: str) -> None:
            setattr(res, r, getattr(res, r) + 1)
            if r == "applied" and check_drift:
                res.drift += 1

        seen_pol = {(i.data.country, i.data.sender_kind) for i in snap.policies}
        seen_reg = {uuid.UUID(i.entity_id) for i in snap.senders}
        for i in (
            snap.policies
        ):  # politikat para (global), pastaj regjistri pa miratime, pastaj miratimet
            tally(_apply_policy(db, i.revision, i.data, snap.snapshot_seq, now, force=force))
        for p in db.scalars(
            select(SyncedSenderPolicy).where(SyncedSenderPolicy.projection_state == "active")
        ):
            if (p.country, p.sender_kind) not in seen_pol:
                p.projection_state, p.withdrawn_at, p.updated_at = "withdrawn", now, now
                res.withdrawn += 1
                res.drift += 1 if check_drift else 0
        db.flush()
        for r in db.scalars(
            select(SyncedSenderAuthorization).where(
                SyncedSenderAuthorization.projection_state == "active"
            )
        ):
            if r.registry_id not in seen_reg:
                r.projection_state, r.approved_key, r.withdrawn_at, r.updated_at = (
                    "withdrawn",
                    None,
                    now,
                    now,
                )
                res.withdrawn += 1
                res.drift += (
                    1 if (check_drift and False) else 0
                )  # largimi nga fusha e autorizuar është legjitim (jo drift)
        db.flush()
        for pass_approved in (False, True):
            for i in snap.senders:
                if (i.data.status == "approved") != pass_approved:
                    continue
                try:
                    tally(
                        _apply_registry(
                            db, i.entity_id, i.revision, i.data, snap.snapshot_seq, now, force=force
                        )
                    )
                except IntegrityError as e:
                    raise EventConflict(
                        "approved sender key conflicts with another projected row"
                    ) from e
        cur.epoch, cur.authorization_generation = snap.epoch, snap.authorization_generation
        cur.last_seq = snap.snapshot_seq if not same_epoch else max(cur.last_seq, snap.snapshot_seq)
        cur.latest_central_seq = max(cur.latest_central_seq or 0, snap.snapshot_seq)
        cur.snapshot_seq, cur.last_snapshot_at, cur.last_success_at, cur.last_error = (
            snap.snapshot_seq,
            now,
            now,
            None,
        )
        if res.drift:
            cur.drift_repairs = (cur.drift_repairs or 0) + res.drift
            log.warning(
                "sender sync drift repaired by snapshot: %d item(s) differed although the cursor was current",
                res.drift,
            )
        db.flush()
    return res


def sync_age_seconds(cur: SenderSyncCursor, now: datetime | None = None) -> float | None:
    if cur.last_success_at is None:
        return None
    return max(0.0, (as_utc(now or utcnow()) - as_utc(cur.last_success_at)).total_seconds())


# --- lexime lokale (pa efekt në submit/dispatch në S2) -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SyncedPolicyView:
    country: str
    sender_kind: str
    source: str  # explicit | default
    allowed: bool
    requires_approval: bool
    policy_id: uuid.UUID | None = None
    policy_revision: int | None = None
    effective_from: datetime | None = None


@dataclass(frozen=True, slots=True)
class SyncedAuthorization:
    found: bool
    enterprise_id: uuid.UUID | None = None
    country: str = ""
    canonical_key: str = ""
    status: str | None = None
    allowed: bool = False
    registry_id: uuid.UUID | None = None
    decision_id: uuid.UUID | None = None
    policy_source: str | None = None
    policy_id: uuid.UUID | None = None
    policy_revision: int | None = None
    cp_revision: int | None = None


def get_synced_policy(db: Session, country: str, sender_kind: str) -> SyncedSenderPolicy | None:
    return db.scalar(
        select(SyncedSenderPolicy).where(
            SyncedSenderPolicy.country == country.upper(),
            SyncedSenderPolicy.sender_kind == sender_kind,
            SyncedSenderPolicy.projection_state == "active",
        )
    )


def effective_synced_policy(db: Session, country: str, sender_kind: str) -> SyncedPolicyView:
    """Politika eksplicite e sinkronizuar nëse ekziston, përndryshe parazgjedhja virtuale e Central (`allowed=true, requires_approval=true`). Jo e lidhur me submit në S2."""
    country = country.upper()
    p = get_synced_policy(db, country, sender_kind)
    if p is None:
        return SyncedPolicyView(country, sender_kind, "default", True, True)
    return SyncedPolicyView(
        country,
        sender_kind,
        "explicit",
        p.allowed,
        p.requires_approval,
        p.policy_id,
        p.policy_revision,
        as_utc(p.effective_from),
    )


def get_synced_sender_authorization(
    db: Session, enterprise_id: uuid.UUID, country: str, value: str
) -> SyncedAuthorization:
    """Gjendja e sinkronizuar për (enterprise, shtet, sender) — një SELECT, case-insensitive me normalizimin e S0. Jo e lidhur me submit në S2."""
    country = country.upper()
    norm = sa.norm_of(value)
    rows = db.scalars(
        select(SyncedSenderAuthorization).where(
            SyncedSenderAuthorization.enterprise_id == enterprise_id,
            SyncedSenderAuthorization.country == country,
            SyncedSenderAuthorization.norm_value == norm,
            SyncedSenderAuthorization.projection_state == "active",
        )
    ).all()
    key = sa.canonical_key(country, norm)
    if not rows:
        return SyncedAuthorization(False, enterprise_id, country, key)
    r = sorted(rows, key=lambda x: (x.status != "approved", x.id))[0]
    return SyncedAuthorization(
        True,
        enterprise_id,
        country,
        key,
        r.status,
        r.status == "approved",
        r.registry_id,
        r.decision_id,
        r.policy_source,
        r.policy_id,
        r.policy_revision,
        r.cp_revision,
    )
