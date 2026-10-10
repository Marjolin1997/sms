"""M10-S2: feed-i `cp.sender.v1` në Central — publikimi transaksional (outbox), mapper-i drejt kontratës, feed-i i ndryshimeve dhe snapshot-i koherent. Vetëm lexim te konsumatori.

Publikimi: çdo funksion publik i `senders` (politikë/kërkesë/miratim/refuzim/revokim/ridërgim) mbledh gjendjet e ndryshuara dhe, NË FUND, `publish` i shkruan si ngjarje me `seq` të njëpasnjëshëm
në të njëjtin transaksion (numërues i kyçur `FOR UPDATE` ⇒ asnjë seq fantazmë pas rollback, asnjë humbje pas commit). Grup = ngjarjet e një transaksioni: politika e re para (rend deterministik),
pastaj regjistri sipas id ⇒ konsumatori e aplikon grupin të plotë (asnjë gjendje e përzier politikë-e-re + miratim-i-vjetër).
Autorizimi: ngjarjet e regjistrit filtrohen sipas bashkësisë së autorizuar të klientit (`service_client_enterprises`, `auth_generation`); politikat janë globale (çdo klient me skop `sender:read`).
`group.size` te feed-i = numri i ngjarjeve të grupit të dukshme për klientin (grupi nuk ndahet kurrë nëpër faqe)."""

import uuid
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from apps.central.core.timeutil import utcnow
from apps.central.models.sender import (
    CountrySenderPolicy,
    SenderDecision,
    SenderRegistry,
    SenderSyncOutbox,
    SenderSyncSequence,
)
from apps.central.services import service_auth
from apps.central.services.sync_feed import SyncApiError
from packages.contracts.control_plane.sender import v1

_SEQ = SenderSyncSequence.__table__
MAX_LIMIT = 500
PENDING = "sender_sync_pending"
_after_boundary_hook = None  # vijë testi: thirret pas leximit të kufirit të snapshot-it


class SenderFeedError(SyncApiError):
    status = 409
    code = "sender_feed_error"
    action = "snapshot"


class EpochMismatch(SenderFeedError):
    status, code = 409, "sender_epoch_mismatch"


class AuthorizationChanged(SenderFeedError):
    status, code = 409, "sender_authorization_changed"


class CursorAhead(SenderFeedError):
    status, code = 409, "sender_cursor_ahead"


class CursorExpired(SenderFeedError):
    status, code = 410, "sender_cursor_expired"


# --- mapper-i drejt kontratës ----------------------------------------------------------------------------------------------------


def policy_state(p: CountrySenderPolicy) -> v1.PolicyStateV1:
    return v1.PolicyStateV1(
        p.country, p.sender_kind, p.allowed, p.requires_approval, str(p.id), p.effective_from
    )


def registry_state(r: SenderRegistry, d: SenderDecision) -> v1.RegistryStateV1:
    return v1.RegistryStateV1(
        str(r.enterprise_id), r.external_ref, r.country, r.sender_kind, r.display_value, r.norm_value, r.current_status, r.approved_key, str(d.id), d.decision,
        d.decided_at, d.policy_source, None if d.policy_id is None else str(d.policy_id), d.policy_revision,
    )  # fmt: skip


# --- publikimi -------------------------------------------------------------------------------------------------------------------


def note_policy(db: Session, p: CountrySenderPolicy) -> None:
    db.info.setdefault(PENDING, []).append(("policy", p))


def note_registry(db: Session, r: SenderRegistry, d: SenderDecision) -> None:
    db.info.setdefault(PENDING, []).append(("registry", r, d))


def discard(db: Session) -> None:
    db.info.pop(PENDING, None)


