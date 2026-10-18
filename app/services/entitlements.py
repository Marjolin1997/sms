"""M7-g: vendimi efektiv i autorizimit për submit (SMS/Email) dhe kapja e portës së enforce.

Tre mënyra (`SMS_CP_SYNC_MODE`):
  off      — asnjë punë shtesë (kthim i menjëhershëm); sjellje identike me legacy.
  shadow   — vëzhgim vetëm (M7-e) + krahasim i burimit të kufirit; kurrë s'ndryshon rezultatin.
  enforce  — legacy lokal (AccountPlan.enabled) DHE entitlement i Control Plane; DENY WINS.

Tabela e vendimit (enforce):
    legacy allow | CP allow -> allow          legacy deny | CP allow -> deny (AccountPlan lokal)
    legacy allow | CP deny  -> deny (kod CP)  legacy deny | CP deny  -> deny (AccountPlan lokal)
CP allow kërkon: enterprise `active` + ≥1 entitlement i kanalit `active` (V1: kanali aktiv nëse
≥1 entitlement aktiv). CP deny sipas arsyes → gabim publik i qëndrueshëm (jo 500, pa detaje sync):
    enterprise nuk është `active`            -> EnterpriseSuspended (enterprise_suspended)
    kurrë i sinkr. / pa entitlement / withdrawn -> ProductNotEntitled (product_not_entitled)
    entitlement-et ekzistojnë, asnjë `active`  -> ProductSuspended   (product_suspended)
FAIL-STATIC: vjetërsia e sinkronizimit NUK refuzon kurrë (last-known-good vazhdon); vetëm log
(WARNING >5 min, ERROR >15 min). "Kurrë i sinkronizuar" (cp_revision=0 / pa rresht) = DENY.
Kufiri/min (enforce): minimumi i kufijve jo-NULL të entitlement-eve aktive, ose DEFAULT lokal;
algoritmi/numëruesi i kufizuesit mbetet i pandryshuar (ndryshon vetëm burimi i vlerës).
Break-glass: AccountPlan.enabled=false mbetet deny lokal edhe kur CP thotë active; sinkronizimi
kurrë s'e vendos true (deri sa të ketë fushë të dedikuar).
Rollback: enforce → shadow/off = vetëm ndryshim konfigurimi (+ rinisje); asnjë migrim, fushat
legacy mbeten të plotësuara."""

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import utcnow
from app.models.control_plane import CpCursor, Entitlement
from app.models.enterprise import Enterprise
from app.models.sending import AccountPlan
from app.services import control_plane_shadow as shadow
from app.services.control_plane_sync import ALERT_AGE_S, SLO_AGE_S, sync_age_seconds

log = logging.getLogger("sms.cp.enforce")

CACHE_TTL_S = 5.0  # vonesa maksimale lokale e një pezullimi të aplikuar nga poller-i (enforce)
DETAIL_LOG_EVERY_S = 600.0
HEALTH_LOG_EVERY_S = 60.0
_MAX_KEYS = 10_000

HEALTHY, WARNING, CRITICAL = "healthy", "warning", "critical"


def sync_health(age_s: float | None) -> str:
    """healthy ≤5 min · warning ≤15 min · critical >15 min ose kurrë i sinkronizuar. Vetëm
    informativ (dashboard/log): nuk shkakton asnjë deny automatik."""
    if age_s is None or age_s > SLO_AGE_S:
        return CRITICAL
    return WARNING if age_s > ALERT_AGE_S else HEALTHY


@dataclass(frozen=True, slots=True)
class Decision:
    allow: bool
    code: str | None = None  # kodi publik i qëndrueshëm; `messages.denial` e kthen në gabim
    reason: str | None = None  # i brendshëm (log/numërues), kurrë te klienti
    rate_limit: int | None = None  # kufiri efektiv i CP (None = default lokal)


_MESSAGES = {  # tekste të sigurta për klientin: asnjë detaj sync/kursori
    "enterprise_suspended": "this account is suspended",
    "product_not_entitled": "this product is not enabled for your account",
    "product_suspended": "this product is suspended for your account",
}

E_SUSPENDED, E_NOT_ENTITLED, E_PRODUCT_SUSPENDED = (
    "enterprise_suspended", "product_not_entitled", "product_suspended",
)  # fmt: skip


