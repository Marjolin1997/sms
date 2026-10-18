"""Nënshkrimi V1 i webhook-ut dalës: `X-SMS-Signature: t=<unix>,v1=<hex>`.

Modul leaf, i pastër, stdlib-only: merr sekretin si string (ruajtja/Fernet jeton diku tjetër).
Algoritmi është i ngrirë (golden: `tests/test_webhook_golden.py`); ndryshimi kërkon version të ri.
NUK lidhet me DLR hyrës (`sha256=<hex>`, `app/api/webhooks.py`): kontratë tjetër.
"""

import hashlib
import hmac
import time

TOLERANCE_S = 300


def sign_v1(secret: str, timestamp: int, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"t={timestamp},v1={mac.hexdigest()}"


def verify_v1(
    secret: str, header: str, body: bytes, tolerance: int = TOLERANCE_S, now=None
) -> bool:
    """Ashtu si e verifikon marrësi (me mbrojtje replay). Sjellja e ruajtur 1:1: çift pa `=`,
    `t` mungon/jo-int → False; çelës i përsëritur: fiton i fundit; `now or time.time()`."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (KeyError, ValueError):
        return False
    if abs((now or time.time()) - ts) > tolerance:
        return False
    expected = sign_v1(secret, ts, body).split("v1=")[1]
    return hmac.compare_digest(parts.get("v1", ""), expected)
