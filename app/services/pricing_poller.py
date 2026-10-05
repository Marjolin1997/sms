"""M9-e: orkestrimi i sinkronizimit të çmimeve (pull). Vetëm roli worker `pricing_control_plane` e thërret; rruga e dërgimit s'e importon.

authority=local ⇒ idle. Çdo iteracion: `GET /internal/pricing/snapshot` me (epoch, revision, generation) të aplikuar; `changed=false` ⇒ vetëm
`last_success_at`; përndryshe parse (verifikon hash-et) → `apply_snapshot` atomik → commit. Dështim (rrjet/auth/5xx/përmbajtje e prishur) ⇒
FAIL-STATIC: cache-i i fundit i plotë mbetet në përdorim, `last_error` regjistrohet, asnjë çmim s'shpikët. Vjetërsia vetëm alarmon (readiness)."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.timeutil import utcnow
from app.services import pricing_sync as ps
from app.services.control_plane_client import (
    ControlPlaneClient,
    CpAuthError,
    CpError,
    CpForbidden,
    CpProtocolError,
    CpTransportError,
)
from packages.contracts.control_plane.pricing import v1 as pv

log = logging.getLogger("sms.pricing.poller")
LOCK_KEY = 0x534D535052  # "SMSPR"
OK, DISABLED = "ok", "disabled"


@dataclass(slots=True)
class PollOutcome:
    kind: str = (
        OK  # ok | disabled | auth_error | forbidden | network_error | protocol_error | apply_error
    )
    outcome: str = ""  # applied | noop | stale | unchanged
    revision: int | None = None
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


def _note(factory, message: str, now: datetime) -> None:
    try:
        with factory() as db:
            ps.record_error(db, message, now)
            _commit(db)
    except Exception:  # noqa: BLE001
        log.exception("could not record pricing sync error")


def poll_once(
    factory: Callable[[], Session], client: ControlPlaneClient, *, now: datetime | None = None, **_
) -> PollOutcome:
    now = now or utcnow()
    out = PollOutcome()
    if settings.pricing_authority == "local":
        out.kind = DISABLED
        return out
    try:
        with factory() as db:
            st = ps.get_state(db)
            known = (
                None if st.epoch is None else str(st.epoch),
                st.revision,
                st.authorization_generation,
            )
            db.rollback()
        resp = client.get_pricing_snapshot(*known)
        if not resp["changed"]:
            with factory() as db:
                ps.mark_success(db, now)
                _commit(db)
            out.outcome = "unchanged"
            return out
        snap = pv.PricingSnapshotV1.parse(
            resp["snapshot"]
        )  # hash-et verifikohen këtu: i paplotë ⇒ ContractError
        with factory() as db:
            r = ps.apply_snapshot(db, snap, now=now)
            _commit(db)
        out.outcome, out.revision = r.outcome, r.revision
        log.info("pricing snapshot %s revision=%d versions+%d rules+%d retired=%d", r.outcome, r.revision,
                 r.new_versions, r.new_rules, r.retired)  # fmt: skip
    except CpAuthError as e:
        out.kind, out.detail = "auth_error", str(e)
        log.error("central auth failed (pricing): %s", e)
    except CpForbidden as e:
        out.kind, out.detail = "forbidden", str(e)
        log.error(
            "central denied pricing access (needs scope pricing:read + enterprise grant): %s", e
        )
    except CpTransportError as e:
        out.kind, out.detail = "network_error", str(e)
        log.warning(
            "central unreachable (pricing; last complete snapshot stays in use, fail-static): %s", e
        )
    except (pv.ContractError, ps.PricingApplyError) as e:
        out.kind, out.detail = "apply_error", f"{type(e).__name__}: {e}"
        log.error("ALERT pricing snapshot rejected (not activated): %s", out.detail)
        _note(factory, out.detail, now)
    except (CpProtocolError, CpError, KeyError, TypeError) as e:
        out.kind, out.detail = "protocol_error", f"{type(e).__name__}: {e}"
        log.error("central pricing protocol problem: %s", e)
    return out


def check_staleness(factory: Callable[[], Session], now: datetime | None = None) -> float | None:
    with factory() as db:
        age = ps.sync_age_seconds(ps.get_state(db), now)
        db.rollback()
    if age is None:
        log.warning("pricing sync has never succeeded")
    elif age > settings.pricing_stale_fail_seconds:
        log.error("pricing sync is stale age_s=%.0f (> %d s); last complete snapshot still in use (fail-static)", age,
                  settings.pricing_stale_fail_seconds)  # fmt: skip
    elif age > settings.pricing_stale_warn_seconds:
        log.warning("pricing sync age_s=%.0f exceeds the warning threshold", age)
    return age
