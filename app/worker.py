"""Worker: python -m app.worker [--role sms|webhooks|control_plane] [--once]

Dy role të ndara qëllimisht: një endpoint i ngadaltë i klientit (timeout 10s) nuk duhet të
bllokojë dërgimin e SMS/email."""

import argparse
import logging
import os
import signal
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

from app.core.config import settings
from app.core.db import SessionLocal
from app.providers import register_configured
from app.services import billing, emails, events, payments, webhooks
from app.services.campaigns import run_due
from app.services.messages import expire_stale, process_one

log = logging.getLogger("sms.worker")
HEARTBEAT = Path(os.environ.get("SMS_WORKER_HEARTBEAT", "/tmp/sms-worker-alive"))  # nosec B108


def heartbeat() -> None:
    """Skedari përditësohet çdo cikël; healthcheck-u i Docker-it kontrollon moshën e tij."""
    try:
        HEARTBEAT.touch()
    except OSError:
        pass


def sweep() -> None:
    with SessionLocal() as db:
        try:
            n = expire_stale(db, timedelta(hours=settings.dlr_timeout_hours))
            db.commit()
            if n:
                log.warning("expired %d messages without DLR", n)
        except Exception:
            db.rollback()
            log.exception("sweep failed")


def billing_tick() -> None:
    with SessionLocal() as db:
        try:
            issued = billing.run_billing(db)
            expired = payments.expire_pending(db)
            db.commit()
            if issued or expired:
                log.info("billing: %d invoices issued, %d payments expired", issued, expired)
        except Exception:
            db.rollback()
            log.exception("billing tick failed")


def run(poll_seconds: float = 1.0, sweep_every: float = 60.0) -> None:
    register_configured()
    last_sweep = last_campaigns = last_billing = 0.0
    while True:
        heartbeat()
        if time.monotonic() - last_sweep >= sweep_every:
            sweep()
            last_sweep = time.monotonic()
        if time.monotonic() - last_billing >= 600:  # çdo 10 minuta
            billing_tick()
            last_billing = time.monotonic()
        with SessionLocal() as db:
            try:
                m = process_one(db) or emails.process_one(db)
                if time.monotonic() - last_campaigns >= 1.0:  # jo më shpesh se 1×/sekondë
                    run_due(db)
                    last_campaigns = time.monotonic()
            except Exception:
                db.rollback()
                log.exception("worker cycle failed")
                m = None
        if m is None:
            time.sleep(poll_seconds)


def purge() -> None:
    with SessionLocal() as db:
        try:
            n = events.purge_old(db, settings.event_retention_days)
            db.commit()
            if n:
                log.info("purged %d old events", n)
        except Exception:
            db.rollback()
            log.exception("event purge failed")


def run_webhooks(poll_seconds: float = 1.0, purge_every: float = 3600.0) -> None:
    last_purge = 0.0
    while True:
        heartbeat()
        if time.monotonic() - last_purge >= purge_every:
            purge()
            last_purge = time.monotonic()
        with SessionLocal() as db:
            try:
                d = webhooks.deliver_next(db)
            except Exception:
                db.rollback()
                log.exception("webhook cycle failed")
                d = None
        if d is None:
            time.sleep(poll_seconds)


def run_control_plane(once: bool = False) -> int:
    """Poller i Control Plane (M7-e): NJË aktiv për DB (kyç advisory PostgreSQL), mbyllje e hijshme
    me SIGTERM/SIGINT. `SMS_CP_SYNC_MODE=off` ⇒ proces boshe (pa dështim/rinisje në cikël)."""
    from app.core.db import engine
    from app.services import control_plane_poller as poller
    from app.services.control_plane_client import (
        ConfigError,
        ControlPlaneClient,
        config_from_settings,
    )

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    if settings.cp_sync_mode == "off":
        log.info("SMS_CP_SYNC_MODE=off: control-plane poller idle")
        while not stop.is_set():
            heartbeat()
            stop.wait(30)
        return 0
    try:
        client = ControlPlaneClient(config_from_settings(settings))
    except ConfigError as e:
        log.critical("control-plane sync misconfigured: %s", e)
        return 2
    lock = poller.PollerLock(engine)
    try:
        if once:
            if not lock.acquire():
                log.info("another control-plane poller is active; nothing to do")
                return 0
            out = poller.poll_once(
                SessionLocal, client, snapshot_interval_s=settings.cp_snapshot_interval_seconds
            )
            poller.check_staleness(SessionLocal)
            return 0 if out.ok else 1
        poller.run_loop(
            SessionLocal, client,
            poll_interval_s=settings.cp_poll_interval_seconds,
            snapshot_interval_s=settings.cp_snapshot_interval_seconds,
            stop=stop, lock=lock, tick=heartbeat,
        )  # fmt: skip
        return 0
    finally:
        lock.release()
        client.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=["sms", "webhooks", "control_plane"], default="sms")
    ap.add_argument("--once", action="store_true", help="control_plane: një iteracion dhe dil")
    args = ap.parse_args()
    if args.role == "control_plane":
        sys.exit(run_control_plane(args.once))
    run() if args.role == "sms" else run_webhooks()
