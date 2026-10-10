"""Dërgon email-et e verifikimit nga outbox (M8-e). Jashtë API-së dhe jashtë çdo transaksioni DB.

    python -m apps.central.tools.send_notifications            # një kalim (cron/systemd timer)
    python -m apps.central.tools.send_notifications --loop --interval 15

Kërkon `CENTRAL_MAILER=smtp|fake` + `CENTRAL_REGISTRATION_VERIFY_KEY`. Retry i kufizuar (5 përpjekje,
backoff 1m·4^n); asnjë retry i pafund. Kodet: 0 ok · 2 konfigurim i munguar.
"""

import argparse
import sys
import time

from sqlalchemy.orm import sessionmaker

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.services import contact_verification, mailer, notifications


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Send pending registration verification emails.")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=int, default=15)
    a = ap.parse_args(argv)
    m = mailer.get_mailer()
    if m is None or not contact_verification.key_configured():
        print("mailer/verification key not configured", file=sys.stderr)
        return 2
    factory = sessionmaker(bind=make_engine(settings.database_url), expire_on_commit=False)
    while True:
        r = notifications.dispatch_due(factory, m)
        print(f"sent={r.sent} retried={r.retried} failed={r.failed} superseded={r.superseded}")
        if not a.loop:
            return 0
        time.sleep(max(1, a.interval))


if __name__ == "__main__":
    sys.exit(main())
