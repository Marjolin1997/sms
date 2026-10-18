"""M11-J/G-03: kufizim për provider (shpejtësi + konkurrencë) pa varësi të jashtme dhe pa mbajtur transaksion DB duke pritur.

Parimi: mesazhet e një provider-i të kufizuar NUK merren fare nga worker-i (filtër në `reserve`), prandaj mbeten QUEUED, të paprekura
(asnjë përpjekje e djegur, asnjë ngjarje, asnjë dërgim i dyfishtë); worker-i thjesht kthen `None` dhe fle sipas `poll_seconds` (pa cikël të zënë).

  · `SMS_PROVIDER_RATE_PER_SEC={"twilio": 5}` — kovë tokenësh për PROCES (kapacitet = max(1, vlera)). Kufiri global ≤ vlera × worker-a SMS.
  · `SMS_PROVIDER_MAX_CONCURRENCY={"twilio": 2}` — thirrje njëkohësisht në ajër në të gjithë DB-në: numërohen SENDING me `dispatch_started_at`
    brenda lease-it. Best-effort: dy worker-a mund ta kalojnë kufirin me ≤ (workers − 1) mes kontrollit dhe claim-it; i saktë me një worker.
Pa konfigurim (parazgjedhja) `blocked()` kthen bosh pa asnjë kërkesë DB: sjellja e vjetër e pandryshuar. Nuk ka garanci të shpërndara më të forta se këto."""

import logging
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.core.config import settings
from app.models.sending import Message, MessageStatus

log = logging.getLogger("sms.throttle")
LOG_EVERY_S = 30.0


class ProviderThrottle:
    def __init__(self) -> None:
        self._buckets: dict[str, list[float]] = {}  # provider → [tokena, vula kohore (epoch s)]
        self._logged: dict[str, float] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._logged.clear()

    @staticmethod
    def enabled() -> bool:
        return bool(settings.provider_rate_per_sec or settings.provider_max_concurrency)

    def _refill(self, provider: str, rate: float, ts: float) -> list[float]:
        cap = max(1.0, rate)
        b = self._buckets.setdefault(provider, [cap, ts])
        b[0] = min(cap, b[0] + max(0.0, ts - b[1]) * rate)
        b[1] = max(b[1], ts)
        return b

    def blocked(self, db, now: datetime) -> dict[str, str]:
        """→ {provider: arsyeja} për provider-ët që s'duhen marrë tani ('rate' | 'concurrency'). Bosh nëse s'ka kufizime."""
        if not self.enabled():
            return {}
        out: dict[str, str] = {}
        ts = now.timestamp()
        with self._lock:
            for name, rate in settings.provider_rate_per_sec.items():
                if self._refill(name, rate, ts)[0] < 1.0:
                    out[name] = "rate"
        caps = settings.provider_max_concurrency
        if caps:
            lease = timedelta(seconds=settings.sending_lease_seconds)
            rows = db.execute(
                select(Message.provider, func.count())
                .where(
                    Message.provider.in_(list(caps)),
                    Message.status == MessageStatus.SENDING,
                    Message.dispatch_started_at.is_not(None),
                    Message.updated_at > now - lease,
                )
                .group_by(Message.provider)
            ).all()
            for name, n in rows:
                if n >= caps[name]:
                    out.setdefault(name, "concurrency")
        mono = time.monotonic()
        for name, why in out.items():
            if (
                mono - self._logged.get(name, -LOG_EVERY_S) >= LOG_EVERY_S
            ):  # një rresht për provider/30 s, jo për çdo cikël
                self._logged[name] = mono
                log.warning(
                    "provider throttled provider=%s reason=%s (QUEUED messages for it wait, nothing is dropped)",
                    name,
                    why,
                )
        return out

    def consume(self, provider: str, now: datetime) -> None:
        """Merr një token pasi mesazhi u rezervua (thirret nga i njëjti thread që kaloi `blocked`)."""
        rate = settings.provider_rate_per_sec.get(provider)
        if rate:
            with self._lock:
                b = self._refill(provider, rate, now.timestamp())
                b[0] = max(0.0, b[0] - 1.0)


throttle = ProviderThrottle()