class EnforceStats:
    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.denied: dict[tuple[str, str], int] = {}  # (kanal, arsye) → numër
            self.allowed = 0
            self._detail_at: dict[tuple, float] = {}
            self._health_at = -1e9

    def snapshot(self) -> dict:
        with self._lock:
            return {"allowed": self.allowed, "denied": dict(self.denied)}

    def allow(self) -> None:
        with self._lock:
            self.allowed += 1

    def deny(self, channel: str, reason: str, eid) -> None:
        mono = time.monotonic()
        with self._lock:
            self.denied[(channel, reason)] = self.denied.get((channel, reason), 0) + 1
            key = (str(eid), channel, reason)
            if mono - self._detail_at.get(key, -1e9) >= DETAIL_LOG_EVERY_S:
                if len(self._detail_at) >= _MAX_KEYS:
                    self._detail_at.clear()
                self._detail_at[key] = mono
                log.warning(
                    "enforce deny enterprise_id=%s channel=%s reason=%s", eid, channel, reason
                )

    def health(self, age_s: float | None) -> None:
        mono = time.monotonic()
        with self._lock:
            if mono - self._health_at < HEALTH_LOG_EVERY_S:
                return
            level = sync_health(age_s)
            if level != HEALTHY:
                self._health_at = mono
                (log.error if level == CRITICAL else log.warning)(
                    "control-plane sync %s age_s=%s; last-known-good state in use (fail-static)",
                    level, None if age_s is None else int(age_s),
                )  # fmt: skip


stats = EnforceStats()


def decide_state(st: shadow.CpState) -> Decision:
    """Vendimi i pastër nga gjendja CP (pa DB, pa kohë: fail-static)."""
    if not st.known:
        return Decision(False, E_NOT_ENTITLED, "never_synced")
    if st.enterprise_status != "active":
        return Decision(False, E_SUSPENDED, "enterprise_suspended")
    live = [(s, lim) for s, lim in st.entitlements if s != "withdrawn"]
    if not st.entitlements:
        return Decision(False, E_NOT_ENTITLED, "no_entitlement")
    if not live:
        return Decision(False, E_NOT_ENTITLED, "withdrawn")
    if not any(s == "active" for s, _ in live):
        return Decision(False, E_PRODUCT_SUSPENDED, "entitlement_suspended")
    return Decision(True, None, None, shadow.effective_rate_limit(st))


def gate(db: Session, plan, owner, channel: str, legacy_allowed: bool, legacy_limit=None):
    """Pika e vetme e thirrjes nga `submit`. off → None (asnjë punë); shadow → vëzhgon, None;
    enforce → `Decision` (thirrësi: deny wins). Në enforce një gabim leximi CP dështon MBYLLUR
    (ProductNotEntitled): nuk ka fallback te legacy allow."""
    mode = settings.cp_sync_mode
    if mode == "off":
        return None
    if mode == "shadow":
        shadow.observe(db, plan, owner, channel, legacy_allowed, legacy_limit)
        return None
    if not legacy_allowed:  # legacy deny fiton: s'duhet as lookup CP
        return None
    eid = getattr(plan, "enterprise_id", None) or getattr(owner, "enterprise_id", None)
    if eid is None:
        dec = Decision(False, E_NOT_ENTITLED, "no_enterprise_identity")
    else:
        try:
            st = shadow.load_state(db, eid, channel, CACHE_TTL_S)
            dec = decide_state(st)
            stats.health(sync_age_seconds_from(st))
        except Exception:  # noqa: BLE001  (fail-closed në enforce)
            log.exception("entitlement lookup failed; denying (enforce is fail-closed)")
            dec = Decision(False, E_NOT_ENTITLED, "lookup_error")
    if dec.allow:
        stats.allow()
    else:
        stats.deny(channel, dec.reason or "unknown", eid)
    return dec


def sync_age_seconds_from(st: shadow.CpState, now: datetime | None = None) -> float | None:
    if st.last_success_at is None:
        return None
    from app.core.timeutil import as_utc

    return (as_utc(now or utcnow()) - as_utc(st.last_success_at)).total_seconds()


