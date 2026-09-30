"""`DispatchQueue`: kontrata për SMS/email (mbajtje me status, pa lease, at-most-once për crash).

Semantika që ruhet (provuar nga tests/test_queue_semantics.py dhe test_queue_concurrency_pg.py):
  • `reserve`: elementi i gatshëm më i vjetër `(next_attempt_at, id)`, `FOR UPDATE SKIP LOCKED`,
    `attempts += 1` (jo në retry), tranzicioni i domain-it përmes `hooks.reserved`. Asnjë commit.
  • `retry`: gabim i përhershëm → `hooks.failed`; i përkohshëm dhe `attempts < max` → planifikon
    `backoff_s * 2**(attempts-1)` + `hooks.requeued`; përndryshe `hooks.failed` (EXHAUSTED).
  • Kurrë commit: sesioni dhe transaksioni janë të thirrësit (claim → COMMIT → provider → COMMIT).
Adapteri nuk njeh statuset, paratë, ngjarjet apo provider-in: ato janë të `DispatchHooks`."""

import enum
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.sql.elements import ColumnElement


class Outcome(enum.StrEnum):
    RETRIED = "retried"  # planifikuar përsëri (hooks.requeued u thirr)
    FAILED = "failed"  # gabim i përhershëm (hooks.failed u thirr)
    EXHAUSTED = (
        "exhausted"  # gabim i përkohshëm, por attempts arriti kufirin (hooks.failed u thirr)
    )


@dataclass(frozen=True)
class DispatchSpec:
    """Çfarë duhet të dijë mekanika e queue-së për një model. Vjen nga service, jo nga adapteri."""

    model: type
    pending: ColumnElement  # "në pritje" (p.sh. status == QUEUED); e re-përdorur nga cancel
    attempts: Any  # kolona/atributi i numëruesit (InstrumentedAttribute)
    next_attempt_at: Any  # kolona e planifikimit
    id: Any  # kolona e identitetit (rendit dytësor dhe kërkimi sipas id)
    backoff_s: int = 30  # vonesa = backoff_s * 2 ** (attempts - 1)
    max_attempts: int = 5


class DispatchHooks(Protocol):
    """Tranzicionet e state machine-it dhe efektet anësore, të zotëruara nga service (`_move`,
    paratë, ngjarje domain). Thirren brenda transaksionit të thirrësit; asnjëri nuk bën commit."""

    def reserved(self, db, item) -> None: ...  # QUEUED → SENDING

    def requeued(self, db, item, error: str) -> None: ...  # SENDING → QUEUED (retry)

    def sent(self, db, item, provider_ref: str) -> None: ...  # SENDING → SENT

    def failed(self, db, item, reason: str) -> None: ...  # → FAILED (+ efekte domain)


class DispatchQueue(Protocol):
    def publish(self, db, item, *, not_before: datetime | None = None) -> Any:
        """Shton rreshtin në sesionin e thirrësit (flush, jo commit); `not_before` = planifikim."""

    def reserve(self, db, now: datetime) -> Any | None: ...

    def acknowledge(self, db, item, provider_ref: str) -> None: ...

    def retry(self, db, item, *, error: str, temporary: bool, now: datetime) -> Outcome: ...

    def fail(self, db, item, reason: str) -> None: ...

    def cancel_if_pending(self, db, item_id: int, *, reason: str) -> bool: ...


NowFn = Callable[[], datetime]
