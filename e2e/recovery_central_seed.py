"""Të dhëna përfaqësuese Central për drill-in e restore-it (M11-I/F-05A). VETËM DB e përkohshme, kurrë prodhim.

    CENTRAL_DATABASE_URL=postgresql+psycopg://… python -m e2e.recovery_central_seed

Krijon: enterprise, produkt, 2 admin (maker/checker), llogari krediti, pagesë të miratuar dhe një grant (⇒ hyrje ledger me para).
Fjalëkalimet janë sintetike dhe vetëm për këtë drill. Asgjë nuk printohet përveç numërimit të krijuar (pa sekrete)."""

import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.central.services import credit_accounts as accts
from apps.central.services import enterprises, grants, payments, users
from apps.central.services import products as prod_svc

PASSWORD = "drill only battery staple"


def seed(url: str) -> None:
    eng = create_engine(url)
    with sessionmaker(bind=eng, expire_on_commit=False)() as s:
        ent = enterprises.create(s, "Drill Enterprise").id
        sms = prod_svc.create(s, "sms", "SMS", "sms").id
        a1 = users.create_user(s, "drill-a1@example.com", PASSWORD, "admin")
        a2 = users.create_user(s, "drill-a2@example.com", PASSWORD, "admin")
        s.commit()
        acct = accts.create(s, ent, sms, "EUR", a1)
        s.commit()
        p = payments.create(
            s, acct.id, "250.00", system="system:payment_import", external_reference="drill-ref-1"
        )
        payments.approve(s, p.id, a2)
        s.commit()
        grants.issue(s, acct.id, "75.00", idempotency_key="drill-grant-0001", actor=a1)
        s.commit()
    eng.dispose()


def main() -> int:
    url = os.environ.get("CENTRAL_DATABASE_URL")
    if not url:
        print("ERROR: CENTRAL_DATABASE_URL is not set", file=sys.stderr)
        return 2
    seed(url)
    print(
        "seeded: 1 enterprise, 1 product, 2 admins, 1 credit account, 1 approved payment, 1 grant"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
