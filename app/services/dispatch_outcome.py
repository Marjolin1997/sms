"""M9-a: logjika e përbashkët (SMS + email) për rezultatin e thirrjes së provider-it dhe rimarrjen e
SENDING të ngecur. Pa para, pa model: vetëm vendime mbi `queue`/`provider`.

Rregullat (kurrë ridërgim pas dyshimi pa idempotencë të provuar):
  * provider-i definitivisht s'e pranoi (`ambiguous=False`): sjellja e vjetër (retry i përkohshëm /
    dështim i përhershëm);
  * rezultat i paqartë (`ambiguous=True`, ose përjashtim i papritur PAS fillimit të thirrjes):
      - provider me `idempotent_by_reference == True` DHE `attempts < max` ⇒ retry me të njëjtën
        `reference` (e pandryshueshme: `public_id`);
      - përndryshe ⇒ UNKNOWN (hold-i/gjendja pa ndryshim; vendimin e merr DLR ose stafi).
  * përjashtim para fillimit të thirrjes (regjistri, kërkesa/MIME) ⇒ i përkohshëm, i sigurt.
"""

from dataclasses import dataclass
from datetime import datetime

from app.providers.base import is_idempotent


@dataclass(slots=True)
class RecoverReport:
    requeued: int = 0
    unknown: int = 0
    failed: int = 0


def settle_error(
    queue, db, item, provider, *, code: str, temporary: bool, ambiguous: bool, now: datetime
) -> None:
    """Zbaton rregullat e mësipërme mbi një gabim të thirrjes (SENDING → QUEUED|FAILED|UNKNOWN)."""
    if not ambiguous:
        queue.retry(db, item, error=code, temporary=temporary, now=now)
        return
    spec = queue.spec
    attempts = getattr(item, spec.attempts.key)
    if is_idempotent(provider) and attempts < spec.max_attempts:
        queue.retry(db, item, error=code, temporary=True, now=now)  # e njëjta reference ⇒ i sigurt
    else:
        queue.unknown(db, item, code)


def lock_claim(db, item, claim_attempts: int, running) -> bool:
    """Rilexon rreshtin me FOR UPDATE dhe kthen True vetëm nëse ky punonjës e ka ende claim-in
    (statusi `running` dhe i njëjti `attempts`). Mbron nga sweeper-i/DLR që e lëvizën ndërkohë."""
    db.refresh(item, with_for_update=True)
    return item.status == running and item.attempts == claim_attempts
