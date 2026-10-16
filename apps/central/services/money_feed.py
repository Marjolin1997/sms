"""Feed-i `cp.money.v1` (M9-c): vetëm lexim, pa gjendje per konsumator, pa mutacion.

Mekanika është ajo e `sync_feed` (M7): `latest_seq` lexohet NJË herë (vlera e commit-uar e `money_sequence`;
rritet në të njëjtin tx me `money_events` dhe `seq` alokohet nën kyç ⇒ çdo `seq <= latest` është i dukshëm);
`next_seq` = `seq` i fundit i faqes nëse ka më shumë, përndryshe `latest_seq` (kapërcimi i seq-ve të
enterprise-eve të tjera është i sigurt: bashkësia e autorizuar ndryshon vetëm me `auth_generation`).
Autorizimi është per enterprise (`service_client_enterprises`) dhe scope i dedikuar `money:read`.
Dallimet nga `cp.v1`: s'ka snapshot (ngjarjet janë historia financiare e plotë; riprodhimi nga 0 është
idempotent te konsumatori) dhe s'ka `floor_seq` (ngjarjet e parave s'pastrohen). Epoka/generation
ndryshuara ⇒ 409 (konsumatori NUK vazhdon në heshtje).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.models.money import MoneyEvent, MoneySequence
from apps.central.services import money_contract, service_auth
from apps.central.services.sync_feed import SyncApiError

_SEQ = MoneySequence.__table__
MAX_LIMIT = 500


class MoneyFeedError(
    SyncApiError
):  # i njëjti handler HTTP; `action` ≠ snapshot (s'ka snapshot parash)
    status = 409
    code = "money_feed_error"
    action = "replay_or_operator"


class EpochMismatch(MoneyFeedError):
    status, code = 409, "money_epoch_mismatch"


class AuthorizationChanged(MoneyFeedError):
    status, code = 409, "money_authorization_changed"


class CursorAhead(MoneyFeedError):
    status, code = 409, "money_cursor_ahead"


def read_state(db: Session) -> tuple[uuid.UUID, int]:
    row = db.execute(select(_SEQ.c.epoch, _SEQ.c.last_seq).where(_SEQ.c.id == 1)).one()
    return row.epoch, int(row.last_seq)


def state(db: Session, client_pk) -> dict:
    epoch, latest = read_state(db)
    generation, _allowed = service_auth.allowed_enterprises(db, client_pk)
    return {"epoch": str(epoch), "authorization_generation": generation, "latest_seq": latest}


def changes(
    db: Session, client_pk, after_seq: int, limit: int, epoch: uuid.UUID, generation: int
) -> dict:
    limit = max(1, min(limit, MAX_LIMIT))
    feed_epoch, latest = read_state(db)
    if epoch != feed_epoch:
        raise EpochMismatch("money feed epoch changed; operator action required")
    current_generation, allowed = service_auth.allowed_enterprises(db, client_pk)
    if generation != current_generation:
        raise AuthorizationChanged("authorization changed; replay from seq 0 is required")
    if after_seq > latest:
        raise CursorAhead("cursor is ahead of the money feed")
    rows: list[MoneyEvent] = []
    if allowed:
        rows = list(
            db.scalars(
                select(MoneyEvent)
                .where(MoneyEvent.enterprise_id.in_(allowed), MoneyEvent.seq > after_seq,
                       MoneyEvent.seq <= latest)
                .order_by(MoneyEvent.seq)
                .limit(limit + 1)
            )
        )  # fmt: skip
    has_more = len(rows) > limit
    page = rows[:limit]
    next_seq = page[-1].seq if has_more else max(latest, after_seq)
    return {
        "epoch": str(feed_epoch),
        "authorization_generation": current_generation,
        "events": [money_contract.to_event(r).to_dict() for r in page],
        "next_seq": next_seq,
        "latest_seq": latest,
        "has_more": has_more,
    }
