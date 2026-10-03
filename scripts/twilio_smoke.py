"""Test i parë real kundër Twilio, pa kaluar nga platforma: dërgon NJË SMS.
    SMS_TWILIO_ACCOUNT_SID=AC... SMS_TWILIO_AUTH_TOKEN=... \\
    python -m scripts.twilio_smoke --to +355691234567 --from +1415... [--confirm]
Pa --confirm shfaq vetëm çfarë do të dërgonte. Numri duhet të jetë i verifikuar në llogarinë provë.
Çelësat merren nga mjedisi/.env, kurrë nga argumentet (mos i shkruani në histori komandash)."""

import argparse
import sys

from app.core.config import settings
from app.providers.base import ProviderError, SendRequest
from app.providers.twilio import TwilioProvider


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True, help="E.164, p.sh. +355691234567")
    ap.add_argument(
        "--from", dest="sender", required=True, help="numri Twilio (ose sender i miratuar)"
    )
    ap.add_argument("--text", default="Test nga SMS Platform")
    ap.add_argument("--confirm", action="store_true", help="dërgo vërtet")
    a = ap.parse_args()
    if not (settings.twilio_account_sid and settings.twilio_auth_token):
        print(
            "Vendosni SMS_TWILIO_ACCOUNT_SID dhe SMS_TWILIO_AUTH_TOKEN në mjedis ose .env",
            file=sys.stderr,
        )
        return 2
    req = SendRequest("smoke-test", a.sender.lstrip("+"), a.to.lstrip("+"), a.text, "GSM-7", 1)
    print(
        f"Do të dërgohet nga {a.sender} te {a.to}: {a.text!r} "
        f"(llogaria {settings.twilio_account_sid[:6]}…)"
    )
    if not a.confirm:
        print("Shtoni --confirm për ta dërguar vërtet.")
        return 0
    cb = f"{settings.public_base_url.rstrip('/')}/webhooks/twilio/status"
    try:
        res = TwilioProvider(settings.twilio_account_sid, settings.twilio_auth_token, cb).send(req)
    except ProviderError as e:
        print(f"DËSHTOI: {e.code} (i përkohshëm={e.temporary})", file=sys.stderr)
        return 1
    print(f"U pranua nga Twilio: {res.provider_message_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
