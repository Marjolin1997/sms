"""M1b: kolona e re e identitetit të tenant-it. `enterprise_id` plotësohet AUTOMATIKISHT nga
`owner_ref` në një vend të vetëm (`app/core/tenancy.py`, ngjarja `before_flush`), jo nga shërbimet.
Është nullable dhe pa FK gjatë kalimit (M1c e bën të detyrueshme pasi backfill-i të verifikohet).
`owner_ref` mbetet burimi i së vërtetës për sjelljen; asgjë nuk lexon ende `enterprise_id`."""

import uuid

from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column


class TenantOwned:
    enterprise_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True, nullable=True)
