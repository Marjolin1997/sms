"""`DeliveryQueue`: kontrata për dërgime me lease (outbox transaksional, AT-LEAST-ONCE).

Ndryshe nga `DispatchQueue` (status SENDING, at-most-once për crash), këtu:
  • rezervimi është LEASE: `reserve` e lë elementin "në pritje" por shtyn `next_attempt_at` me
    `lease_s` dhe rrit `attempts`; nuk ka status të ri. Nëse worker-i vdes, lease skadon dhe
    elementi merret sërish (dublikim i mundshëm; pranuesi deduplikon me identitetin e elementit);
  • retry ka listë EKSPLICITE vonesash (`retry_delays_s`), jo formulë;
  • `publish` vetëm shton rreshtat në sesionin e thirrësit (outbox transaksional: dalin ose
    zhduken bashkë me transaksionin e biznesit); asnjë commit/rollback kurrë.
Adapteri nuk njeh HTTP, nënshkrime, payload, politikën e endpoint-it apo ngjarjet (hooks)."""

import enum
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement


class DeliveryOutcome(enum.StrEnum):
    RETRIED = "retried"  # planifikuar sipas retry_delays_s
    FAILED = "failed"  # i përhershëm: pa retry (hooks.failed)
    EXHAUSTED = "exhausted"  # përpjekjet mbaruan (hooks.failed)


@dataclass(frozen=True)
class DeliverySpec:
    """Forma e query-t dhe parametrat e planifikimit, të dhëna nga service (jo nga adapteri)."""

    model: type
    base: Callable[[], Select]  # SELECT i elementit me JOIN-et e domain-it (p.sh. endpoint)
    eligible: Callable[
        [datetime], ColumnElement
    ]  # "në pritje, due, dhe i lejuar" në momentin `now`
    attempts: Any  # kolona e numëruesit
    next_attempt_at: Any  # kolona e planifikimit/lease
    id: Any
    lease_s: int = 120
    retry_delays_s: Sequence[int] = (30, 120, 600, 1800, 7200, 21600, 43200)

    @property
    def max_attempts(self) -> int:
        return len(self.retry_delays_s) + 1


class DeliveryHooks(Protocol):
    """Efektet e domain-it; thirren brenda transaksionit të thirrësit, asnjëri nuk bën commit."""

    def completed(self, db, item, now: datetime) -> None: ...  # sukses

    def failed(
        self, db, item, outcome: DeliveryOutcome
    ) -> None: ...  # FAILED + politika e domain-it

    def replayed(self, db, item) -> None: ...  # kthim në "në pritje" (identiteti ruhet)


class DeliveryQueue(Protocol):
    def publish(self, db, items) -> None: ...  # db.add; kurrë commit

    def reserve(self, db, now: datetime) -> Any | None: ...  # due + eligible, SKIP LOCKED, lease

    def lock(self, db, item_id: int, *, where=None) -> Any | None: ...  # rilexim me FOR UPDATE

    def complete(self, db, item, now: datetime) -> None: ...

    def retry(self, db, item, *, now: datetime, permanent: bool) -> DeliveryOutcome: ...

    def fail(self, db, item, outcome: DeliveryOutcome) -> None: ...

    def replay(self, db, item, *, now: datetime) -> None: ...
