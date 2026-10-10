"""M7-e: orkestrimi i sinkronizimit me Central (pull). Lidh `ControlPlaneClient` (transport) me
`control_plane_sync` (aplikues) dhe vendos KUR bëhet snapshot/feed. Nuk di HTTP/JWT, nuk prek
AccountPlan, nuk merr vendime trafiku (shadow vetëm vëzhgon).

Rrjedha e një iteracioni (`poll_once`):
  1. kursor epoch NULL ⇒ snapshot i plotë (kurrë feed-first); i detyruar edhe nga rakordimi
     periodik (`snapshot_interval`) — MEKANIZËM KORREKTËSIE: ngjarjet për enterprise të panjohur
     lokalisht kalohen nga aplikuesi dhe kursori përparon, kështu që vetëm snapshot-i i plotë i
     popullon më vonë; mbron edhe nga drift dhe ndryshime të fushës së autorizimit;
  2. feed me kursorin lokal; çdo faqe aplikohet me `next_seq` të mbështjellësit (kurrë seq-i i
     fundit i ngjarjeve) dhe commit-ohet; `has_more` ⇒ faqja tjetër (kufi faqesh/iteracion);
  3. 409 (epoch/generation/cursor_ahead) ose 410 (cursor_expired) ose `SnapshotRequired` lokale
     ⇒ snapshot i plotë (pa reset automatik), pastaj feed nga `snapshot_seq`; maksimumi NJË
     snapshot i detyruar për iteracion;
  4. asnjë riprovim i brendshëm: një dështim kthen `PollOutcome` dhe `run_loop` vendos vonesën.

`last_success_at` përditësohet vetëm nga aplikuesi, pas apply të suksesshëm (jo nga HTTP 200).
Fail-static: asnjë dështim/vjetërsi nuk çaktivizon asgjë."""

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.timeutil import as_utc, utcnow
from app.services import control_plane_sync as cps
from app.services.control_plane_client import (
    ControlPlaneClient,
    CpAuthError,
    CpError,
    CpForbidden,
    CpProtocolError,
    CpSnapshotRequired,
    CpTransportError,
)
from packages.contracts.control_plane.v1 import ContractError

log = logging.getLogger("sms.cp.poller")

ALERT_AGE_S, SLO_AGE_S = cps.ALERT_AGE_S, cps.SLO_AGE_S
FORBIDDEN_DELAY_S = 300  # 403 s'është i kalueshëm me riprovim agresiv
ALERT_AFTER_FAILURES = 5
MAX_PAGES = 50
PAGE_LIMIT = 200
LOCK_KEY = 0x534D534350  # "SMSCP": kyç advisory për DB-në e Enterprise
OK = "ok"


@dataclass(slots=True)
class PollOutcome:
    kind: str = OK  # ok | auth_error | forbidden | network_error | protocol_error | apply_error
    snapshots: int = 0
    pages: int = 0
    applied: int = 0
    noop: int = 0
    stale: int = 0
    skipped_unknown_enterprise: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.kind == OK


class Backoff:
    """Eksponencial i kufizuar me jitter: 1,2,4,8,16,30,30… s (× rastësi 0.5–1.0)."""

    def __init__(self, base: float = 1.0, cap: float = 30.0, rng: random.Random | None = None):
        self.base, self.cap, self.n = base, cap, 0
        self._rng = rng or random.Random()

    def next(self) -> float:
        raw = min(self.cap, self.base * (2**self.n))
        self.n += 1
        return raw * self._rng.uniform(0.5, 1.0)

    def reset(self) -> None:
        self.n = 0


# --- një iteracion ----------------------------------------------------------------------------


def _snapshot(factory, client: ControlPlaneClient, reason: str, now: datetime, out: PollOutcome):
    raw = client.get_snapshot()  # FULL: pa enterprise_id
    snap = cps.parse_snapshot(raw)
    with factory() as db:
        try:
            r = cps.apply_snapshot(db, snap, now=now, full_scope=True)
            db.commit()
        except Exception:
            db.rollback()
            raise
    out.snapshots += 1
    out.skipped_unknown_enterprise += r.skipped_unknown_enterprise
    log.info(
        "snapshot applied reason=%s epoch=%s generation=%d snapshot_seq=%d enterprises=%d "
        "entitlements_applied=%d withdrawn=%d out_of_scope=%d unknown_enterprise=%d reset=%s",
        reason, snap.epoch, snap.authorization_generation, snap.snapshot_seq,
        len(snap.enterprises), r.entitlements_applied, r.entitlements_withdrawn,
        r.entitlements_out_of_scope, r.skipped_unknown_enterprise, r.reset,
    )  # fmt: skip
    if r.reset:
        log.warning("epoch change: local state replaced from the new central epoch")
    if r.skipped_unknown_enterprise:
        log.warning(
            "snapshot has %d enterprise(s) unknown locally (skipped; reconciled once they exist)",
            r.skipped_unknown_enterprise,
        )


