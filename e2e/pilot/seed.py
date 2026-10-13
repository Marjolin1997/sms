"""Seed i stack-ut M11-H (dy tenant-ë + Central) në PostgreSQL të përkohshëm. VETËM zhvillim; refuzon çdo DB që s'është `h_*`.

    PILOT_DIR=... python -m e2e.pilot.seed      (SMS_* dhe CENTRAL_* të vendosura nga stack.sh)

Krijon: tenant-ët `tenant-a` / `tenant-b` (wallet, tarifë, rrugë `fake`, kontakte me pëlqim, lista, plan + faturë), çelësat e rolave
Enterprise, enterprise-t në Central me të njëjtat UUID, admin + operator Central dhe klientin e shërbimit të raportuesit (Ed25519).
NUK krijon sender-a (rrjedha kalon nga kërkesa → Central → projeksioni) dhe NUK konfiguron asnjë provider të vërtetë.
"""

import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.context import for_owner
from app.core.db import SessionLocal
from app.models.sending import AccountPlan, Route
from app.models.wallet import TopupMethod
from app.services import apikeys, billing, consent, rates
from app.services import contacts as contacts_svc
from app.services import wallet as wallets
from apps.central.core.config import settings as csettings
from apps.central.core.db import make_engine
from apps.central.models.enterprise import Enterprise
from apps.central.models.user import CentralUser
from apps.central.services import service_auth
from apps.central.tools import create_admin

PAST = datetime(2020, 1, 1, tzinfo=UTC)
TENANTS = {"a": ("tenant-a", "Tenant A Sh.p.k.", 30), "b": ("tenant-b", "Tenant B Sh.p.k.", 12)}


def main(out: str) -> None:
    for url in (settings.database_url, csettings.database_url):
        if "/h_" not in url:
            raise SystemExit("refusing: only the temporary h_* databases")
    ids: dict = {"tenants": {}}
    with SessionLocal() as db:
        db.add(Route(prefix="355", country="AL", provider="fake"))
        plan = billing.create_plan(db, "gold", "Gold", "EUR", "10", 5, "0.1")
        for tag, (owner, legal, n_contacts) in TENANTS.items():
            ctx = for_owner(db, owner, create=True)
            w = wallets.create_wallet(db, owner, "EUR")
            wallets.confirm_topup(db, wallets.create_topup(db, w.id, "100", TopupMethod.CASH).id)
            card = rates.create_card(db, f"std-{owner}", "EUR")
            v = rates.new_draft(db, card.id)
            rates.set_rate(db, v.id, "355", "0.05")
            rates.publish(db, v.id, PAST, now=PAST - timedelta(days=1))
            db.add(AccountPlan(owner_ref=owner, rate_card_id=card.id))
            billing.set_profile(
                db,
                owner,
                legal,
                "Rruga e Durrësit 1, Tiranë",
                "AL",
                f"{tag}@example.test",
                "K1",
                "0.2",
            )
            sub = billing.assign_plan(
                db, owner, plan.id, auto_pay=False, now=datetime.now(UTC) - timedelta(days=70)
            )
            inv = billing.generate_invoice(db, sub.id, datetime.now(UTC))
            lst = contacts_svc.create_list(db, owner, f"{tag.upper()} customers")
            cids = []
            for i in range(n_contacts):
                c, _ = contacts_svc.upsert(
                    db,
                    owner,
                    phone=f"35569{tag == 'a' and 1 or 2}23{i + 1:04d}",
                    first_name=f"{tag.upper()}Person{i + 1:02d}",
                    last_name="Test",
                )
                consent.record(db, owner, "sms", c.phone, "opt_in", "x", "form", "u", "evidence")
                cids.append(c.id)
            contacts_svc.add_members(db, owner, lst.id, cids)
            db.commit()
            ids["tenants"][tag] = {
                "owner_ref": owner, "enterprise_id": str(ctx.enterprise_id), "wallet": w.id, "list": lst.id,
                "invoice": inv.id, "invoice_number": inv.number, "contacts": cids,
                "key": apikeys.create_key(db, f"pilot {tag}", "client", owner, "pilot")[1],
                "phone_prefix": f"35569{tag == 'a' and 1 or 2}23",
            }  # fmt: skip
        ids["keys"] = {
            role: apikeys.create_key(db, f"pilot {role}", role, None, "pilot")[1]
            for role in ("support", "approver", "finance", "superadmin", "pricing")
        }
        db.commit()
    engine = make_engine()
    for email, role, env in (
        ("admin@pilot.test", "admin", "PILOT_ADMIN_PASSWORD"),
        ("operator@pilot.test", "operator", "PILOT_OPERATOR_PASSWORD"),
    ):
        code, msg = create_admin.run(email, os.environ[env], role, engine)
        assert code == 0, msg
    with Session(engine) as cdb:
        for tag, t in ids["tenants"].items():
            cdb.add(Enterprise(id=uuid.UUID(t["enterprise_id"]), name=TENANTS[tag][1]))
        cdb.flush()
        priv = Ed25519PrivateKey.generate()
        pem = priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        pub = (
            priv.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )
        eids = [uuid.UUID(t["enterprise_id"]) for t in ids["tenants"].values()]
        service_auth.create_client(cdb, "ent-h", ["sender:report", "sender:read"], eids)
        service_auth.add_key(cdb, "ent-h", "k1", pub)
        cdb.commit()
        ids["admin_id"] = str(
            cdb.scalar(select(CentralUser.id).where(CentralUser.email == "admin@pilot.test"))
        )
    kp = os.environ["PILOT_KEY_OUT"]
    with open(kp, "wb") as fh:
        fh.write(pem)
    os.chmod(kp, 0o600)
    with open(out, "w") as fh:
        json.dump(ids, fh)


if __name__ == "__main__":
    main(sys.argv[1])
