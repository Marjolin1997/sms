"""Të dhëna për E2E të konsolës Central.

VETËM DB e përkohshme e zhvillimit (CENTRAL_DATABASE_URL), kurrë prodhim.

    CENTRAL_DATABASE_URL=sqlite:///... CENTRAL_AUTH_SECRET=... \\
    E2E_ADMIN_PASSWORD=... E2E_OPERATOR_PASSWORD=... python e2e/central_seed.py <out.json>

Krijon: admin + operator, 2 enterprise, politika (XK pa miratim, DE e ndaluar), sender-a
pending / auto-miratuar / refuzuar nga politika dhe një provë kërkese nga Enterprise.
Shkruan në <out.json> vetëm ID-të e krijuara (pa sekrete).
"""

import json
import os
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.central.core.config import settings
from apps.central.core.db import make_engine
from apps.central.models.enterprise import Enterprise
from apps.central.models.sender import SenderRequestOperation
from apps.central.models.user import CentralUser
from apps.central.services import senders as svc
from apps.central.tools import create_admin


def main(out: str) -> None:
    if not settings.database_url.startswith("sqlite") or "prod" in settings.database_url:
        raise SystemExit("refusing: use a temporary SQLite development database")
    engine = make_engine()
    for email, role, env in (
        ("admin@e2e.test", "admin", "E2E_ADMIN_PASSWORD"),
        ("operator@e2e.test", "operator", "E2E_OPERATOR_PASSWORD"),
    ):
        code, msg = create_admin.run(email, os.environ[env], role, engine)
        assert code == 0, msg
    ids: dict = {}
    with Session(engine) as db:
        admin = db.scalar(select(CentralUser).where(CentralUser.email == "admin@e2e.test"))
        e1, e2 = (Enterprise(id=uuid.uuid4(), name=n) for n in ("Acme Ltd", "Globex"))
        db.add_all([e1, e2])
        db.flush()
        ids["enterprise_acme"], ids["enterprise_globex"] = str(e1.id), str(e2.id)
        svc.set_policy(db, admin, "XK", "alphanumeric", True, False, "trusted market")
        svc.set_policy(db, admin, "DE", "alphanumeric", False, True, "closed market")
        for key, ent, country, value in (
            ("pending_acme", e1, "AL", "ACMEPEND"),
            ("pending_globex", e2, "AL", "GLOBEXPEND"),
            ("auto_ok", e1, "XK", "AUTOOK"),
            ("denied", e1, "DE", "DENIED1"),
        ):
            r = svc.request_sender(
                db, admin, ent.id, f"e2e:{key}", country, value, None, source="admin"
            )
            ids[key] = str(r.sender.id)
        # provë e kërkesave të marra nga Enterprise (për kontrollin request_ingest)
        db.add(
            SenderRequestOperation(
                operation_id=uuid.uuid4(),
                enterprise_id=e1.id,
                registry_id=uuid.UUID(ids["pending_acme"]),
                external_ref="e2e:pending_acme",
                operation="request",
                request_hash="0" * 64,
                outcome="created",
                auto="not_applicable",
                status_after="pending",
            )
        )
        db.commit()
    with open(out, "w") as fh:
        json.dump(ids, fh)


if __name__ == "__main__":
    main(sys.argv[1])