def _feed(factory, client: ControlPlaneClient, now: datetime, out: PollOutcome) -> None:
    for _ in range(MAX_PAGES):
        with factory() as db:
            cur = cps.get_cursor(db)
            epoch, gen, after = cur.epoch, cur.authorization_generation, cur.last_seq
        if epoch is None or gen is None:
            raise cps.SnapshotRequired("no_snapshot")
        page = client.get_changes(after, epoch, gen, PAGE_LIMIT)
        events = cps.parse_events(page.events)  # të gjitha ose asgjë; asnjë anashkalim
        with factory() as db:
            try:
                r = cps.apply_feed_batch(
                    db, epoch=page.epoch, authorization_generation=page.authorization_generation,
                    events=events, next_seq=page.next_seq, now=now,
                )  # fmt: skip
                db.commit()
            except Exception:
                db.rollback()
                raise
        out.pages += 1
        out.applied += r.applied
        out.noop += r.noop
        out.stale += r.stale
        out.skipped_unknown_enterprise += r.skipped_unknown_enterprise
        log.info(
            "poll applied events=%d applied=%d noop=%d stale=%d unknown_enterprise=%d cursor=%d "
            "latest=%d more=%s",
            len(events), r.applied, r.noop, r.stale, r.skipped_unknown_enterprise, page.next_seq,
            page.latest_seq, page.has_more,
        )  # fmt: skip
        if r.skipped_unknown_enterprise:
            log.warning(
                "%d event(s) skipped for enterprise unknown locally", r.skipped_unknown_enterprise
            )
        if not page.has_more:
            return
        if page.next_seq <= after:  # mbrojtje nga cikli pa progres
            raise CpProtocolError("has_more with no cursor progress")


def snapshot_due(cur, snapshot_interval_s: float, now: datetime) -> str | None:
    if cur.epoch is None or cur.authorization_generation is None:
        return "bootstrap"
    if cur.last_snapshot_at is None:
        return "periodic"
    if (as_utc(now) - as_utc(cur.last_snapshot_at)).total_seconds() >= snapshot_interval_s:
        return "periodic"
    return None


def poll_once(
    factory: Callable[[], Session],
    client: ControlPlaneClient,
    *,
    snapshot_interval_s: float,
    now: datetime | None = None,
) -> PollOutcome:
    now = now or utcnow()
    out = PollOutcome()
    try:
        with factory() as db:
            reason = snapshot_due(cps.get_cursor(db), snapshot_interval_s, now)
        if reason:
            _snapshot(factory, client, reason, now, out)
        try:
            _feed(factory, client, now, out)
        except (CpSnapshotRequired, cps.SnapshotRequired) as e:
            why = e.code if isinstance(e, CpSnapshotRequired) else e.reason
            log.warning("snapshot required reason=%s: full snapshot, no automatic reset", why)
            _snapshot(factory, client, why, now, out)
            _feed(factory, client, now, out)
    except CpAuthError as e:
        out.kind, out.detail = "auth_error", str(e)
        log.error("central auth failed: %s", e)
    except CpForbidden as e:
        out.kind, out.detail = "forbidden", str(e)
        log.error("central denied access: %s", e)
    except CpTransportError as e:
        out.kind, out.detail = "network_error", str(e)
        log.warning("central unreachable (local state retained, fail-static): %s", e)
    except (CpProtocolError, CpSnapshotRequired, cps.SnapshotRequired) as e:
        out.kind, out.detail = "protocol_error", str(e)
        log.error("central protocol problem: %s", e)
    except (ContractError, cps.ApplyError, cps.StaleSnapshot) as e:
        out.kind, out.detail = "apply_error", f"{type(e).__name__}: {e}"
        log.error("could not apply central data (cursor unchanged): %s: %s", type(e).__name__, e)
    except CpError as e:  # çdo e panjohur tjetër e klientit
        out.kind, out.detail = "protocol_error", str(e)
        log.error("central client error: %s", e)
    return out


