"""Të dhëna për E2E të mesazheve/campaign-eve të klientit (M11-F). VETËM DB e përkohshme (SMS_DATABASE_URL).

    SMS_DATABASE_URL=sqlite:///... SMS_PII_HMAC_KEY=... SMS_SECRETS_KEY=... python -m e2e.messaging_seed <out.json>

NUK dërgon SMS të vërtetë: provider-i është `FakeProvider` brenda këtij procesi (pa rrjet); API-ja E2E nuk nis punonjës dërgimi.
Krijon tenant-in `e2e-m` me wallet, tarifë, rrugë `fake`, sender ACME të miratuar, 30 kontakte me pëlqim, lista VIP/Empty,
një campaign të përfunduar me mesazhe të dorëzuara/dështuara/UNKNOWN, një të planifikuar, 27 drafte (faqosje) dhe një tenant tjetër (`e2e-x`).
"""

import json
import sys
from datetime import UTC, datetime, timedelta

import app.providers as providers
from app.core.db import SessionLocal
from app.models.sending import AccountPlan, Message, MessageStatus, Route
from app.providers import FakeProvider
from app.services import apikeys, consent, rates, sender_ids
from app.services import campaigns as svc
from app.services import contacts as contacts_svc
from app.services import messages as msg
from app.services import wallet as wallets
from app.services.wallet import TopupMethod

PAST = datetime(2020, 1, 1, tzinfo=UTC)


def tenant(db, owner: str, sender: str = "ACME") -> None:
    w = wallets.create_wallet(db, owner, "EUR")
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "100", TopupMethod.CASH).id)
    card = rates.create_card(db, f"std-{owner}", "EUR")
    v = rates.new_draft(db, card.id)
    rates.set_rate(db, v.id, "355", "0.05")
    rates.publish(db, v.id, PAST, now=PAST - timedelta(days=1))
    db.add(AccountPlan(owner_ref=owner, rate_card_id=card.id))
    s = sender_ids.request(db, owner, "AL", sender)
    sender_ids.approve(db, s.id, "admin")
    db.commit()


def main(out: str) -> None:
    providers._registry["fake"] = fake = FakeProvider()
    ids: dict = {}
    with SessionLocal() as db:
        db.add(Route(prefix="355", country="AL", provider="fake"))
        tenant(db, "e2e-m")
        tenant(db, "e2e-x", "OTHERX")
        # kontakte me pëlqim (numrat 0001/0002 shkaktojnë gabim të përkohshëm/të përhershëm të FakeProvider)
        lst = contacts_svc.create_list(db, "e2e-m", "VIP")
        contacts_svc.create_list(db, "e2e-m", "Empty")
        cids = []
        for i in range(30):
            phone = f"35569123{i + 1:04d}"
            c, _ = contacts_svc.upsert(
                db, "e2e-m", phone=phone, first_name=f"Person{i + 1:02d}", last_name="Test"
            )
            consent.record(db, "e2e-m", "sms", c.phone, "opt_in", "x", "form", "u", "evidence")
            cids.append(c.id)
        contacts_svc.add_members(db, "e2e-m", lst.id, cids[:28])
        # tenant tjetër me kontaktin dhe campaign-in e vet
        other, _ = contacts_svc.upsert(db, "e2e-x", phone="355691239999", first_name="Foreign")
        ol = contacts_svc.create_list(db, "e2e-x", "OtherList")
        contacts_svc.add_members(db, "e2e-x", ol.id, [other.id])
        db.commit()
        xc = svc.create(
            db,
            "e2e-x",
            "Other tenant campaign",
            ol.id,
            sender="OTHERX",
            created_by="e2e",
            text="hi",
        )
        db.commit()
        ids["other_campaign"] = xc.id
        ids["other_contact"] = other.id
        # campaign i përfunduar me gjendje të përziera
        done = svc.create(
            db,
            "e2e-m",
            "Autumn sale",
            lst.id,
            sender="ACME",
            created_by="e2e",
            text="Big sale for you!",
        )
        db.commit()
        now = datetime.now(UTC)
        svc.schedule(db, "e2e-m", done.id, None, now=now)
        db.commit()
        for _ in range(8):
            svc.run_due(db, now)
        for _ in range(60):
            if msg.process_one(db) is None:
                break
        ids["campaign_done"] = done.id
        sent = (
            db.query(Message)
            .filter(Message.owner_ref == "e2e-m", Message.provider_message_id.is_not(None))
            .order_by(Message.id)
            .all()
        )
        for i, m in enumerate(sent):
            if i == 3:
                msg.apply_dlr(
                    db, "fake", m.provider_message_id, delivered=False, code="twilio_30003"
                )
            elif i == 4:
                m.status = (
                    MessageStatus.UNKNOWN
                )  # kanali i panjohur: klienti duhet ta shohë "sending", kurrë "failed"
            else:
                msg.apply_dlr(db, "fake", m.provider_message_id, delivered=True)
        db.commit()
        # campaign i planifikuar (e ardhmja) dhe 27 drafte për faqosje
        sch = svc.create(
            db, "e2e-m", "Scheduled promo", lst.id, sender="ACME", created_by="e2e", text="Later!"
        )
        db.commit()
        svc.schedule(db, "e2e-m", sch.id, now + timedelta(days=3), now=now)
        ids["campaign_scheduled"] = sch.id
        draft = svc.create(
            db, "e2e-m", "Draft promo", lst.id, sender="ACME", created_by="e2e", text="Draft text"
        )
        ids["campaign_draft"] = draft.id
        for i in range(26):
            svc.create(
                db, "e2e-m", f"Bulk {i:02d}", lst.id, sender="ACME", created_by="e2e", text="bulk"
            )
        db.commit()
        ids["list_vip"] = lst.id
        ids["key_m"] = apikeys.create_key(db, "E2E messaging", "client", "e2e-m", "e2e")[1]
        ids["key_x"] = apikeys.create_key(db, "E2E other", "client", "e2e-x", "e2e")[1]
        db.commit()
        ids["provider_calls"] = len(fake.calls)
    with open(out, "w") as fh:
        json.dump(ids, fh)


if __name__ == "__main__":
    main(sys.argv[1])