def public_message(dec: Decision) -> str:
    """Tekst i sigurt për klientin (asnjë detaj sync/kursori)."""
    assert dec.code is not None
    return _MESSAGES[dec.code]


# --- gatishmëria për enforce ---------------------------------------------------------


@dataclass
class Readiness:
    ok: bool
    problems: list[str] = field(default_factory=list)
    sync_health: str = CRITICAL
    never_synced_enterprises: int = 0
    mismatches: dict = field(default_factory=dict)  # kanal → klasa → numër (pa përjashtimet)
    accepted: int = 0  # çifte (enterprise, kanal) të pranuara eksplicit nga operatori

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "problems": self.problems, "sync_health": self.sync_health,
            "never_synced_enterprises": self.never_synced_enterprises,
            "mismatches": self.mismatches, "accepted_exceptions": self.accepted,
        }  # fmt: skip


def can_enable_enforce(
    db: Session,
    exceptions: set[tuple[uuid.UUID, str]] | frozenset = frozenset(),
    now: datetime | None = None,
) -> Readiness:
    """Raport vetëm-lexim: a është Enterprise gati për `enforce`? NUK ndryshon konfigurimin.

    Kërkon: snapshot ekziston (epoch+generation), `last_success_at` ekziston dhe sinkronizimi nuk
    është kritik, asnjë enterprise me AccountPlan "kurrë i sinkronizuar", dhe asnjë mospërputhje
    legacy↔CP (legacy_allow_cp_deny, legacy_deny_cp_allow, cp_missing, cp_withdrawn) përveç atyre
    që operatori i pranon eksplicit (`exceptions` = {(enterprise_id, 'sms'|'email')}). Rishikimi/
    zgjidhja e review/conflict të M7-f është port i jashtëm (nuk verifikohet këtu)."""
    rep = Readiness(ok=False)
    cur = db.get(CpCursor, 1, populate_existing=True)
    if cur is None or cur.epoch is None or cur.authorization_generation is None:
        rep.problems.append("no snapshot applied yet (epoch/generation missing)")
        return rep
    age = sync_age_seconds(cur, now)
    rep.sync_health = sync_health(age)
    if cur.last_success_at is None:
        rep.problems.append("last_success_at is missing")
    elif rep.sync_health == CRITICAL:
        rep.problems.append(f"sync is critically stale (age {int(age or 0)} s)")
    ents: dict[tuple[uuid.UUID, str], list] = {}
    for e in db.scalars(select(Entitlement)):
        ents.setdefault((e.enterprise_id, e.channel), []).append((e.status, e.rate_limit_per_min))
    counts: dict[str, dict[str, int]] = {"sms": {}, "email": {}}
    never = set()
    rows = db.execute(
        select(AccountPlan.enterprise_id, AccountPlan.enabled, Enterprise.status,
               Enterprise.cp_revision)
        .join(Enterprise, Enterprise.id == AccountPlan.enterprise_id)
    ).all()  # fmt: skip
    for eid, enabled, e_status, cp_rev in rows:
        if not cp_rev:
            never.add(eid)
        for ch in ("sms", "email"):
            st = shadow.CpState(
                bool(cp_rev), e_status, tuple(ents.get((eid, ch), ())), cur.last_success_at
            )
            cls = shadow.classify_state(st, bool(enabled), now, check_stale=False)
            if cls in (shadow.LEGACY_ALLOW_CP_ALLOW, shadow.LEGACY_DENY_CP_DENY):
                continue
            if (eid, ch) in exceptions:
                rep.accepted += 1
                continue
            counts[ch][cls] = counts[ch].get(cls, 0) + 1
    rep.never_synced_enterprises = len({e for e in never if any(
        (e, ch) not in exceptions for ch in ("sms", "email"))})  # fmt: skip
    rep.mismatches = {ch: dict(sorted(v.items())) for ch, v in counts.items() if v}
    if rep.never_synced_enterprises:
        rep.problems.append(
            f"{rep.never_synced_enterprises} enterprise(s) with an AccountPlan were never synced"
        )
    for ch, v in rep.mismatches.items():
        rep.problems.append(f"{ch}: unexplained mismatches {v}")
    rep.ok = not rep.problems
    return rep
