"""Token aksesi të Central: JWT HS256 i nënshkruar me `CENTRAL_AUTH_SECRET` (PyJWT).

- Algoritmi është konstant (HS256), jo i konfigurueshëm dhe jo i lexuar nga token-i.
- Claims: iss, aud, sub (user id), iat, exp, jti. Roli dhe statusi NUK janë në token: lexohen nga
  DB në çdo kërkesë, ndaj çaktivizimi/ndryshimi i rolit vlen menjëherë.
- Pa revokim për token të veçantë (stateless): një token i vlefshëm punon deri në `exp` për
  përdorues aktiv; TTL i shkurtër (default 900 s) dhe nuk ka refresh token në M4-d.
"""

import uuid
from datetime import UTC, datetime, timedelta

import jwt

from apps.central.core.config import settings

ALGORITHM = "HS256"
ISSUER = "sms-central"
AUDIENCE = "sms-central-admin"
MIN_SECRET_LENGTH = 32


class TokenError(Exception):
    pass


def configured() -> bool:
    return len(settings.auth_secret) >= MIN_SECRET_LENGTH


def issue(
    user_id: uuid.UUID, now: datetime | None = None, ttl: int | None = None
) -> tuple[str, int]:
    """→ (token, expires_in sekonda)."""
    if not configured():
        raise TokenError("auth secret is not configured")
    now = now or datetime.now(UTC)
    ttl = settings.auth_ttl_seconds if ttl is None else ttl
    claims = {
        "iss": ISSUER, "aud": AUDIENCE, "sub": str(user_id), "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()), "exp": int((now + timedelta(seconds=ttl)).timestamp()),
    }  # fmt: skip
    return jwt.encode(claims, settings.auth_secret, algorithm=ALGORITHM), ttl


def decode(token: str) -> uuid.UUID:
    """→ user id nga token i vlefshëm; çdo problem (formë, nënshkrim, skadim) → TokenError."""
    if not configured():
        raise TokenError("auth secret is not configured")
    try:
        claims = jwt.decode(
            token, settings.auth_secret, algorithms=[ALGORITHM], audience=AUDIENCE, issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "iss", "aud"]},
        )  # fmt: skip
        return uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError, TypeError, KeyError) as e:
        raise TokenError("invalid token") from e
