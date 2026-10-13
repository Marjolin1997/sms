"""Shtesa të të dhënave për E2E të raporteve (M11-G). Ekzekutohet PAS `e2e.messaging_seed`. VETËM DB e përkohshme.

    SMS_DATABASE_URL=sqlite:///... SMS_PII_HMAC_KEY=... SMS_SECRETS_KEY=... python -m e2e.reports_seed <ids.json>

Shton: një campaign me emër formule (`=cmd|…`) për kontrollin e CSV, një kod gabimi të panjohur te një mesazh i dështuar,
dhe rreshta audit për dy tenant-ë (për kontrollin e skopimit). NUK dërgon SMS: asnjë provider i vërtetë, asnjë punonjës.
"""

import json
import sys

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.admin import AuditLog
from app.models.sending import Message, MessageStatus
from app.services import campaigns as svc

FORMULA = "=cmd|' /C calc'!A0"


def main(path: str) -> None:
    ids = json.load(open(path))
    with SessionLocal() as db:
        c = svc.create(
            db, "e2e-m", FORMULA, ids["list_vip"], sender="ACME", created_by="e2e", text="x"
        )
        ids["campaign_formula"] = c.id
        failed = db.scalars(
            select(Message).where(
                Message.owner_ref == "e2e-m", Message.status == MessageStatus.FAILED
            )
        ).first()
        if failed is not None:
            failed.error_code = "weird_x9"  # kod i panjohur: duhet të mbetet i dukshëm si "Failed"
        rows = [
            ("client:e2e", "client", "campaign.create", "campaign", str(ids["campaign_done"]), "e2e-m", {"list": 1}),
            ("client:e2e", "client", "campaign.pause", "campaign", str(ids["campaign_done"]), "e2e-m", {"at": "2026-10-01", "url": "https://secret.example", "token": "SECRET-TOKEN"}),
            ("staff-person@example.com", "finance", "wallet.adjust", "wallet", "1", "e2e-m", {"note": "x"}),
            ("staff-person@example.com", "support", "cross_tenant.read", "wallet", "1", "e2e-m", None),
            ("client:other", "client", "campaign.cancel", "campaign", str(ids["other_campaign"]), "e2e-x", None),
            ("client:old", "client", "legacy.row", "campaign", "1", None, None),
        ]  # fmt: skip
        for actor, role, action, ttype, tid, owner, detail in rows:
            db.add(
                AuditLog(
                    actor=actor,
                    role=role,
                    action=action,
                    target_type=ttype,
                    target_id=tid,
                    owner_ref=owner,
                    detail=json.dumps(detail) if detail else None,
                )  # fmt: skip
            )
        db.commit()
    json.dump(ids, open(path, "w"))


if __name__ == "__main__":
    main(sys.argv[1])
