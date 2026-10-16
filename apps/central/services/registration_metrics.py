"""Numërues + log të strukturuar për regjistrimin (M8-e). Pa backend metrics në projekt (shih
`control_plane_shadow`): numëruesit janë në-proçes (`snapshot()`); eksporti (Prometheus/StatsD) është
borxh i dokumentuar. Logu i strukturuar (`event=… key=value`) është kanali operacional real.
Fusha të lejuara VETËM nga lista e sigurt: asnjëherë token/hash/email/trup/Authorization."""

import logging
import re
import threading
from collections import Counter

log = logging.getLogger("central.registration")

NAMES = (
    "registration_submitted_total",
    "registration_rejected_quota_total",
    "registration_verified_total",
    "registration_verification_failed_total",
    "registration_approved_total",
    "registration_auto_approved_total",
    "registration_provisioned_total",
    "registration_provision_failed_total",
    "registration_public_4xx_total",
    "registration_public_5xx_total",
    "registration_verification_email_sent_total",
    "registration_verification_email_failed_total",
)
SAFE_FIELDS = frozenset({
    "registration_id", "result", "error_code", "products", "decision_mode",
    "provisioning_status", "request_id", "ip", "attempt", "created", "status",
})  # fmt: skip
_lock = threading.Lock()
_counters: Counter = Counter()
_RID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def inc(name: str, n: int = 1) -> None:
    if name not in NAMES:
        raise KeyError(name)
    with _lock:
        _counters[name] += n


def snapshot() -> dict:
    with _lock:
        return {n: _counters.get(n, 0) for n in NAMES}


def reset() -> None:  # vetëm për teste
    with _lock:
        _counters.clear()


def request_id(value: str | None) -> str | None:
    return value if value and _RID.match(value) else None


def event(name: str, **fields) -> None:
    """Një rresht log i strukturuar; fushat jashtë listës së sigurt hidhen poshtë."""
    safe = {k: v for k, v in fields.items() if k in SAFE_FIELDS and v is not None}
    log.info(
        "registration event=%s %s", name, " ".join(f"{k}={v}" for k, v in sorted(safe.items()))
    )
