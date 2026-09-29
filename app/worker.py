"""Worker: python -m app.worker"""

import logging
import time

from app.core.db import SessionLocal
from app.services.messages import process_one

log = logging.getLogger("sms.worker")


def run(poll_seconds: float = 1.0) -> None:
    while True:
        with SessionLocal() as db:
            try:
                m = process_one(db)
            except Exception:
                db.rollback()
                log.exception("worker cycle failed")
                m = None
        if m is None:
            time.sleep(poll_seconds)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
