"""Worker: python -m app.worker [--role sms|webhooks|control_plane|money_control_plane|sender_control_plane] [--once]

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
from app.services import billing, billing_authority, emails, events, messages, payments, webhooks
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
            # M9-a: SENDING i ngecur (> lease) ⇒ UNKNOWN/rirradhitje sipas fazës; kurrë release.
            for kind, svc in (("sms", messages), ("email", emails)):
                rep = svc.recover_stuck(db)
                db.commit()
                if rep.requeued or rep.unknown or rep.failed:
                    log.warning("recovered stuck %s: requeued=%d unknown=%d failed=%d",
                                kind, rep.requeued, rep.unknown, rep.failed)  # fmt: skip
        except Exception:
            db.rollback()
            log.exception("sweep failed")


def billing_tick() -> None:
    if (
        billing_authority.frozen()
    ):  # M9-g4: Central lëshon; ky worker s'bën asgjë (historia legacy mbetet vetëm-lexim)
        return
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


def run_money_control_plane(once: bool = False) -> int:
    """Consumer-i i parave `cp.money.v1` (M9-c): rol i VEÇANTË nga `control_plane` (cp.v1) dhe nga dërgimi.
    authority=local ⇒ proces boshe (pa dështim/rinisje). NJË aktiv për DB (kyç advisory i veçantë)."""
    from app.core.db import engine
    from app.services import control_plane_poller as poller
    from app.services import money_poller
    from app.services.control_plane_client import (
        MONEY_SCOPE,
        ConfigError,
        ControlPlaneClient,
        config_from_settings,
    )

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    if settings.money_authority == "local":
        log.info("SMS_MONEY_AUTHORITY=local: money consumer idle")
        while not stop.is_set():
            heartbeat()
            stop.wait(30)
        return 0
    try:
        client = ControlPlaneClient(config_from_settings(settings), scope=MONEY_SCOPE)
    except ConfigError as e:
        log.critical("money consumer misconfigured: %s", e)
        return 2
    lock = poller.PollerLock(engine, key=money_poller.LOCK_KEY)
    try:
        if once:
            if not lock.acquire():
                log.info("another money consumer is active; nothing to do")
                return 0
            out = money_poller.poll_once(SessionLocal, client)
            money_poller.check_staleness(SessionLocal)
            return 0 if out.ok else 1
        poller.run_loop(
            SessionLocal, client, poll_interval_s=settings.money_poll_interval_seconds,
            snapshot_interval_s=0, stop=stop, lock=lock, tick=heartbeat,
            poll=money_poller.poll_once, staleness=money_poller.check_staleness,
        )  # fmt: skip
        return 0
    finally:
        lock.release()
        client.close()


def run_sender_control_plane(once: bool = False) -> int:
    """Consumer-i i `cp.sender.v1` (M10-S2): rol i VEÇANTË (domen dështimi i ndarë nga cp.v1, parat, çmimet dhe dërgimi). `SMS_SENDER_SYNC_ENABLED=false` ⇒ proces boshe.
    Mban projeksionin e sinkronizuar; NUK ndryshon `SenderId`, as autorizimin e SMS. NJË aktiv për DB (kyç advisory i veçantë)."""
    from app.core.db import engine
    from app.services import control_plane_poller as poller
    from app.services import sender_sync_poller
    from app.services.control_plane_client import (
        SENDER_SCOPE,
        ConfigError,
        ControlPlaneClient,
        config_from_settings,
    )

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    if not settings.sender_sync_enabled:
        log.info("SMS_SENDER_SYNC_ENABLED=false: sender consumer idle")
        while not stop.is_set():
            heartbeat()
            stop.wait(30)
        return 0
    try:
        client = ControlPlaneClient(config_from_settings(settings), scope=SENDER_SCOPE)
    except ConfigError as e:
        log.critical("sender consumer misconfigured: %s", e)
        return 2
    lock = poller.PollerLock(engine, key=sender_sync_poller.LOCK_KEY)
    try:
        if once:
            if not lock.acquire():
                log.info("another sender consumer is active; nothing to do")
                return 0
            out = sender_sync_poller.poll_once(
                SessionLocal, client, snapshot_interval_s=settings.sender_snapshot_interval_seconds
            )
            sender_sync_poller.check_staleness(SessionLocal)
            return 0 if out.ok else 1
        poller.run_loop(
            SessionLocal, client, poll_interval_s=settings.sender_poll_interval_seconds,
            snapshot_interval_s=settings.sender_snapshot_interval_seconds, stop=stop, lock=lock, tick=heartbeat,
            poll=sender_sync_poller.poll_once, staleness=sender_sync_poller.check_staleness,
        )  # fmt: skip
        return 0
    finally:
        lock.release()
        client.close()


def run_money_usage_reporter(once: bool = False) -> int:
    """Raportuesi i përdorimit financiar (M9-d): rol i VEÇANTË (domen dështimi tjetër nga consumer-i i grant-eve
    dhe nga dërgimi SMS). `SMS_MONEY_REPORTING=false` ⇒ proces boshe. Një aktiv për DB (kyç advisory i veçantë).
    Çelësi Ed25519 duhet të ketë scope `money:report` te Central."""
    from app.core.db import engine
    from app.services import control_plane_poller as poller
    from app.services import money_usage
    from app.services.control_plane_client import (
        REPORT_SCOPE,
        ConfigError,
        ControlPlaneClient,
        config_from_settings,
    )

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    if not settings.money_reporting:
        log.info("SMS_MONEY_REPORTING=false: usage reporter idle")
        while not stop.is_set():
            heartbeat()
            stop.wait(30)
        return 0
    try:
        client = ControlPlaneClient(config_from_settings(settings), scope=REPORT_SCOPE)
    except ConfigError as e:
        log.critical("usage reporter misconfigured: %s", e)
        return 2
    lock = poller.PollerLock(engine, key=money_usage.LOCK_KEY)

    def tick(factory, cl, **_):
        try:
            return money_usage.run_once(engine, factory, cl)
        except Exception:  # noqa: BLE001  (një cikël i keq s'e vret procesin; rifillon pas backoff-it)
            log.exception("usage report cycle failed")
            return money_usage.DeliveryOutcome(kind="protocol_error", detail="cycle failed")

    try:
        if once:
            if not lock.acquire():
                return 0
            return 0 if tick(SessionLocal, client).ok else 1
        poller.run_loop(
            SessionLocal, client, poll_interval_s=settings.money_report_interval_seconds,
            snapshot_interval_s=0, stop=stop, lock=lock, tick=heartbeat, poll=tick,
            staleness=lambda *_: None,
        )  # fmt: skip
        return 0
    finally:
        lock.release()
        client.close()


def run_billing_usage_reporter(once: bool = False) -> int:
    """Raportuesi i përdorimit të faturueshëm të email-it (M9-g2): rol i VEÇANTË. `SMS_BILLING_USAGE_REPORTING=false` ⇒ proces boshe.
    Dështimi i tij nuk prek dërgimin e email-it (provë + outbox janë tashmë të commit-uara). Scope Ed25519: `billing:report`."""
    from app.core.db import engine
    from app.services import billing_usage
    from app.services import control_plane_poller as poller
    from app.services.control_plane_client import (
        BILLING_REPORT_SCOPE,
        ConfigError,
        ControlPlaneClient,
        config_from_settings,
    )

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    if not settings.billing_usage_reporting:
        log.info("SMS_BILLING_USAGE_REPORTING=false: billing usage reporter idle")
        while not stop.is_set():
            heartbeat()
            stop.wait(30)
        return 0
    try:
        client = ControlPlaneClient(config_from_settings(settings), scope=BILLING_REPORT_SCOPE)
    except ConfigError as e:
        log.critical("billing usage reporter misconfigured: %s", e)
        return 2
    lock = poller.PollerLock(engine, key=billing_usage.LOCK_KEY)

    def tick(factory, cl, **_):
        try:
            return billing_usage.run_once(engine, factory, cl)
        except Exception:  # noqa: BLE001  (një cikël i keq s'e vret procesin)
            log.exception("billing usage report cycle failed")
            return billing_usage.DeliveryOutcome(kind="protocol_error", detail="cycle failed")

    try:
        if once:
            if not lock.acquire():
                return 0
            return 0 if tick(SessionLocal, client).ok else 1
        poller.run_loop(
            SessionLocal, client, poll_interval_s=settings.billing_usage_report_interval_seconds,
            snapshot_interval_s=0, stop=stop, lock=lock, tick=heartbeat, poll=tick,
            staleness=lambda *_: None,
        )  # fmt: skip
        return 0
    finally:
        lock.release()
        client.close()


def run_pricing_control_plane(once: bool = False) -> int:
    """Konsumatori i çmimeve `cp.pricing.v1` (M9-e): rol i VEÇANTË (shëndet i veçantë nga money/cp.v1). authority=local ⇒ proces boshe."""
    from app.core.db import engine
    from app.services import control_plane_poller as poller
    from app.services import pricing_poller
    from app.services.control_plane_client import (
        PRICING_SCOPE,
        ConfigError,
        ControlPlaneClient,
        config_from_settings,
    )

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    if settings.pricing_authority == "local":
        log.info("SMS_PRICING_AUTHORITY=local: pricing consumer idle")
        while not stop.is_set():
            heartbeat()
            stop.wait(30)
        return 0
    try:
        client = ControlPlaneClient(config_from_settings(settings), scope=PRICING_SCOPE)
    except ConfigError as e:
        log.critical("pricing consumer misconfigured: %s", e)
        return 2
    lock = poller.PollerLock(engine, key=pricing_poller.LOCK_KEY)
    try:
        if once:
            if not lock.acquire():
                return 0
            out = pricing_poller.poll_once(SessionLocal, client)
            pricing_poller.check_staleness(SessionLocal)
            return 0 if out.ok else 1
        poller.run_loop(
            SessionLocal, client, poll_interval_s=settings.pricing_poll_interval_seconds, snapshot_interval_s=0,
            stop=stop, lock=lock, tick=heartbeat, poll=pricing_poller.poll_once, staleness=pricing_poller.check_staleness,
        )  # fmt: skip
        return 0
    finally:
        lock.release()
        client.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--role",
        choices=[
            "sms",
            "webhooks",
            "control_plane",
            "money_control_plane",
            "money_usage_reporter",
            "billing_usage_reporter",
            "pricing_control_plane",
            "sender_control_plane",
        ],
        default="sms",
    )
    ap.add_argument("--once", action="store_true", help="control_plane: një iteracion dhe dil")
    args = ap.parse_args()
    if args.role == "control_plane":
        sys.exit(run_control_plane(args.once))
    if args.role == "money_control_plane":
        sys.exit(run_money_control_plane(args.once))
    if args.role == "sender_control_plane":
        sys.exit(run_sender_control_plane(args.once))
    if args.role == "pricing_control_plane":
        sys.exit(run_pricing_control_plane(args.once))
    if args.role == "billing_usage_reporter":
        sys.exit(run_billing_usage_reporter(args.once))
    if args.role == "money_usage_reporter":
        sys.exit(run_money_usage_reporter(args.once))
    run() if args.role == "sms" else run_webhooks()
