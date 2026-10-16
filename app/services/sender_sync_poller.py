"""M10-S2: orkestrimi i feed-it `cp.sender.v1` (pull). VETËM roli worker `sender_control_plane` e thërret; rruga e dërgimit s'e importon.

Snapshot fillestar → faqe të kufizuara → apliko (atomike) → kursor. `409/410` të njohura (epokë/autorizim/kursor para/skaduar) ⇒ snapshot i plotë (numërohet si rikuperim boshllëku).
Dështim (rrjet/auth/5xx) ⇒ fail-static: projeksioni ekzistues mbetet i paprekur, kursori s'lëviz, asnjë miratim lokal i shpikur. Periodikisht: snapshot rakordimi (zbulon drift)."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.timeutil import as_utc, utcnow
from app.services import sender_sync as ss
from app.services.control_plane_client import (
    ControlPlaneClient,
    CpAuthError,
    CpError,
    CpForbidden,
    CpProtocolError,
    CpSnapshotRequired,
    CpTransportError,
)
from packages.contracts.control_plane.sender.v1 import ContractError

log = logging.getLogger("sms.sender.poller")
MAX_PAGES = 100
PAGE_LIMIT = 200
LOCK_KEY = 0x534D53534E  # "SMSSN": kyç advisory i veçantë nga cp.v1 / parat / çmimet
OK = "ok"


@dataclass(slots=True)
class PollOutcome:
    kind: str = OK  # ok | auth_error | forbidden | network_error | protocol_error | apply_error
    snapshots: int = 0
    pages: int = 0
    applied: int = 0
    noop: int = 0
    stale: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.kind == OK


def _snapshot(
    factory, client: ControlPlaneClient, reason: str, now: datetime, out: PollOutcome
) -> None:
    snap = ss.parse_snapshot(client.get_sender_snapshot())
    with factory() as db:
        try:
            r = ss.apply_snapshot(db, snap, now=now)
            db.commit()
        except Exception:
            db.rollback()
            raise
    out.snapshots += 1
    log.info("sender snapshot applied reason=%s epoch=%s generation=%d seq=%d policies=%d senders=%d applied=%d noop=%d withdrawn=%d drift=%d reset=%s",
             reason, snap.epoch, snap.authorization_generation, snap.snapshot_seq, len(snap.policies), len(snap.senders), r.applied, r.noop, r.withdrawn, r.drift, r.reset)  # fmt: skip


def _feed(factory, client: ControlPlaneClient, now: datetime, out: PollOutcome) -> None:
    for _ in range(MAX_PAGES):
        with factory() as db:
            cur = ss.get_cursor(db)
            epoch, gen, after = cur.epoch, cur.authorization_generation, cur.last_seq
        if epoch is None or gen is None:
            raise ss.SnapshotRequired("no_snapshot")
        page = client.get_sender_changes(after, epoch, gen, PAGE_LIMIT)
        events = ss.parse_events(page.events)  # të gjitha ose asgjë
        with factory() as db:
            try:
                r = ss.apply_feed_batch(
                    db,
                    epoch=page.epoch,
                    authorization_generation=page.authorization_generation,
                    events=events,
                    next_seq=page.next_seq,
                    latest_seq=page.latest_seq,
                    now=now,
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
        out.pages += 1
        out.applied += r.applied
        out.noop += r.noop
        out.stale += r.stale
        log.info(
            "sender poll events=%d applied=%d noop=%d stale=%d cursor=%d latest=%d more=%s",
            len(events),
            r.applied,
            r.noop,
            r.stale,
            page.next_seq,
            page.latest_seq,
            page.has_more,
        )
        if not page.has_more:
            return
        if page.next_seq <= after:
            raise CpProtocolError("has_more with no cursor progress")


def snapshot_due(cur, snapshot_interval_s: float, now: datetime) -> str | None:
    if cur.epoch is None or cur.authorization_generation is None:
        return "bootstrap"
    if (
        cur.last_snapshot_at is None
        or (as_utc(now) - as_utc(cur.last_snapshot_at)).total_seconds() >= snapshot_interval_s
    ):
        return "periodic"
    return None


def _gap(factory) -> None:
    with factory() as db:
        ss.count_gap_recovery(db)
        db.commit()


def _note(factory, message: str, now: datetime) -> None:
    try:
        with factory() as db:
            ss.record_error(db, message, now)
            db.commit()
    except Exception:  # noqa: BLE001  (shënimi s'duhet të mbulojë gabimin origjinal)
        log.exception("could not record sender sync error")


def poll_once(
    factory: Callable[[], Session],
    client: ControlPlaneClient,
    *,
    snapshot_interval_s: float = 3600,
    now: datetime | None = None,
) -> PollOutcome:
    now = now or utcnow()
    out = PollOutcome()
    try:
        with factory() as db:
            reason = snapshot_due(ss.get_cursor(db), snapshot_interval_s, now)
        if reason:
            _snapshot(factory, client, reason, now, out)
        try:
            _feed(factory, client, now, out)
        except (CpSnapshotRequired, ss.SnapshotRequired, ss.IncompleteGroup) as e:
            why = (
                e.code
                if isinstance(e, CpSnapshotRequired)
                else getattr(e, "reason", type(e).__name__)
            )
            log.warning("sender snapshot required reason=%s: full snapshot recovery", why)
            _gap(factory)
            _snapshot(factory, client, str(why), now, out)
            _feed(factory, client, now, out)
    except CpAuthError as e:
        out.kind, out.detail = "auth_error", str(e)
        log.error("central auth failed (sender): %s", e)
        _note(factory, out.detail, now)
    except CpForbidden as e:
        out.kind, out.detail = "forbidden", str(e)
        log.error(
            "central denied sender access (needs scope sender:read + enterprise grant): %s", e
        )
        _note(factory, out.detail, now)
    except CpTransportError as e:
        out.kind, out.detail = "network_error", str(e)
        log.warning("central unreachable (sender; projection retained, fail-static): %s", e)
        _note(factory, out.detail, now)
    except (CpProtocolError, CpSnapshotRequired, ss.SnapshotRequired) as e:
        out.kind, out.detail = "protocol_error", str(e)
        log.error("central sender protocol problem: %s", e)
        _note(factory, out.detail, now)
    except (ContractError, ss.ApplyError, ss.StaleSnapshot) as e:
        out.kind, out.detail = "apply_error", f"{type(e).__name__}: {e}"
        log.error("ALERT could not apply sender data (cursor held): %s: %s", type(e).__name__, e)
        _note(factory, out.detail, now)
    except CpError as e:
        out.kind, out.detail = "protocol_error", str(e)
        log.error("central client error (sender): %s", e)
        _note(factory, out.detail, now)
    return out


def check_staleness(factory: Callable[[], Session], now: datetime | None = None) -> float | None:
    """Vetëm log/alarm. KURRË skadim i miratimeve nga mosha."""
    with factory() as db:
        age = ss.sync_age_seconds(ss.get_cursor(db), now)
    if age is None:
        log.warning("sender sync has never succeeded")
    elif age > ss.SLO_AGE_S:
        log.error(
            "sender sync is stale age_s=%.0f (> %d s); projection unchanged (fail-static)",
            age,
            ss.SLO_AGE_S,
        )
    return age
