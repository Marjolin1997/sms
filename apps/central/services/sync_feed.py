"""Feed-i i ndryshimeve dhe snapshot-i i plotë (vetëm lexim; Central s'ruan gjendje per konsumator).

**Feed:** `after_seq = N` kthen `seq` në (N, latest_seq] për enterprise-et e autorizuara, `seq ASC`, nga
`sync_outbox` + mapper `cp.v1` (kurrë nga tabelat e biznesit). `latest_seq` lexohet NJË herë në fillim
(vlera e commit-uar e numëruesit): meqë numëruesi rritet në të njëjtin tx me outbox-in dhe seq alokohen
në rend commit-i, çdo `seq <= latest_seq` është i commit-uar; `seq > latest_seq` filtrohet. Kursori i
ri (`next_seq`) = `seq` i fundit i kthyer nëse ka faqe të tjera, përndryshe `latest_seq`: kapërcimi i
`seq` të enterprise-eve të tjera është i sigurt SHUMË pse bashkësia e autorizuar ndryshon vetëm me
`auth_generation` (konsumatori e dërgon dhe merr snapshot kur ndryshon) dhe me `epoch`.
Kontrollet e rendit: epoka → generation → kursori (i ardhshëm/skaduar). Pa gjysmë-histori në heshtje.

**Snapshot:** një transaksion REPEATABLE READ vetëm-lexim (PostgreSQL). Numëruesi (`last_seq`) dhe
rreshtat e entiteteve ndryshojnë gjithmonë në të njëjtin commit, ndaj një snapshot i vetëm i DB-së i
sheh të dyja në të njëjtin çast: gjendja = të gjitha ndryshimet me `seq <= snapshot_seq`, asnjë më shumë.
"""

import uuid
from contextlib import contextmanager

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from apps.central.core.errors import CentralError
from apps.central.models.enterprise import Enterprise
from apps.central.models.enterprise_product import EnterpriseProduct
from apps.central.models.product import Product
from apps.central.models.sync import SyncOutbox, SyncSequence
from apps.central.services import service_auth, sync_contract
from packages.contracts.control_plane import v1

_SEQ = SyncSequence.__table__
MAX_LIMIT = 500

# Vijë test: thirret pas leximit të kufirit të snapshot-it (prova e konkurrencës); None në prodhim.
_after_boundary_hook = None


class SyncApiError(CentralError):
    status = 409
    code = "sync_error"
    action = "snapshot"


class EpochMismatch(SyncApiError):
    status, code = 409, "sync_epoch_mismatch"


class AuthorizationChanged(SyncApiError):
    status, code = 409, "sync_authorization_changed"


class CursorAhead(SyncApiError):
    status, code = 409, "sync_cursor_ahead"


class CursorExpired(SyncApiError):
    status, code = 410, "sync_cursor_expired"


def read_state(db: Session) -> tuple[uuid.UUID, int, int]:
    """→ (epoch, floor_seq, last_seq) në një pyetje të vetme."""
    row = db.execute(
        select(_SEQ.c.epoch, _SEQ.c.floor_seq, _SEQ.c.last_seq).where(_SEQ.c.id == 1)
    ).one()
    return row.epoch, int(row.floor_seq), int(row.last_seq)


def changes(
    db: Session, client_pk, after_seq: int, limit: int, epoch: uuid.UUID, generation: int
) -> dict:
    limit = max(1, min(limit, MAX_LIMIT))
    feed_epoch, floor_seq, latest = read_state(db)
    if epoch != feed_epoch:
        raise EpochMismatch("feed epoch changed; a full snapshot is required")
    current_generation, allowed = service_auth.allowed_enterprises(db, client_pk)
    if generation != current_generation:
        raise AuthorizationChanged("authorization changed; a full snapshot is required")
    if after_seq > latest:
        raise CursorAhead("cursor is ahead of the feed; a full snapshot is required")
    if after_seq < floor_seq:
        raise CursorExpired(
            "cursor is older than the retained history; a full snapshot is required"
        )
    rows: list[SyncOutbox] = []
    if allowed:
        rows = list(
            db.scalars(
                select(SyncOutbox)
                .where(SyncOutbox.enterprise_id.in_(allowed), SyncOutbox.seq > after_seq,
                       SyncOutbox.seq <= latest)
                .order_by(SyncOutbox.seq)
                .limit(limit + 1)
            )
        )  # fmt: skip
    has_more = len(rows) > limit
    page = rows[:limit]
    next_seq = page[-1].seq if has_more else max(latest, after_seq)
    return {
        "epoch": str(feed_epoch),
        "authorization_generation": current_generation,
        "events": [sync_contract.to_event(r).to_dict() for r in page],
        "next_seq": next_seq,
        "latest_seq": latest,
        "oldest_available_seq": floor_seq + 1,
        "has_more": has_more,
    }


@contextmanager
def snapshot_session(engine: Engine):
    """Transaksion i vetëm snapshot-i: REPEATABLE READ + vetëm-lexim në PostgreSQL."""
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


def snapshot(db: Session, client_pk, enterprise_id: uuid.UUID | None = None) -> dict:
    """Gjendja aktuale e enterprise-eve të autorizuara + kufiri `snapshot_seq` (në të njëjtin snapshot)."""
    epoch, _floor, last_seq = read_state(db)  # kufiri lexohet i pari, brenda snapshot-it
    if _after_boundary_hook is not None:
        _after_boundary_hook()
    generation, allowed = service_auth.allowed_enterprises(db, client_pk)
    if enterprise_id is not None:
        allowed = allowed & {enterprise_id}
    enterprises, assignments = [], []
    if allowed:
        for e in db.scalars(
            select(Enterprise)
            .where(Enterprise.id.in_(allowed))
            .order_by(Enterprise.created_at, Enterprise.id)
        ):
            state = v1.EnterpriseStateV1(str(e.id), e.name, e.status)
            enterprises.append({"entity": {"type": v1.ENTITY_ENTERPRISE, "id": str(e.id)},
                                "enterprise_id": str(e.id), "revision": int(e.revision),
                                "data": state.to_dict()})  # fmt: skip
        q = (
            select(EnterpriseProduct, Product)
            .join(Product, Product.id == EnterpriseProduct.product_id)
            .where(EnterpriseProduct.enterprise_id.in_(allowed))
            .order_by(EnterpriseProduct.created_at, EnterpriseProduct.id)
        )
        for ep, p in db.execute(q):
            state = v1.EnterpriseProductStateV1(
                str(ep.id), str(ep.enterprise_id), str(p.id), p.code, p.channel, ep.status
            )
            assignments.append({"entity": {"type": v1.ENTITY_ENTERPRISE_PRODUCT, "id": str(ep.id)},
                                "enterprise_id": str(ep.enterprise_id), "revision": int(ep.revision),
                                "data": state.to_dict()})  # fmt: skip
    return {
        "epoch": str(epoch),
        "authorization_generation": generation,
        "snapshot_seq": last_seq,
        "enterprises": enterprises,
        "assignments": assignments,
    }


def rotate_epoch(db: Session) -> uuid.UUID:
    """Procedurë eksplicite (restore/reseed i qëllimshëm): epokë e re → konsumatorët bëjnë snapshot."""
    value = uuid.uuid4()
    db.execute(_SEQ.update().where(_SEQ.c.id == 1).values(epoch=value))
    return value
