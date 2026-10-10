"""TOTP (RFC 6238): SHA-1, 6 shifra, hap 30 s. Pa varësi të jashtme."""

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP = 30
DIGITS = 6
WINDOW = 1  # toleranca ±1 hap për ndryshim ore


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    n = (struct.unpack(">I", mac[off : off + 4])[0] & 0x7FFFFFFF) % 10**DIGITS
    return str(n).zfill(DIGITS)


def verify(
    secret: str, code: str, last_step: int | None = None, now: float | None = None
) -> int | None:
    """→ hapi i pranuar (ruhet kundër riluajtjes) ose None. Një hap përdoret vetëm një herë."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    cur = int((time.time() if now is None else now) // STEP)
    for step in range(cur - WINDOW, cur + WINDOW + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(_code(secret, step), code):
            return step
    return None


def provisioning_uri(secret: str, account: str, issuer: str = "SMS Platform") -> str:
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&digits={DIGITS}&period={STEP}"
