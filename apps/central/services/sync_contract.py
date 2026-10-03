"""Mapper: rresht `sync_outbox` → kontrata `cp.v1` → bytes. Vetëm nga rreshti (payload i ngrirë).

Pa Session, pa query: një ngjarje historike nuk rindërtohet kurrë nga gjendja e tanishme e DB-së.
`event_id` vjen nga rreshti (i njëjti rresht → i njëjti UUID); `seq`/`revision` po nga rreshti;
`occurred_at` = `created_at` i rreshtit (informativ).
"""

from apps.central.models.sync import SyncOutbox
from packages.contracts.control_plane import v1


def _state(row: SyncOutbox):
    p = row.payload
    try:
        if row.event_type == v1.EVENT_ENTERPRISE_UPSERTED:
            return v1.EnterpriseStateV1(p["enterprise_id"], p["name"], p["status"])
        if row.event_type == v1.EVENT_ENTERPRISE_PRODUCT_UPSERTED:
            product = p["product"]
            return v1.EnterpriseProductStateV1(
                p["assignment_id"], p["enterprise_id"], product["id"], product["code"],
                product["channel"], p["status"],
                p["rate_limit_per_min"] if "rate_limit_per_min" in p else None,  # i munguar = NULL
            )  # fmt: skip
    except (KeyError, TypeError) as e:
        raise v1.ContractError(f"outbox payload is malformed: {e!r}") from e
    raise v1.UnknownEventTypeError(f"unknown event type {row.event_type!r}")


def to_event(row: SyncOutbox) -> v1.ControlPlaneEventV1:
    state = _state(row)
    if row.entity_type != v1.ENTITY_BY_EVENT[row.event_type]:
        raise v1.ContractError("outbox entity_type does not match its event_type")
    return v1.ControlPlaneEventV1(
        event_id=str(row.event_id), seq=row.seq, type=row.event_type,
        enterprise_id=str(row.enterprise_id), entity_id=str(row.entity_id),
        revision=row.revision, occurred_at=row.created_at, data=state,
    )  # fmt: skip


def to_bytes(row: SyncOutbox) -> bytes:
    return to_event(row).to_bytes()
