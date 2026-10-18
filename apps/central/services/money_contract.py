"""Mapper: rresht `money_events` → kontrata `cp.money.v1` → bytes. VETËM nga payload-i i ngrirë i rreshtit.

Pa Session, pa query: një ngjarje historike s'rindërtohet kurrë nga gjendja e tanishme e grantit/llogarisë.
`event_id`, `seq`, `occurred_at` (= `created_at` i rreshtit) vijnë nga rreshti. Ngjarjet e M9-b pa `purpose`
lexohen si `standard` (fusha e munguar = e vetmja semantikë që ekzistonte).
"""

from decimal import Decimal, InvalidOperation

from apps.central.models.money import MoneyEvent
from packages.contracts.control_plane.money import v1


def to_event(row: MoneyEvent) -> v1.MoneyEventV1:
    p = row.payload
    try:
        data = v1.GrantDataV1(
            account_id=p["account_id"], product_id=p["product_id"], currency=p["currency"],
            amount=Decimal(p["amount"]), purpose=p.get("purpose", v1.PURPOSE_STANDARD),
            baseline_ref=p.get("baseline_ref"),
        )  # fmt: skip
        if p["grant_id"] != str(row.entity_id) or p["enterprise_id"] != str(row.enterprise_id):
            raise v1.ContractError("payload identity does not match the event row")
    except (KeyError, TypeError, InvalidOperation) as e:
        raise v1.ContractError(f"money payload is malformed: {e!r}") from e
    return v1.MoneyEventV1(
        event_id=str(row.event_id), seq=row.seq, event_type=row.event_type,
        enterprise_id=str(row.enterprise_id), grant_id=str(row.entity_id),
        occurred_at=row.created_at, data=data,
    )  # fmt: skip


def to_bytes(row: MoneyEvent) -> bytes:
    return to_event(row).to_bytes()