def check_staleness(factory: Callable[[], Session], now: datetime | None = None) -> float | None:
    """Raporton vjetërsinë e sinkronizimit (vetëm log/alarm). KURRË çaktivizim automatik."""
    with factory() as db:
        age = cps.sync_age_seconds(cps.get_cursor(db), now)
    if age is None:
        log.warning("control-plane sync has never succeeded")
    elif age > SLO_AGE_S:
        log.error(
            "control-plane sync is stale age_s=%.0f (> SLO %d s); state kept (fail-static)",
            age,
            SLO_AGE_S,
        )
    elif age > ALERT_AGE_S:
        log.warning("control-plane sync age_s=%.0f exceeds alert threshold %d s", age, ALERT_AGE_S)
    return age


# --- singleton ---------------------------------------------------------------------------------


class PollerLock:
    """Një poller aktiv për DB të Enterprise. PostgreSQL: kyç advisory i nivelit SESION mbi një
    lidhje të dedikuar (AUTOCOMMIT: `idle_in_transaction_session_timeout` s'e vret); lirohet
    automatikisht kur lidhja mbyllet/proçesi vdes. Nuk është kusht korrektësie (aplikuesi vetë
    serializon kursorin): vetëm shmang kërkesat e dyfishta. SQLite (vetëm zhvillim, një proçes):
    pa koordinim."""

    def __init__(self, engine: Engine, key: int = LOCK_KEY):
        self._engine, self._key, self._conn = engine, key, None
        self._pg = engine.dialect.name == "postgresql"

    def acquire(self) -> bool:
        if not self._pg:
            return True
        if self._conn is not None:
            return self.held()
        conn = self._engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            ok = bool(
                conn.execute(text("select pg_try_advisory_lock(:k)"), {"k": self._key}).scalar()
            )
        except Exception:
            conn.close()
            raise
        if not ok:
            conn.close()
            return False
        self._conn = conn
        return True

    def held(self) -> bool:
        if not self._pg:
            return True
        if self._conn is None:
            return False
        try:
            self._conn.execute(text("select 1"))
            return True
        except Exception:  # noqa: BLE001  (lidhja humbi ⇒ kyçi u lirua nga serveri)
            self._drop()
            return False

    def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def release(self) -> None:
        if self._conn is not None:
            try:
                self._conn.execute(text("select pg_advisory_unlock(:k)"), {"k": self._key})
            except Exception:  # noqa: BLE001
                pass
            self._drop()


# --- cikli -------------------------------------------------------------------------------------


def _sleep(stop: threading.Event, seconds: float, tick: Callable[[], None] | None) -> None:
    end = time.monotonic() + seconds
    while not stop.is_set():
        if tick:
            tick()
        left = end - time.monotonic()
        if left <= 0:
            return
        stop.wait(min(left, 5.0))


def run_loop(
    factory: Callable[[], Session],
    client: ControlPlaneClient,
    *,
    poll_interval_s: float,
    snapshot_interval_s: float,
    stop: threading.Event,
    lock: PollerLock | None = None,
    backoff: Backoff | None = None,
    tick: Callable[[], None] | None = None,
    poll: Callable[..., PollOutcome] = poll_once,
    staleness: Callable[..., object] | None = None,
) -> None:
    """Cikli i poller-it deri te `stop`. Pas suksesit kthehet te `poll_interval_s`; pas dështimit
    pret `Backoff` (1→30 s me jitter; 403: 300 s). Humbja e kyçit ⇒ riprovim para poll-it tjetër."""
    backoff = backoff or Backoff()
    failures = 0
    while not stop.is_set():
        if lock is not None and not lock.acquire():
            log.info("another control-plane poller holds the lock; standing by")
            _sleep(stop, poll_interval_s, tick)
            continue
        out = poll(factory, client, snapshot_interval_s=snapshot_interval_s)
        (staleness or check_staleness)(factory)
        if out.ok:
            failures = 0
            backoff.reset()
            delay = poll_interval_s
        else:
            failures += 1
            delay = FORBIDDEN_DELAY_S if out.kind == "forbidden" else backoff.next()
            if failures == ALERT_AFTER_FAILURES or (
                failures > ALERT_AFTER_FAILURES and failures % 20 == 0
            ):
                log.error(
                    "ALERT control-plane sync failing: %d consecutive failures (last=%s)",
                    failures,
                    out.kind,
                )
        if lock is not None and not lock.held():
            log.warning("poller lock lost; will re-acquire before the next poll")
        _sleep(stop, delay, tick)
    if lock is not None:
        lock.release()
