"""Të dhëna demo për zhvillim/pamje ekrani. NUK për prodhim.

    SMS_DATABASE_URL=... SMS_PII_HMAC_KEY=... SMS_SECRETS_KEY=... python -m scripts.seed_demo

Krijon llogarinë "acme" (wallet, tarifa, sender, routes, kontakte, campaigns, email, webhook)
dhe printon dy çelësa API: një `client` (acme) dhe një `superadmin`.
"""

from datetime import UTC, datetime, timedelta

import httpx

from app.core.db import SessionLocal
from app.core.security import Principal
from app.models.sending import AccountPlan, Route
from app.providers import FakeEmailProvider  # noqa: F401
from app.services import (
    apikeys,
    billing,
    campaigns,
    consent,
    email_domains,
    emails,
    inbox,
    net_guard,
    payments,
    rates,
    sender_ids,
    switches,
    templates,
    webhooks,
)
from app.services import contacts as contacts_svc
from app.services import dns_check as dns
from app.services import messages as msg
from app.services import wallet as wallets
from app.services.audit import audit

OWNER = "acme"
PAST = datetime(2020, 1, 1, tzinfo=UTC)

PEOPLE = [
    ("Ana", "Hoxha", "+355691110001"),
    ("Besa", "Krasniqi", "+38344210002"),
    ("Dritan", "Leka", "+355691110003"),
    ("Elira", "Shehu", "+38344210004"),
    ("Fatos", "Berisha", "+355691110005"),
    ("Gent", "Marku", "+355691110006"),
    ("Hana", "Gashi", "+38344210007"),
    ("Ilir", "Dervishi", "+355691110008"),
    ("Jeta", "Rama", "+355691110009"),
    ("Klea", "Vata", "+4915112345610"),
    ("Luan", "Osmani", "+38344210011"),
    ("Mira", "Prifti", "+355691110012"),
]


class _Dns:
    def __init__(self):
        self.r: dict[str, list[str]] = {}

    def txt(self, name):
        return self.r.get(name, [])