def publish(db: Session, now: datetime | None = None) -> int:
    """Shkruan ngjarjet e mbledhura (një grup) me seq të njëpasnjëshëm; kthen numrin. Thirret NË FUND të mutacionit, brenda të njëjtit transaksion."""
    pend = db.info.pop(PENDING, None)
    if not pend:
        return 0
    now = now or utcnow()
    pols: dict = {}
    regs: dict = {}
    for item in pend:  # fitron e fundit per entitet
        if item[0] == "policy":
            pols[(item[1].country, item[1].sender_kind)] = item[1]
        else:
            regs[item[1].id] = (item[1], item[2])
    events: list[tuple] = []
    for (_c, _k), p in sorted(pols.items()):
        st = policy_state(p)
        events.append(
            (
                v1.EVENT_POLICY,
                None,
                uuid.UUID(v1.policy_entity_id(p.country, p.sender_kind)),
                p.revision,
                st.to_dict(),
            )
        )
    for rid in sorted(regs, key=str):
        r, d = regs[rid]
        events.append(
            (v1.EVENT_REGISTRY, r.enterprise_id, r.id, d.seq, registry_state(r, d).to_dict())
        )
    n = len(events)
    db.execute(select(_SEQ.c.epoch).where(_SEQ.c.id == 1).with_for_update())  # kyç deri në commit
    last = int(
        db.execute(
            _SEQ.update()
            .where(_SEQ.c.id == 1)
            .values(last_seq=_SEQ.c.last_seq + n)
            .returning(_SEQ.c.last_seq)
        ).scalar_one()
    )
    gid = uuid.uuid4()
    for i, (etype, eid, entity, rev, payload) in enumerate(events):
        db.add(
            SenderSyncOutbox(
                seq=last - n + 1 + i,
                event_id=uuid.uuid4(),
                enterprise_id=eid,
                event_type=etype,
                entity_id=entity,
                revision=rev,
                group_id=gid,
                payload=payload,
                created_at=now,
            )
        )
    db.flush()
    return n


# --- feed-i ----------------------------------------------------------------------------------------------------------------------


def read_state(db: Session) -> tuple[uuid.UUID, int, int]:
    row = db.execute(
        select(_SEQ.c.epoch, _SEQ.c.floor_seq, _SEQ.c.last_seq).where(_SEQ.c.id == 1)
    ).one()
    return row.epoch, int(row.floor_seq), int(row.last_seq)


def state(db: Session, client_pk) -> dict:
    epoch, _floor, latest = read_state(db)
    generation, _allowed = service_auth.allowed_enterprises(db, client_pk)
    return {"epoch": str(epoch), "authorization_generation": generation, "latest_seq": latest}


def _event_dict(row: SenderSyncOutbox, group_size: int) -> dict:
    d = {
        "schema": v1.SCHEMA, "event_id": str(row.event_id), "seq": row.seq, "event_type": row.event_type,
        "enterprise_id": None if row.enterprise_id is None else str(row.enterprise_id),
        "entity": {"type": v1.ENTITY_BY_EVENT[row.event_type], "id": str(row.entity_id)}, "revision": int(row.revision),
        "group": {"id": str(row.group_id), "size": group_size}, "occurred_at": v1.format_ts(row.created_at), "data": row.payload,
    }  # fmt: skip
    return v1.SenderEventV1.from_dict(
        d
    ).to_dict()  # validim + normalizim në dalje (kurrë ngjarje e pavlefshme në rrjet)


