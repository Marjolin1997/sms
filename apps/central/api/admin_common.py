"""Ndihmëse të përbashkëta të API-ve admin financiare (M9-f): skema strikte, faqosje, serializues."""

import re
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, ConfigDict, Field, StrictStr

from apps.central.services import money_common

_AMOUNT_RE = re.compile(r"^(0|[1-9]\d{0,13})(\.\d{1,6})?$")
_KEY_RE = r"^[A-Za-z0-9._:-]{8,128}$"


def _amount(v: str) -> str:
    if not _AMOUNT_RE.match(v):
        raise ValueError(
            "amount must be a plain decimal string (<= 14 integer digits, <= 6 decimals), never a float"
        )
    if Decimal(v) <= 0:
        raise ValueError("amount must be > 0")
    return v


def _price(v: str) -> str:
    if not re.match(r"^(0|[1-9]\d{0,13})(\.\d{1,6})?$", v):
        raise ValueError("price must be a plain decimal string (<= 6 decimals), never a float")
    return v


# Shumat/çmimet vijnë VETËM si string dhjetor (JSON number/float refuzohet nga StrictStr → 422).
Amount = Annotated[StrictStr, AfterValidator(_amount)]
UnitPrice = Annotated[StrictStr, AfterValidator(_price)]
Reason = Annotated[StrictStr, Field(min_length=1, max_length=500)]
Note = Annotated[StrictStr, Field(max_length=500)]
Currency = Annotated[StrictStr, Field(pattern=r"^[A-Z]{3}$")]
IdempotencyKey = Annotated[StrictStr, Field(pattern=_KEY_RE)]
Aware = AwareDatetime

STRICT = ConfigDict(extra="forbid")


def money_str(d) -> str:
    return format(Decimal(d).quantize(money_common.QUANT), "f")


def iso(d: datetime | None) -> str | None:
    return None if d is None else d.isoformat()


def page(items: list, limit: int, offset: int, serialize) -> dict:
    """Faqosje e qëndrueshme: kërko limit+1 për `has_more`; asnjë ORM nuk del jashtë."""
    more = len(items) > limit
    return {
        "items": [serialize(x) for x in items[:limit]],
        "limit": limit,
        "offset": offset,
        "has_more": more,
    }


def actor_of(user_id, label) -> dict | None:
    if user_id is None and label is None:
        return None
    return {"user_id": None if user_id is None else str(user_id), "label": label}