def main() -> None:
    fake_dns = _Dns()
    dns.set_resolver(fake_dns)
    net_guard.set_resolver(lambda host: ["93.184.216.34"])
    ok = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    webhooks.set_client(ok)
    boss = Principal("demo-seed", "superadmin")

    with SessionLocal() as db:
        # --- para dhe tarifa
        w = wallets.create_wallet(db, OWNER, "EUR")
        wallets.confirm_topup(
            db,
            wallets.create_topup(
                db, w.id, "250", wallets.TopupMethod.ELECTRONIC, external_ref="demo-topup-1"
            ).id,
        )
        card = rates.create_card(db, "acme-standard", "EUR")
        v = rates.new_draft(db, card.id)
        for prefix, price in (("355", "0.045"), ("383", "0.052"), ("49", "0.071")):
            rates.set_rate(db, v.id, prefix, price)
        rates.publish(db, v.id, PAST, now=PAST - timedelta(days=1))
        db.add(AccountPlan(owner_ref=OWNER, rate_card_id=card.id))
        for prefix, cc in (("355", "AL"), ("383", "XK"), ("49", "DE")):
            db.add(Route(prefix=prefix, country=cc, provider="fake"))
            s = sender_ids.request(db, OWNER, cc, "ACME")
            sender_ids.approve(db, s.id, "demo-approver")
        audit(db, boss, "ratecard.publish", "ratecard_version", v.id, {"seed": True})
        db.commit()

        # --- webhook (para trafikut, që eventet e mëpasshme dërgohen)
        webhooks.create_endpoint(
            db,
            OWNER,
            "https://hooks.acme-demo.example/sms",
            ["message.*", "campaign.*", "email.bounced"],
            "Production listener",
        )
        db.commit()

        # --- kontakte, lista, consent
        lst = contacts_svc.create_list(db, OWNER, "Buletini")
        vip = contacts_svc.create_list(db, OWNER, "VIP customers")
        ids = []
        for i, (first, last, phone) in enumerate(PEOPLE):
            c, _ = contacts_svc.upsert(
                db,
                OWNER,
                phone=phone,
                email=f"{first.lower()}.{last.lower()}@customer.example",
                first_name=first,
                last_name=last,
                attributes={"plan": "pro" if i % 3 == 0 else "free"},
            )
            ids.append(c.id)
            if i not in (4, 9):  # dy pa opt-in
                consent.record(
                    db,
                    OWNER,
                    "sms",
                    c.phone,
                    "opt_in",
                    "opt_in",
                    "web_form",
                    "seed",
                    "Signup form v3, 2026-08-14",
                )
                consent.record(
                    db,
                    OWNER,
                    "email",
                    c.email,
                    "opt_in",
                    "opt_in",
                    "web_form",
                    "seed",
                    "Signup form v3, 2026-08-14",
                )
        contacts_svc.add_members(db, OWNER, lst.id, ids)
        contacts_svc.add_members(db, OWNER, vip.id, ids[::3])
        consent.apply_inbound_keyword(db, OWNER, PEOPLE[7][2], "STOP")  # një STOP
        db.commit()

        # --- mesazhe SMS transaksionale (një pjesë e dorëzuar, një e dështuar)
        for i, (_, _, phone) in enumerate(PEOPLE):
            try:
                msg.submit(
                    db, OWNER, f"tx-{i}", phone, "ACME", text=f"Kodi juaj është {481200 + i}"
                )
            except consent.RecipientSuppressed:
                pass  # kontakti që dërgoi STOP
        db.commit()
        later = datetime.now(UTC) + timedelta(seconds=5)
        sent = []
        for _ in range(len(PEOPLE) - 2):
            m = msg.process_one(db, later)
            if m and m.status.value == "sent":  # 0001 → riprovim, 0002 → refuzim (provider fals)
                sent.append(m)
        for i, m in enumerate(sent):
            if i % 5 == 4:
                msg.apply_dlr(db, "fake", m.provider_message_id, False, "absent_subscriber")
            elif i % 5 != 3:
                msg.apply_dlr(db, "fake", m.provider_message_id, True)
        db.commit()

        # --- campaign SMS e përfunduar dhe një në draft
        c1 = campaigns.create(
            db,
            OWNER,
            "Ulje vjeshte",
            lst.id,
            "ACME",
            "demo",
            text="Përshëndetje {{first_name}}, 20% ulje këtë javë. Shkruani STOP për çregjistrim.",
            max_cost="25",
        )
        db.commit()
        campaigns.schedule(db, OWNER, c1.id, None)
        db.commit()
        for _ in range(8):
            campaigns.run_due(db, datetime.now(UTC))
        for _ in range(30):
            m = msg.process_one(db, later + timedelta(seconds=30))
            if m and m.status.value == "sent":
                msg.apply_dlr(db, "fake", m.provider_message_id, True)
        db.commit()
        campaigns.create(
            db,
            OWNER,
            "Paralajmërim Black Friday",
            vip.id,
            "ACME",
            "demo",
            text="Po vjen diçka e madhe, {{first_name}}...",
        )
        db.commit()

        # --- email: domen i verifikuar, disa email, bounce
        d = email_domains.create(db, OWNER, "acme-demo.example")
        for rec in email_domains.dns_records(d):
            fake_dns.r.setdefault(rec["name"], []).append(rec["value"])
        email_domains.verify(db, OWNER, d.id)
        d2 = email_domains.create(
            db, OWNER, "news.acme-demo.example"
        )  # i pa verifikuar (tregon rekordet DNS)
        db.commit()
        for i in range(4):
            emails.submit(
                db,
                OWNER,
                f"em-{i}",
                "hello@acme-demo.example",
                f"{PEOPLE[i][0].lower()}.{PEOPLE[i][1].lower()}@customer.example",
                "Your order has shipped",
                "Good news! Your order is on the way.",
            )
        db.commit()
        sent_e = [emails.process_one(db, later) for _ in range(4)]
        emails.apply_event(db, "fake", sent_e[0].provider_message_id, "delivered")
        emails.apply_event(db, "fake", sent_e[1].provider_message_id, "delivered")
        emails.apply_event(
            db,
            "fake",
            sent_e[2].provider_message_id,
            "bounce_hard",
            "550 5.1.1 mailbox unavailable",
        )
        db.commit()
        del d2

        for _ in range(80):
            if not webhooks.deliver_next(db, datetime.now(UTC) + timedelta(minutes=1)):
                break
        for kind, tid in (("apikey.create", 1), ("sender.approve", 1), ("wallet.adjust", w.id)):
            audit(db, boss, kind, "demo", tid, {"seed": True})
        switches.set_switch(db, switches.SUBMIT, True, "demo-seed", None)
        db.commit()

        # --- SMS hyrës: numër me sender numerik të miratuar, fjalë kyçe dhe disa mesazhe
        num = sender_ids.request(db, OWNER, "AL", "+355690000001")
        sender_ids.approve(db, num.id, "demo-approver")
        inbox.set_keyword(
            db, OWNER, "help", "Na telefononi në 0800 123 ose shkruani në ndihme@acme.example"
        )
        db.commit()
        for i, (frm, text) in enumerate(
            [
                ("+355691110001", "Përshëndetje, kur mbërrin porosia ime?"),
                ("+38344210002", "HELP"),
                ("+355691110003", "Faleminderit!"),
            ]
        ):
            inbox.receive(db, "fake", f"seed-mo-{i}", "+355690000001", frm, text)
            db.commit()

        # --- faturim: plan, profil me TVSH, abonim 75 ditë më parë → dy fatura (njëra e paguar)
        plan = billing.create_plan(db, "growth", "Growth", "EUR", "29.00", 5000, "0.001")
        billing.set_profile(db, OWNER, "Acme Sh.p.k.", "Rr. Myslym Shyri 12, Tirane", "AL",
                            "billing@acme-demo.example", "L12345678A", vat_rate="0.2")  # fmt: skip
        billing.assign_plan(
            db, OWNER, plan.id, auto_pay=False, now=datetime.now(UTC) - timedelta(days=75)
        )
        db.commit()
        billing.run_billing(db, datetime.now(UTC))
        db.commit()
        first = db.query(billing.Invoice).order_by(billing.Invoice.id).first()
        billing.pay_from_wallet(db, OWNER, first.id)
        pay_ok = payments.start_payment(db, OWNER, "topup", "100", wallet_id=w.id)
        payments.complete(db, "fake", pay_ok.external_id, "succeeded", "100", "EUR")
        payments.start_payment(db, OWNER, "topup", "50", wallet_id=w.id)  # një në pritje
        db.commit()

        # --- llogari e dytë me punë në pritje për stafin (miratime + top-up)
        g = wallets.create_wallet(db, "globex", "EUR")
        wallets.create_topup(db, g.id, "75", wallets.TopupMethod.CASH, created_by="seed-cashier")
        db.add(AccountPlan(owner_ref="globex", rate_card_id=card.id))
        sender_ids.request(db, "globex", "AL", "GLOBEX")
        tv = templates.create(
            db,
            "globex",
            "Porosia u nis",
            "Përshëndetje {{first_name}}, porosia juaj {{order_id}} është nisur.",
        )
        del tv
        tv2 = templates.create(
            db, OWNER, "Kodi i hyrjes", "Kodi juaj ACME është {{code}}. Skadon pas 10 minutash."
        )
        templates.review(db, tv2.id, "approve", "demo-approver")
        templates.new_version(db, tv2.template_id, "Kodi juaj ACME: {{code}} (i vlefshëm 10 min)")
        db.commit()

        client_key = apikeys.create_key(db, "Acme console", "client", OWNER, "demo-seed")[1]
        admin_key = apikeys.create_key(db, "Demo superadmin", "superadmin", None, "demo-seed")[1]
        db.commit()
    print(f"CLIENT_KEY={client_key}")
    print(f"ADMIN_KEY={admin_key}")


if __name__ == "__main__":
    main()
