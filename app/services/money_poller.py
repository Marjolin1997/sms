"""M9-c: orkestrimi i feed-it `cp.money.v1` (pull). Lidh `ControlPlaneClient` (transport, scope `money:read`)
me `money_sync` (aplikues). VETËM roli worker `money_control_plane` e thërret; rruga e dërgimit s'e importon.

`poll_once`: authority=local ⇒ asgjë. Kursor bosh ⇒ `/state` → init (kursor 0, riprodhim i plotë idempotent).
Faqe → parse (të gjitha ose asgjë) → `apply_batch` → commit. 409:
  - `money_authorization_changed` ⇒ rebase në generation-in e ri, riprodhim nga 0 (idempotent);
  - `money_epoch_mismatch` / `money_cursor_ahead` ⇒ NUK vazhdon: regjistron gabim, veprim operatori;
Dështim (rrjet/auth/5xx) ⇒ fail-static: asgjë s'çaktivizohet, kredia e sinkronizuar mbetet e shpenzueshme.
`last_success_at` përditësohet vetëm nga aplikuesi pas aplikimit të suksesshëm (jo nga HTTP 200)."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import utcnow
from app.services import money_sync as ms
from app.services.control_plane_client import (
    ControlPlaneClient,
    CpAuthError,
    CpError,
    CpForbidden,
    CpMoneyConflict,
    CpProtocolError,
    CpTransportError,
)
from packages.contracts.control_plane.money.v1 import ContractError

log = logging.getLogger("sms.money.poller")

MAX_PAGES = 50
PAGE_LIMIT = 200
LOCK_KEY = 0x534D534D4F  # "SMSMO": kyç advisory i veçantë nga ai i cp.v1
OK, DISABLED = "ok", "disabled"


@dataclass(slots=True)
class PollOutcome:
    kind: str = OK  # ok | disabled | auth_error | forbidden | network_error | protocol_error | apply_error | operator
    pages: int = 0
    applied: int = 0
    noop: int = 0
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.kind in (OK, DISABLED)


def _commit(db: Session) -> None:
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise


def _feed(factory, client: ControlPlaneClient, now: datetime, out: PollOutcome) -> None:
    for _ in range(MAX_PAGES):
        with factory() as db:
            cur = ms.get_cursor(db)
            epoch, gen, after = cur.epoch, cur.authorization_generation, cur.last_seq
        if epoch is None or gen is None:
            raise ms.CursorMismatch("no_cursor")
        page = client.get_money_changes(after, epoch, gen, PAGE_LIMIT)
        try:
            events = ms.parse_events(page.events)  # të gjitha ose asgjë (parsimi është i pastër)
        except ContractError as e:
            with factory() as db:
                ms.record_error(db, f"unparseable money event: {e}", now)
                _commit(db)
            raise
        with factory() as db:
            r = ms.apply_batch(
                db, epoch=page.epoch, authorization_generation=page.authorization_generation,
                events=events, next_seq=page.next_seq, now=now,
            )  # fmt: skip
            _commit(db)
        out.pages += 1
        out.applied += r.applied
        out.noop += r.noop
        log.info("money poll events=%d applied=%d noop=%d cursor=%d latest=%d more=%s",
                 len(events), r.applied, r.noop, r.last_seq, page.latest_seq, page.has_more)  # fmt: skip
        if r.error:
            raise ms.EventConflict(r.error)
        if not page.has_more:
            return
        if page.next_seq <= after:
            raise CpProtocolError("has_more with no cursor progress")


def poll_once(
    factory: Callable[[], Session], client: ControlPlaneClient, *, now: datetime | None = None, **_
) -> PollOutcome:
    now = now or utcnow()
    out = PollOutcome()
    if settings.money_authority == "local":
        out.kind = DISABLED
        return out
    try:
        with factory() as db:
            uninit = ms.get_cursor(db).epoch is None
        if uninit:
            st = client.get_money_state()
            with factory() as db:
                ms.init_cursor(db, st["epoch"], st["generation"])
                _commit(db)
        try:
            _feed(factory, client, now, out)
        except CpMoneyConflict as e:
            if e.code != "money_authorization_changed":
                raise
            st = client.get_money_state()
            with factory() as db:
                ms.rebase_generation(db, st["epoch"], st["generation"])
                _commit(db)
            log.warning("money authorization changed: replaying from seq 0 (idempotent)")
            _feed(factory, client, now, out)
        with (
            factory() as db
        ):  # grant-et e regjistruara/unmapped rivlerësohen edhe kur faqja është bosh
            ms.drain_deferred(db, now)
            _commit(db)
    except CpAuthError as e:
        out.kind, out.detail = "auth_error", str(e)
        log.error("central auth failed (money): %s", e)
    except CpForbidden as e:
        out.kind, out.detail = "forbidden", str(e)
        log.error("central denied money access (needs scope money:read + enterprise grant): %s", e)
    except CpTransportError as e:
        out.kind, out.detail = "network_error", str(e)
        log.warning("central unreachable (money; local credit stays spendable, fail-static): %s", e)
    except CpMoneyConflict as e:
        out.kind, out.detail = "operator", f"{e.code}: operator action required"
        log.error("ALERT money feed conflict %s: operator action required", e.code)
        _note(factory, out.detail, now)
    except ms.CursorMismatch as e:
        out.kind, out.detail = "operator", f"cursor {e.reason}"
        log.error("ALERT money cursor problem: %s", e.reason)
    except (ms.EventConflict, ms.MoneySyncError, ContractError) as e:
        out.kind, out.detail = "apply_error", f"{type(e).__name__}: {e}"
        log.error("ALERT could not apply money events (cursor held): %s: %s", type(e).__name__, e)
    except CpProtocolError as e:
        out.kind, out.detail = "protocol_error", str(e)
        log.error("central money protocol problem: %s", e)
    except CpError as e:
        out.kind, out.detail = "protocol_error", str(e)
        log.error("central client error (money): %s", e)
    return out


def _note(factory, message: str, now: datetime) -> None:
    try:
        with factory() as db:
            ms.record_error(db, message, now)
            _commit(db)
    except Exception:  # noqa: BLE001  (shënimi s'duhet të mbulojë gabimin origjinal)
        log.exception("could not record money cursor error")


def check_staleness(factory: Callable[[], Session], now: datetime | None = None) -> float | None:
    """Vetëm log/alarm. KURRË çaktivizim automatik: kredia e sinkronizuar mbetet e shpenzueshme."""
    with factory() as db:
        age = ms.sync_age_seconds(ms.get_cursor(db), now)
    if age is None:
        log.warning("money sync has never succeeded")
    elif age > ms.SLO_AGE_S:
        log.error("money sync is stale age_s=%.0f (> %d s); balances unchanged (fail-static)",
                  age, ms.SLO_AGE_S)  # fmt: skip
    return age
