"""Të dhëna Enterprise për E2E të faturimit/wallet-it të klientit (M11-E2). VETËM DB e përkohshme (SMS_DATABASE_URL).

    SMS_DATABASE_URL=sqlite:///... SMS_PII_HMAC_KEY=... SMS_SECRETS_KEY=... python -m e2e.enterprise_billing_seed <out.json>

Krijon dy tenant-e (e2e-a, e2e-b): wallet me bilanc, plan + profil + abonim + një faturë secili; te e2e-a edhe një grant me status
`reconciliation_required` (pending recalculation) për wallet-in e dytë (USD) — wallet-i EUR mbetet normal. Shkruan çelësat vetëm te <out.json>.
"""

import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.core.db import SessionLocal
from app.models.money_authority import G_RECON, MoneyGrant
from app.models.wallet import TopupMethod
from app.services import apikeys, billing
from app.services import wallet as wallets


def main(out: str) -> None:
    ids: dict = {}
    with SessionLocal() as db:
        plan = billing.create_plan(db, "gold", "Gold", "EUR", "1000", 5, "0.1")
        for tag, name in (("a", "Müller & Söhne – Tiranë Sh.p.k."), ("b", "Beta Sh.p.k.")):
            owner = f"e2e-{tag}"
            w = wallets.create_wallet(db, owner, "EUR")
            wallets.confirm_topup(db, wallets.create_topup(db, w.id, "1234.5", TopupMethod.CASH).id)
            billing.set_profile(
                db,
                owner,
                name,
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
            assert inv is not None
            db.commit()
            ids[f"invoice_{tag}"] = inv.id
            ids[f"invoice_{tag}_number"] = inv.number
            ids[f"key_{tag}"] = apikeys.create_key(db, f"E2E {tag}", "client", owner, "e2e")[1]
            ids[f"wallet_{tag}"] = w.id
        w2 = wallets.create_wallet(db, "e2e-a", "USD")
        wallets.confirm_topup(db, wallets.create_topup(db, w2.id, "50", TopupMethod.CASH).id)
        db.add(
            MoneyGrant(
                grant_id=uuid.uuid4(),
                enterprise_id=uuid.uuid4(),
                account_id=uuid.uuid4(),
                product_id=uuid.uuid4(),
                currency="USD",
                amount=Decimal("5"),
                purpose="standard",
                wallet_id=w2.id,
                status=G_RECON,
                issued_seq=1,
                issued_event_id=uuid.uuid4(),
                issued_payload_hash="0" * 64,
                detail="insufficient available funds for reversal",
            )
        )
        db.commit()
        ids["wallet_usd"] = w2.id
    with open(out, "w") as fh:
        json.dump(ids, fh)


if __name__ == "__main__":
    main(sys.argv[1])
