"""M7-e: krahasimi SHADOW (vetëm vëzhgim). Për çdo submit llogaritet çfarë do të vendoste Control
Plane (Enterprise.status + entitlement i kanalit) krahas vendimit legacy (`AccountPlan.enabled`,
që thirrësi e kalon si bool). KURRË nuk ndryshon rezultatin e submit-it: çdo gabim përlahet,
asgjë s'shkruhet, query-t e rrezikshme ekzekutohen në savepoint.

Kosto në rrugën e nxehtë: vetëm kur `SMS_CP_SYNC_MODE=shadow`; gjendja CP ruhet në cache
për-proces (TTL 30 s) ⇒ në gjendje të qëndrueshme 0 SQL shtesë, përndryshe 1 SELECT (savepoint).
Pa infrastrukturë metrics në projekt: numëruesit janë në-proçes (`stats.snapshot()`), me përmbledhje
të rrallë në log dhe log detaji të kufizuar për (enterprise, kanal, klasë)."""

import logging
import threading
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import as_utc, utcnow
from app.models.control_plane import CpCursor, Entitlement, entitlement_enabled
from app.models.enterprise import Enterprise
from app.services.control_plane_sync import SLO_AGE_S

log = logging.getLogger("sms.cp.shadow")

LEGACY_ALLOW_CP_ALLOW = "legacy_allow_cp_allow"
LEGACY_ALLOW_CP_DENY = "legacy_allow_cp_deny"
LEGACY_DENY_CP_ALLOW = "legacy_deny_cp_allow"
LEGACY_DENY_CP_DENY = "legacy_deny_cp_deny"
CP_MISSING = "cp_missing"
CP_WITHDRAWN = "cp_withdrawn"
CP_STALE = "cp_stale"
CLASSES = (
    LEGACY_ALLOW_CP_ALLOW, LEGACY_ALLOW_CP_DENY, LEGACY_DENY_CP_ALLOW, LEGACY_DENY_CP_DENY,
    CP_MISSING, CP_WITHDRAWN, CP_STALE,
)  # fmt: skip
QUIET = frozenset({LEGACY_ALLOW_CP_ALLOW, LEGACY_DENY_CP_DENY})  # pajtim: pa log detaji

CACHE_TTL_S = 30.0
DETAIL_LOG_EVERY_S = 600.0
SUMMARY_EVERY_S = 300.0
_MAX_KEYS = 10_000


@dataclass(frozen=True, slots=True)
class CpState:
    """Gjendja CP e lexuar (e ruajtur në cache): vendimi llogaritet nga kjo + koha."""

    known: bool  # enterprise ekziston lokalisht me cp_revision > 0
    enterprise_status: str | None
    entitlement_statuses: tuple[str, ...]
    last_success_at: object  # datetime | None


def _load(db: Session, eid: uuid.UUID, channel: str) -> CpState:
    q = (
        select(
            Enterprise.status, Enterprise.cp_revision, Entitlement.status, CpCursor.last_success_at
        )
        .select_from(Enterprise)
        .outerjoin(
            Entitlement,
            and_(Entitlement.enterprise_id == Enterprise.id, Entitlement.channel == channel),
        )
        .outerjoin(CpCursor, CpCursor.id == 1)
        .where(Enterprise.id == eid)
    )
    with db.begin_nested():  # query-ja s'duhet të helmojë transaksionin e submit-it
        rows = db.execute(q).all()
    if not rows or not rows[0][1]:
        return CpState(False, None, (), None)
    return CpState(
        True, rows[0][0], tuple(r[2] for r in rows if r[2] is not None), rows[0][3]
    )  # fmt: skip


def classify_state(st: CpState, legacy_allowed: bool, now=None) -> str:
    """Klasifikim i pastër (pa DB). Rendi: missing → withdrawn → stale → 4 kombinimet."""
    if not st.known or not st.entitlement_statuses:
        return CP_MISSING
    if all(s == "withdrawn" for s in st.entitlement_statuses):
        return CP_WITHDRAWN
    if st.last_success_at is None or (
        (as_utc(now or utcnow()) - as_utc(st.last_success_at)).total_seconds() > SLO_AGE_S
    ):
        return CP_STALE
    cp_allowed = any(
        entitlement_enabled(st.enterprise_status or "", s) for s in st.entitlement_statuses
    )  # V1: kanali aktiv nëse ≥1 entitlement aktiv
    if legacy_allowed:
        return LEGACY_ALLOW_CP_ALLOW if cp_allowed else LEGACY_ALLOW_CP_DENY
    return LEGACY_DENY_CP_ALLOW if cp_allowed else LEGACY_DENY_CP_DENY


class ShadowStats:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.counts: dict[str, int] = dict.fromkeys(CLASSES, 0)
            self._detail_at: dict[tuple, float] = {}
            self._summary_at = time.monotonic()
            self._summarised = 0

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self.counts)

    def record(self, cls: str, eid, channel: str) -> None:
        mono = time.monotonic()
        with self._lock:
            self.counts[cls] += 1
            if cls not in QUIET:
                key = (str(eid), channel, cls)
                if mono - self._detail_at.get(key, -1e9) >= DETAIL_LOG_EVERY_S:
                    if len(self._detail_at) >= _MAX_KEYS:
                        self._detail_at.clear()
                    self._detail_at[key] = mono
                    log.warning(
                        "shadow mismatch class=%s enterprise_id=%s channel=%s", cls, eid, channel
                    )
            total = sum(self.counts.values())
            if mono - self._summary_at >= SUMMARY_EVERY_S and total != self._summarised:
                self._summary_at, self._summarised = mono, total
                log.info(
                    "shadow summary %s", " ".join(f"{k}={v}" for k, v in self.counts.items() if v)
                )


stats = ShadowStats()
_cache: dict[tuple[uuid.UUID, str], tuple[float, CpState]] = {}


def clear_cache() -> None:
    _cache.clear()


def classify(db: Session, enterprise_id, channel: str, legacy_allowed: bool, now=None) -> str:
    if enterprise_id is None:
        return CP_MISSING
    key = (enterprise_id, channel)
    mono = time.monotonic()
    hit = _cache.get(key)
    if hit is None or hit[0] <= mono:
        if len(_cache) >= _MAX_KEYS:
            _cache.clear()
        hit = (mono + CACHE_TTL_S, _load(db, enterprise_id, channel))
        _cache[key] = hit
    return classify_state(hit[1], legacy_allowed, now)


def observe(db: Session, plan, owner, channel: str, legacy_allowed: bool) -> None:
    """Thirret nga rruga e submit-it. Vetëm `shadow`: përndryshe kthim i menjëhershëm. Nuk hedh
    kurrë përjashtim dhe nuk ndryshon asgjë te thirrësi."""
    if settings.cp_sync_mode != "shadow":
        return
    try:
        eid = getattr(plan, "enterprise_id", None) or getattr(owner, "enterprise_id", None)
        stats.record(classify(db, eid, channel, legacy_allowed), eid, channel)
    except Exception:  # noqa: BLE001  (shadow s'duhet të prishë kurrë submit-in)
        log.debug("shadow observation failed", exc_info=True)
