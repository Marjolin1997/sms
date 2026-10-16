"""Ndihmëse të përbashkëta të domain-it të parave (M9-b): validim Decimal, aktorë, arsye."""

import re
import uuid
from decimal import Decimal, InvalidOperation

from apps.central.core.errors import Forbidden, Invalid
from apps.central.models.user import CentralUser, Role

QUANT = Decimal("0.000001")
MAX_AMOUNT = Decimal("9999999999999.999999")  # NUMERIC(20,6): 14 shifra para presjes
REASON_MAX = 500
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_LABEL = re.compile(r"^system:[a-z][a-z0-9_.-]{0,56}$")
_SOURCE = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")


def money(value) -> Decimal:
    """Vetëm Decimal/str/int (kurrë float/bool), i fundëm, ≤ 6 shifra pas presjes, 0 < x ≤ MAX."""
    if (
        isinstance(value, bool)
        or isinstance(value, float)
        or not isinstance(value, Decimal | str | int)
    ):
        raise Invalid("amount must be a Decimal, string or integer (never float)")
    try:
        d = Decimal(value)
    except InvalidOperation:
        raise Invalid("invalid amount") from None
    if not d.is_finite() or d != d.quantize(QUANT):
        raise Invalid("amount must be finite with at most 6 decimal places")
    d = d.quantize(QUANT)
    if d <= 0 or d > MAX_AMOUNT:
        raise Invalid("amount must be > 0 and within range")
    return d


def currency(value) -> str:
    c = value.strip().upper() if isinstance(value, str) else ""
    if not _CURRENCY.match(c):
        raise Invalid("currency must be a 3-letter ISO-4217 style code")
    return c


def reason(value) -> str:
    if not isinstance(value, str):
        raise Invalid("a reason is required")
    r = value.strip()
    if not r or len(r) > REASON_MAX or _CTRL.search(r):
        raise Invalid(f"reason must be 1..{REASON_MAX} characters without control characters")
    return r


def optional_text(value, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise Invalid(f"{field} must be a string")
    v = value.strip()
    if len(v) > REASON_MAX or _CTRL.search(v):
        raise Invalid(f"{field} is too long or has control characters")
    return v or None


def idempotency_key(value) -> str:
    if not isinstance(value, str) or not _KEY.match(value):
        raise Invalid("idempotency_key must be 8..128 chars of [A-Za-z0-9._:-]")
    return value


def source_name(value) -> str:
    if not isinstance(value, str) or not _SOURCE.match(value):
        raise Invalid("invalid payment source")
    return value


def external_reference(value) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or _CTRL.search(value):
        raise Invalid("invalid external_reference")
    return value.strip()


def admin(actor) -> CentralUser:
    """Çdo mutacion parash: vetëm CentralUser njeri me rol admin (operator = lexim). Pa përdorues të rremë."""
    if not isinstance(actor, CentralUser):
        raise Invalid("a human Central admin is required")
    if actor.role != Role.ADMIN.value:
        raise Forbidden("money operations require the admin role")
    return actor


def system_label(label) -> str:
    if not isinstance(label, str) or not _LABEL.match(label):
        raise Invalid("invalid system actor label (system:<name>)")
    return label


def uid(value, what: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise Invalid(f"invalid {what}") from None