def changes(
    db: Session, client_pk, after_seq: int, limit: int, epoch: uuid.UUID, generation: int
) -> dict:
    limit = max(1, min(limit, MAX_LIMIT))
    feed_epoch, floor_seq, latest = read_state(db)
    if epoch != feed_epoch:
        raise EpochMismatch("sender feed epoch changed; a full snapshot is required")
    current_generation, allowed = service_auth.allowed_enterprises(db, client_pk)
    if generation != current_generation:
        raise AuthorizationChanged("authorization changed; a full snapshot is required")
    if after_seq > latest:
        raise CursorAhead("cursor is ahead of the sender feed; a full snapshot is required")
    if after_seq < floor_seq:
        raise CursorExpired(
            "cursor is older than the retained history; a full snapshot is required"
        )
    visible = SenderSyncOutbox.enterprise_id.is_(None)
    if allowed:
        visible = visible | SenderSyncOutbox.enterprise_id.in_(allowed)
    rows = list(
        db.scalars(
            select(SenderSyncOutbox)
            .where(visible, SenderSyncOutbox.seq > after_seq, SenderSyncOutbox.seq <= latest)
            .order_by(SenderSyncOutbox.seq)
            .limit(limit + 1)
        )
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    if (
        has_more and page and rows[limit].group_id == page[-1].group_id
    ):  # grupi nuk ndahet: plotëso me pjesën e mbetur
        tail = list(
            db.scalars(
                select(SenderSyncOutbox)
                .where(
                    visible,
                    SenderSyncOutbox.group_id == page[-1].group_id,
                    SenderSyncOutbox.seq > page[-1].seq,
                    SenderSyncOutbox.seq <= latest,
                )
                .order_by(SenderSyncOutbox.seq)
            )
        )
        page += tail
        more = db.scalar(
            select(SenderSyncOutbox.seq)
            .where(visible, SenderSyncOutbox.seq > page[-1].seq, SenderSyncOutbox.seq <= latest)
            .limit(1)
        )
        has_more = more is not None
    sizes: dict = {}
    for r in page:
        sizes[r.group_id] = sizes.get(r.group_id, 0) + 1
    next_seq = page[-1].seq if has_more else max(latest, after_seq)
    return {
        "epoch": str(feed_epoch), "authorization_generation": current_generation, "events": [_event_dict(r, sizes[r.group_id]) for r in page],
        "next_seq": next_seq, "latest_seq": latest, "oldest_available_seq": floor_seq + 1, "has_more": has_more,
    }  # fmt: skip


# --- snapshot --------------------------------------------------------------------------------------------------------------------


@contextmanager
def snapshot_session(engine: Engine):
    """Transaksion i vetëm snapshot-i: REPEATABLE READ + vetëm-lexim në PostgreSQL (numëruesi dhe rreshtat ndryshojnë gjithmonë në të njëjtin commit)."""
    conn = engine.connect()
    try:
        if engine.dialect.name == "postgresql":
            conn = conn.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True
            )
        with Session(bind=conn) as session:
            yield session
    finally:
        conn.close()


def snapshot(db: Session, client_pk) -> dict:
    epoch, _floor, last_seq = read_state(db)  # kufiri lexohet i pari, brenda snapshot-it
    if _after_boundary_hook is not None:
        _after_boundary_hook()
    generation, allowed = service_auth.allowed_enterprises(db, client_pk)
    latest = (
        select(
            CountrySenderPolicy.country,
            CountrySenderPolicy.sender_kind,
            func.max(CountrySenderPolicy.revision).label("r"),
        )
        .group_by(CountrySenderPolicy.country, CountrySenderPolicy.sender_kind)
        .subquery()
    )
    policies = []
    for p in db.scalars(
        select(CountrySenderPolicy)
        .join(
            latest,
            (latest.c.country == CountrySenderPolicy.country)
            & (latest.c.sender_kind == CountrySenderPolicy.sender_kind)
            & (latest.c.r == CountrySenderPolicy.revision),
        )
        .order_by(CountrySenderPolicy.country, CountrySenderPolicy.sender_kind)
    ):
        policies.append(
            v1.SnapshotItemV1(
                v1.EVENT_POLICY,
                None,
                v1.policy_entity_id(p.country, p.sender_kind),
                p.revision,
                policy_state(p),
            ).to_dict()
        )
    senders = []
    if allowed:
        q = (
            select(SenderRegistry, SenderDecision)
            .join(SenderDecision, SenderDecision.id == SenderRegistry.current_decision_id)
            .where(SenderRegistry.enterprise_id.in_(allowed))
            .order_by(SenderRegistry.created_at, SenderRegistry.id)
        )
        for r, d in db.execute(q):
            senders.append(
                v1.SnapshotItemV1(
                    v1.EVENT_REGISTRY, str(r.enterprise_id), str(r.id), d.seq, registry_state(r, d)
                ).to_dict()
            )
    return {
        "schema": v1.SCHEMA,
        "epoch": str(epoch),
        "authorization_generation": generation,
        "snapshot_seq": last_seq,
        "policies": policies,
        "senders": senders,
    }
