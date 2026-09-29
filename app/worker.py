"""Worker: python -m app.worker"""

import logging
import time
from datetime import timedelta

from app.core.config import settings
from app.core.db import SessionLocal
from app.providers import register_configured
from app.services import emails
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


def run(poll_seconds: float = 1.0, sweep_every: float = 60.0) -> None:
    register_configured()
    last_sweep = last_campaigns = 0.0
    while True:
        if time.monotonic() - last_sweep >= sweep_every:
            sweep()
            last_sweep = time.monotonic()
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
