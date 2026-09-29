"""Worker: python -m app.worker [--role sms|webhooks]

Dy role të ndara qëllimisht: një endpoint i ngadaltë i klientit (timeout 10s) nuk duhet të
bllokojë dërgimin e SMS/email."""

import argparse
import logging
import time
from datetime import timedelta

from app.core.config import settings
from app.core.db import SessionLocal
from app.providers import register_configured
from app.services import billing, emails, events, payments, webhooks
from app.services.campaigns import run_due
from app.services.messages import expire_stale, process_one

log = logging.getLogger("sms.worker")


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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=["sms", "webhooks"], default="sms")
    role = ap.parse_args().role
    run() if role == "sms" else run_webhooks()
