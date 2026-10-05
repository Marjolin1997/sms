"""M9-e: aplikuesi i snapshot-it `cp.pricing.v1` në cache-in lokal. Pa HTTP/JWT (transport te `control_plane_client`).

NJË snapshot = NJË transaksion atomik: valido (kontrata e verifikon `snapshot_hash` dhe `content_hash` per version ⇒ snapshot i
paplotë/i prishur s'aktivizohet) → kyç `sms_pricing_state` → shto rreshtat e rinj të pandryshueshëm (libra, versione + rregulla që s'ekzistojnë,
caktimet e këtij snapshot-i) → `active→retired` për versionet e tërhequra → pointer-i. Lexuesit (motori i çmimit) shohin ose snapshot-in e
mëparshëm të plotë ose të riun të plotë. Replay i të njëjtit (epoch, revision, generation, hash) = no-op; revision më e vogël brenda të njëjtës
epokë = i vjetruar (injorohet). Çdo mospërputhje e përmbajtjes së një versioni ekzistues (Central s'duhet ta bëjë kurrë) ⇒ ApplyError, asgjë s'aktivizohet.
Dështim ⇒ cache-i i fundit i plotë mbetet në përdorim (fail-static); `last_error` regjistrohet."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app.core.timeutil import as_utc, utcnow
from app.models.pricing import (
    PricingAssignment,
    PricingBook,
    PricingRule,
    PricingSnapshot,
    PricingState,
    PricingVersion,
)
from packages.contracts.control_plane.pricing import v1 as pv

APPLIED, NOOP, STALE = "applied", "noop", "stale"


class ApplyError(Exception):
    """Snapshot që s'mund të aplikohet në mënyrë të sigurt (përmbajtje e kundërt me atë të ruajtur)."""


@dataclass(slots=True)
class ApplyResult:
    outcome: str
    revision: int
    new_versions: int = 0
    new_rules: int = 0
    retired: int = 0


def _insert_state_if_missing(db: Session) -> None:
    ins = (postgresql.insert if db.get_bind().dialect.name == "postgresql" else sqlite.insert)(
        PricingState.__table__
    )
    db.execute(ins.values(id=1).on_conflict_do_nothing())


def get_state(db: Session, *, lock: bool = False) -> PricingState:
    q = select(PricingState).where(PricingState.id == 1).execution_options(populate_existing=True)
    row = db.scalar(q.with_for_update() if lock else q)
    if row is None:
        _insert_state_if_missing(db)
        row = db.scalar(q.with_for_update() if lock else q)
    assert row is not None
    return row


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def apply_snapshot(
    db: Session, snap: pv.PricingSnapshotV1, *, now: datetime | None = None
) -> ApplyResult:
    """Atomik në transaksionin e thirrësit (që bën commit). Hedh `ApplyError` pa ndryshuar asgjë."""
    now = as_utc(now or utcnow())
    d = snap.doc
    state = get_state(db, lock=True)
    epoch, rev, gen = uuid.UUID(d["epoch"]), d["revision"], d["authorization_generation"]
    if state.active_snapshot_id is not None:
        if (state.epoch, state.revision, state.authorization_generation, state.snapshot_hash) == (
            epoch,
            rev,
            gen,
            d["snapshot_hash"],
        ):
            state.last_success_at, state.last_error, state.last_error_at = now, None, None
            return ApplyResult(NOOP, rev)
        if state.epoch == epoch and state.authorization_generation == gen and rev < state.revision:
            return ApplyResult(STALE, rev)
    res = ApplyResult(APPLIED, rev)
    snap_row = PricingSnapshot(id=uuid.uuid4(), epoch=epoch, revision=rev, authorization_generation=gen,
                               snapshot_hash=d["snapshot_hash"], received_at=now)  # fmt: skip
    db.add(snap_row)
    db.flush()
    for b in d["books"]:
        bid = uuid.UUID(b["book_id"])
        book = db.get(PricingBook, bid)
        if book is None:
            db.add(PricingBook(id=bid, code=b["code"], currency=b["currency"], created_at=now))
            db.flush()
        elif (book.code, book.currency) != (b["code"], b["currency"]):
            raise ApplyError(
                f"book {bid} changed code/currency in Central (immutable): refusing the snapshot"
            )
        for v in b["versions"]:
            vid = uuid.UUID(v["version_id"])
            ver = db.get(PricingVersion, vid, populate_existing=True)
            if ver is None:
                db.add(PricingVersion(id=vid, book_id=bid, version=v["version"], status=v["status"],
                                      effective_from=_dt(v["effective_from"]), content_hash=v["content_hash"],
                                      rule_count=len(v["rules"]), created_at=now))  # fmt: skip
                db.flush()
                for r in v["rules"]:
                    db.add(PricingRule(id=uuid.UUID(r["rule_id"]), version_id=vid, channel=r["channel"], prefix=r["prefix"],
                                       operator=r["operator"], unit_price=Decimal(r["unit_price"])))  # fmt: skip
                res.new_versions += 1
                res.new_rules += len(v["rules"])
            else:
                if (
                    ver.content_hash != v["content_hash"]
                    or ver.book_id != bid
                    or ver.version != v["version"]
                ):
                    raise ApplyError(
                        f"version {vid} changed content in Central (immutable): refusing the snapshot"
                    )
                if ver.status == "retired" and v["status"] == "active":
                    raise ApplyError(f"version {vid} was retired and cannot become active again")
                if ver.status == "active" and v["status"] == "retired":
                    ver.status = "retired"
                    res.retired += 1
    db.flush()
    for e in d["enterprises"]:
        for a in e["assignments"]:
            db.add(PricingAssignment(id=uuid.uuid4(), snapshot_id=snap_row.id, assignment_id=uuid.UUID(a["assignment_id"]),
                                     enterprise_id=uuid.UUID(e["enterprise_id"]), product_id=uuid.UUID(a["product_id"]),
                                     book_id=uuid.UUID(a["price_book_id"]), effective_from=_dt(a["effective_from"])))  # fmt: skip
    db.flush()
    state.active_snapshot_id, state.epoch, state.revision = snap_row.id, epoch, rev
    state.authorization_generation, state.snapshot_hash = gen, d["snapshot_hash"]
    state.activated_at, state.last_success_at = now, now
    state.first_active_at = state.first_active_at or now
    state.last_error = state.last_error_at = None
    return res


def mark_success(db: Session, now: datetime | None = None) -> None:
    """Central: asnjë ndryshim (changed=false) — sinkronizimi është i freskët."""
    st = get_state(db, lock=True)
    st.last_success_at, st.last_error, st.last_error_at = as_utc(now or utcnow()), None, None


def record_error(db: Session, message: str, now: datetime | None = None) -> None:
    st = get_state(db, lock=True)
    st.last_error, st.last_error_at = message[:2000], as_utc(now or utcnow())


def sync_age_seconds(st: PricingState, now: datetime | None = None) -> float | None:
    if st.last_success_at is None:
        return None
    return (as_utc(now or utcnow()) - as_utc(st.last_success_at)).total_seconds()
