import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from app.core.config import settings
from app.models.billing import (
    Invoice,
    InvoiceImmutableError,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentStatus,
    Subscription,
)
from app.models.email import Email, EmailStatus
from app.models.events import Event
from app.models.wallet import EntryType, LedgerEntry, TopupMethod
from app.providers import payments as gw
from app.services import billing, email_domains, payments
from app.services import wallet as wallets
from app.services.wallet import Conflict, NotFound
from tests.test_pipeline import world  # noqa: F401

OWNER = "c1"
T0 = datetime(2030, 1, 15, 9, tzinfo=UTC)
AFTER = datetime(2030, 2, 16, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fresh_gateway():
    gw._gateways["fake"] = gw.FakePaymentGateway()
    return gw._gateways["fake"]


@pytest.fixture
def plan(db):
    p = billing.create_plan(db, "starter", "Starter", "eur", "20.00", 100, "0.002")
    db.commit()
    return p


def profile(db, owner=OWNER, vat=None, name="Acme Sh.p.k."):
    p = billing.set_profile(db, owner, name, "Rr. Test 1, Tirane", "al", "billing@acme.example",
                            "K12345678A", vat)  # fmt: skip
    db.commit()
    return p


def subscribe(db, plan, owner=OWNER, auto_pay=False, now=T0):
    profile(db, owner) if not billing.get_profile(db, owner) else None
    s = billing.assign_plan(db, owner, plan.id, auto_pay=auto_pay, now=now)
    db.commit()
    return s


def add_emails(db, n, when, status=EmailStatus.DELIVERED, owner=OWNER):
    dom = db.query(email_domains.EmailDomain).filter_by(owner_ref=owner).first()
    if dom is None:
        dom = email_domains.create(db, owner, "acme.example")
    for i in range(n):
        db.add(Email(public_id=f"{when.timestamp()}-{status.value}-{i}-{db.query(Email).count()}",
                     owner_ref=owner, idempotency_key=f"k{db.query(Email).count()}-{i}",
                     request_hash="h", domain_id=dom.id, from_email="a@acme.example",
                     to_email="b@x.org", subject="s", text_body="t", provider="fake",
                     status=status, created_at=when))  # fmt: skip
        db.flush()
    db.commit()


# --- Kohë ----------------------------------------------------------------------------


def test_add_months_clamps_without_drift():
    jan31 = datetime(2030, 1, 31, tzinfo=UTC)
    assert billing.add_months(jan31, 1).day == 28
    assert billing.add_months(jan31, 2).day == 31  # s'rrëshqet nga 28
    assert billing.add_months(datetime(2028, 1, 31, tzinfo=UTC), 1).day == 29  # vit i brishtë
    assert billing.add_months(datetime(2030, 12, 15, tzinfo=UTC), 1) == datetime(
        2031, 1, 15, tzinfo=UTC
    )


# --- Plane, profil, abonim -------------------------------------------------------------


def test_plan_rules(db):
    with pytest.raises(billing.InvalidBilling):
        billing.create_plan(db, "Bad Code!", "x", "EUR", "1")
    with pytest.raises(wallets.InvalidAmount):
        billing.create_plan(db, "p1", "x", "EUR", 1.5)  # float refuzohet
    with pytest.raises(wallets.InvalidAmount):
        billing.create_plan(db, "p2", "x", "EUR", "-1")
    p = billing.create_plan(db, "p3", "x", "EUR", "5")
    with pytest.raises(Conflict):
        billing.create_plan(db, "p3", "y", "EUR", "5")
    billing.retire_plan(db, p.id)
    profile(db)
    with pytest.raises(billing.InvalidBilling):
        billing.assign_plan(db, OWNER, p.id)  # i tërhequr


def test_subscribe_needs_profile_and_validates_profile(db, plan):
    with pytest.raises(billing.InvalidBilling):
        billing.assign_plan(db, OWNER, plan.id)
    for bad in ({"email": "nope"}, {"country": "ALB"}):
        args = {
            "legal_name": "A B",
            "address": "somewhere",
            "country": "AL",
            "email": "a@b.co",
        } | bad
        with pytest.raises(billing.InvalidBilling):
            billing.set_profile(db, OWNER, **args)
    with pytest.raises(billing.InvalidBilling):
        billing.set_profile(db, OWNER, "A B", "somewhere", "AL", "a@b.co", vat_rate="1.5")


# --- Lëshimi -----------------------------------------------------------------------------


def test_no_invoice_before_period_end(db, plan):
    sub = subscribe(db, plan)
    assert billing.generate_invoice(db, sub.id, datetime(2030, 2, 14, tzinfo=UTC)) is None
    assert sub.periods_billed == 0


def test_invoice_lines_tax_rounding_and_numbering(db, world):  # noqa: F811
    p = billing.create_plan(db, "odd", "Odd", "EUR", "19.99", 10, "0.125")
    profile(db, vat="0.2")
    sub = billing.assign_plan(db, OWNER, p.id, auto_pay=False, now=T0)
    db.commit()
    add_emails(db, 15, datetime(2030, 1, 20, tzinfo=UTC))  # 5 mbi kuotën
    add_emails(db, 3, datetime(2030, 1, 21, tzinfo=UTC), EmailStatus.QUEUED)  # s'faturohen
    add_emails(db, 2, datetime(2030, 2, 20, tzinfo=UTC))  # periudhë tjetër
    inv = billing.generate_invoice(db, sub.id, AFTER)
    db.commit()
    lines = billing.invoice_lines(db, inv.id)
    assert [(ln.quantity, ln.amount) for ln in lines] == [(D("1"), D("19.99")), (D("5"), D("0.63"))]
    # 5 × 0.125 = 0.625 → 0.63 (HALF_UP); subtotal 20.62; TVSH 20% = 4.124 → 4.12
    assert (inv.subtotal, inv.tax, inv.total) == (D("20.62"), D("4.12"), D("24.74"))
    assert inv.number == "INV-2030-000001" and inv.status == InvoiceStatus.OPEN
    assert inv.total == inv.subtotal + inv.tax
    assert inv.due_at - inv.issued_at == timedelta(days=settings.invoice_due_days)


def test_one_invoice_per_period_and_catch_up(db, plan, world):  # noqa: F811
    sub = subscribe(db, plan)
    assert billing.generate_invoice(db, sub.id, AFTER) is not None
    db.commit()
    assert billing.generate_invoice(db, sub.id, AFTER) is None  # periudha tjetër s'ka mbaruar
    n = billing.run_billing(db, datetime(2030, 5, 20, tzinfo=UTC))  # mars, prill, maj (3 të tjera)
    assert n == 3
    nums = [i.number for i in db.query(Invoice).order_by(Invoice.id)]
    assert nums == [f"INV-2030-00000{k}" for k in (1, 2, 3, 4)]
    assert billing.run_billing(db, datetime(2030, 5, 20, tzinfo=UTC)) == 0
    starts = [i.period_start.replace(tzinfo=UTC) for i in db.query(Invoice).order_by(Invoice.id)]
    assert starts == [datetime(2030, m, 15, 9, tzinfo=UTC) for m in (1, 2, 3, 4)]


def test_free_plan_advances_without_invoice(db, world):  # noqa: F811
    free = billing.create_plan(db, "free", "Free", "EUR", "0", 1000, "0.001")
    sub = subscribe(db, free)
    assert billing.generate_invoice(db, sub.id, AFTER) is None
    assert sub.periods_billed == 1 and db.query(Invoice).count() == 0
    add_emails(db, 1100, datetime(2030, 2, 20, tzinfo=UTC))  # 100 mbi kuotën → 0.10
    inv = billing.generate_invoice(db, sub.id, datetime(2030, 3, 16, tzinfo=UTC))
    assert inv.total == D("0.10")


def test_bill_to_snapshot_is_frozen_and_invoice_immutable(db, plan):
    sub = subscribe(db, plan)
    inv = billing.generate_invoice(db, sub.id, AFTER)
    db.commit()
    profile(db, name="Renamed Ltd")
    assert json.loads(inv.bill_to)["legal_name"] == "Acme Sh.p.k."
    inv.total = D("1")
    with pytest.raises(InvoiceImmutableError):
        db.flush()
    db.rollback()
    inv = db.query(Invoice).one()
    db.delete(inv)
    with pytest.raises(InvoiceImmutableError):
        db.flush()
    db.rollback()
    line = db.query(InvoiceLine).first()
    line.amount = D("0.01")
    with pytest.raises(InvoiceImmutableError):
        db.flush()
    db.rollback()


# --- Pagesa nga wallet ----------------------------------------------------------------------


def test_autopay_from_wallet_and_insufficient_stays_open(db, plan, world):  # noqa: F811
    w, _ = world  # 10 EUR, fatura 20
    sub = subscribe(db, plan, auto_pay=True)
    inv = billing.generate_invoice(db, sub.id, AFTER)
    db.commit()
    assert inv.status == InvoiceStatus.OPEN
    assert wallets.balances(db, w.id) == (D("10"), D("0"))  # asgjë s'u debitua
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "15", TopupMethod.CASH).id)
    db.commit()
    paid = billing.pay_from_wallet(db, OWNER, inv.id)
    billing.pay_from_wallet(db, OWNER, inv.id)  # retry
    db.commit()
    assert paid.status == InvoiceStatus.PAID and paid.paid_via == "wallet"
    assert wallets.balances(db, w.id) == (D("5"), D("0"))
    assert db.query(LedgerEntry).filter_by(entry_type=EntryType.INVOICE).count() == 1
    assert wallets.verify_wallet(db, w.id)


def test_autopay_succeeds_when_funded(db, world):  # noqa: F811
    w, _ = world
    cheap = billing.create_plan(db, "cheap", "Cheap", "EUR", "4.00")
    sub = subscribe(db, cheap, auto_pay=True)
    inv = billing.generate_invoice(db, sub.id, AFTER)
    db.commit()
    assert inv.status == InvoiceStatus.PAID and wallets.balances(db, w.id)[0] == D("6")
    types = [e.type for e in db.query(Event).order_by(Event.id)]
    assert types == ["invoice.issued", "invoice.paid"]
    assert db.query(Event).filter_by(type="invoice.paid").one().data["via"] == "wallet"


def test_wallet_currency_mismatch_leaves_invoice_open(db, world):  # noqa: F811
    usd = billing.create_plan(db, "usd", "USD", "USD", "3.00")
    sub = subscribe(db, usd, auto_pay=True)
    inv = billing.generate_invoice(db, sub.id, AFTER)
    db.commit()
    assert inv.status == InvoiceStatus.OPEN
    with pytest.raises(NotFound):
        billing.pay_from_wallet(db, OWNER, inv.id)
    with pytest.raises(NotFound):  # faturë e tjetrit
        billing.pay_from_wallet(db, "c2", inv.id)


# --- Plani i ri dhe anulimi ---------------------------------------------------------------


def test_plan_change_applies_next_period_and_cancel_at_period_end(db, plan):
    pro = billing.create_plan(db, "pro", "Pro", "EUR", "50.00")
    sub = subscribe(db, plan)
    billing.assign_plan(db, OWNER, pro.id, auto_pay=False, now=T0)
    db.commit()
    assert sub.plan_id == plan.id and sub.pending_plan_id == pro.id
    first = billing.generate_invoice(db, sub.id, AFTER)
    assert first.total == D("20.00") and sub.plan_id == pro.id and sub.pending_plan_id is None
    billing.cancel_subscription(db, OWNER)
    db.commit()
    second = billing.generate_invoice(db, sub.id, datetime(2030, 3, 16, tzinfo=UTC))
    db.commit()
    assert second.total == D("50.00") and sub.status.value == "cancelled"
    assert billing.generate_invoice(db, sub.id, datetime(2030, 4, 16, tzinfo=UTC)) is None
    assert db.query(Invoice).count() == 2


def test_no_invoice_without_profile_is_postponed_not_lost(db, plan):
    profile(db)
    sub = billing.assign_plan(db, OWNER, plan.id, now=T0)
    db.commit()
    from app.models.billing import BillingProfile

    db.delete(db.query(BillingProfile).one())
    db.commit()
    assert billing.generate_invoice(db, sub.id, AFTER) is None and sub.periods_billed == 0
    profile(db)
    assert billing.generate_invoice(db, sub.id, AFTER) is not None


# --- Anulimi i faturës -------------------------------------------------------------------


def test_void_rules(db, plan, world):  # noqa: F811
    w, _ = world
    sub = subscribe(db, plan)
    inv = billing.generate_invoice(db, sub.id, AFTER)
    db.commit()
    with pytest.raises(billing.InvalidBilling):
        billing.void_invoice(db, inv.id, "")
    pay = payments.start_payment(db, OWNER, "invoice", invoice_id=inv.id)
    billing.void_invoice(db, inv.id, "issued by mistake")
    db.commit()
    assert inv.status == InvoiceStatus.VOID and pay.status == PaymentStatus.EXPIRED
    with pytest.raises(Conflict):
        billing.pay_from_wallet(db, OWNER, inv.id)
    with pytest.raises(Conflict):
        billing.void_invoice(db, inv.id, "again")
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "30", TopupMethod.CASH).id)
    other = billing.generate_invoice(db, sub.id, datetime(2030, 3, 16, tzinfo=UTC))
    billing.pay_from_wallet(db, OWNER, other.id)
    with pytest.raises(Conflict):  # e paguar: kërkon credit note (jashtë kësaj faze)
        billing.void_invoice(db, other.id, "too late")


def test_overdue_listing(db, plan):
    sub = subscribe(db, plan)
    inv = billing.generate_invoice(db, sub.id, AFTER)
    db.commit()
    assert billing.overdue(db, AFTER + timedelta(days=13)) == []
    assert billing.overdue(db, AFTER + timedelta(days=15)) == [inv]


# --- Pagesa online ----------------------------------------------------------------------


def test_topup_payment_credits_wallet_exactly_once(db, world):  # noqa: F811
    w, _ = world
    p = payments.start_payment(db, OWNER, "topup", "25.50", wallet_id=w.id)
    db.commit()
    assert p.status == PaymentStatus.PENDING and p.checkout_url.startswith(
        "https://pay.example.com/"
    )
    for _ in range(3):  # webhook i përsëritur
        payments.complete(db, "fake", p.external_id, "succeeded", "25.50", "eur")
        db.commit()
    assert wallets.balances(db, w.id)[0] == D("35.50")
    assert p.status == PaymentStatus.SUCCEEDED
    assert wallets.verify_wallet(db, w.id)
    assert [e.type for e in db.query(Event)] == ["payment.succeeded"]


def test_amount_and_currency_are_verified_against_server_record(db, world):  # noqa: F811
    w, _ = world
    for amount, cur in (
        ("1.00", "EUR"),
        ("25.51", "EUR"),
        ("25.50", "USD"),
        (None, "EUR"),
        ("abc", "EUR"),
    ):
        p = payments.start_payment(db, OWNER, "topup", "25.50", wallet_id=w.id)
        db.commit()
        with pytest.raises(Conflict):
            payments.complete(db, "fake", p.external_id, "succeeded", amount, cur)
        db.commit()
        assert p.status == PaymentStatus.FAILED and p.failure_reason == "amount_mismatch"
    assert wallets.balances(db, w.id)[0] == D("10")  # asnjë kredit


def test_payment_validation(db, world):  # noqa: F811
    w, _ = world
    with pytest.raises(wallets.InvalidAmount):
        payments.start_payment(db, OWNER, "topup", "0.5", wallet_id=w.id)  # nën minimumin
    with pytest.raises(wallets.InvalidAmount):
        payments.start_payment(db, OWNER, "topup", "20000", wallet_id=w.id)
    with pytest.raises(wallets.InvalidAmount):
        payments.start_payment(db, OWNER, "topup", 5.5, wallet_id=w.id)
    with pytest.raises(NotFound):  # wallet i tjetrit
        payments.start_payment(db, "c2", "topup", "5", wallet_id=w.id)
    with pytest.raises(NotFound):
        payments.complete(db, "fake", "cs_none", "succeeded", "5", "EUR")


def test_invoice_payment_uses_server_amount_and_marks_paid(db, plan):
    sub = subscribe(db, plan)
    inv = billing.generate_invoice(db, sub.id, AFTER)
    db.commit()
    p = payments.start_payment(
        db, OWNER, "invoice", amount="0.01", invoice_id=inv.id
    )  # shuma injorohet
    assert p.amount == inv.total and p.currency == inv.currency
    assert payments.start_payment(db, OWNER, "invoice", invoice_id=inv.id).id == p.id  # një seancë
    payments.complete(db, "fake", p.external_id, "succeeded", str(inv.total), "EUR")
    db.commit()
    assert inv.status == InvoiceStatus.PAID and inv.paid_via == "online"
    with pytest.raises(Conflict):
        payments.start_payment(db, OWNER, "invoice", invoice_id=inv.id)
    with pytest.raises(NotFound):
        payments.start_payment(db, "c2", "invoice", invoice_id=inv.id)


def test_money_is_never_lost_when_invoice_settled_meanwhile(db, plan, world):  # noqa: F811
    w, _ = world
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "40", TopupMethod.CASH).id)
    sub = subscribe(db, plan)
    inv = billing.generate_invoice(db, sub.id, AFTER)
    db.commit()
    p = payments.start_payment(db, OWNER, "invoice", invoice_id=inv.id)
    db.commit()
    billing.pay_from_wallet(db, OWNER, inv.id)  # klienti paguan me wallet ndërsa checkout hapet
    db.commit()
    before = wallets.balances(db, w.id)[0]
    payments.complete(db, "fake", p.external_id, "succeeded", str(inv.total), "EUR")
    db.commit()
    assert p.failure_reason == "invoice_already_settled"
    assert wallets.balances(db, w.id)[0] == before + inv.total  # kreditohet, s'humbet
    assert inv.paid_via == "wallet"


def test_failed_and_expired_payments(db, world, fresh_gateway):  # noqa: F811
    w, _ = world
    p = payments.start_payment(db, OWNER, "topup", "5", wallet_id=w.id)
    db.commit()
    payments.complete(db, "fake", p.external_id, "failed", None, None)
    payments.complete(db, "fake", p.external_id, "failed", None, None)  # idempotent
    with pytest.raises(Conflict):
        payments.complete(db, "fake", p.external_id, "succeeded", "5", "EUR")
    old = payments.start_payment(db, OWNER, "topup", "6", wallet_id=w.id)
    db.commit()
    assert payments.expire_pending(db, now=datetime.now(UTC) + timedelta(days=2)) == 1
    assert old.status == PaymentStatus.EXPIRED
    fresh_gateway.fail = True
    with pytest.raises(payments.GatewayError):
        payments.start_payment(db, OWNER, "topup", "7", wallet_id=w.id)
    assert db.query(Payment).count() == 2  # asnjë rresht i gjysmë-krijuar


# --- API ----------------------------------------------------------------------------------

BOOT = {"X-Admin-Key": "test-key"}


@pytest.fixture
def raw_client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app())


def _key(c, role, owner=None):
    r = c.post(
        "/v1/admin/api-keys", json={"name": "k", "role": role, "owner_ref": owner}, headers=BOOT
    )
    return {"Authorization": f"Bearer {r.json()['key']}"}


def test_api_end_to_end(db, world, raw_client):  # noqa: F811
    c = raw_client
    h1, h2, fin = _key(c, "client", "c1"), _key(c, "client", "c2"), _key(c, "finance")
    # stafi krijon planin dhe TVSH-në; klienti s'mund
    assert c.post("/v1/admin/billing/plans", json={"code": "gold", "name": "Gold", "currency": "EUR",
                  "monthly_fee": "10", "included_emails": 5, "email_overage_price": "0.1"},
                  headers=h1).status_code == 403  # fmt: skip
    plan = c.post("/v1/admin/billing/plans", json={"code": "gold", "name": "Gold", "currency": "EUR",
                  "monthly_fee": "10", "included_emails": 5, "email_overage_price": "0.1"},
                  headers=fin).json()  # fmt: skip
    assert plan["monthly_fee"] == "10.000000"
    prof = {"legal_name": "<script>alert(1)</script> Ltd", "address": "Main 1", "country": "AL",
            "email": "a@acme.example", "tax_id": "K1"}  # fmt: skip
    assert c.put("/v1/billing/profile", json={**prof, "vat_rate": "0.99"}, headers=h1).json()[
        "vat_rate"
    ] in ("0.0000", "0")
    assert c.put(
        "/v1/admin/billing/c1/profile", json={**prof, "vat_rate": "0.2"}, headers=fin
    ).json()["vat_rate"] in ("0.2000", "0.2")
    assert c.put("/v1/admin/billing/c1/subscription", json={"plan_id": plan["id"], "auto_pay": False},
                 headers=fin).status_code == 200  # fmt: skip
    sub = c.get("/v1/billing/subscription", headers=h1).json()
    assert sub["plan"]["code"] == "gold" and sub["usage"]["emails"] == 0
    assert c.get("/v1/billing/subscription", headers=h2).json() is None
    # lësho faturën (kalojmë kohën përmes shërbimit; worker-i e bën këtë automatikisht)
    s = db.query(Subscription).one()
    inv = billing.generate_invoice(db, s.id, datetime.now(UTC) + timedelta(days=40))
    db.commit()
    lst = c.get("/v1/billing/invoices", headers=h1).json()
    assert [i["number"] for i in lst] == [inv.number] and lst[0]["total"] == "12.000000"
    assert c.get("/v1/billing/invoices", headers=h2).json() == []
    assert c.get(f"/v1/billing/invoices/{inv.id}", headers=h2).status_code == 404
    det = c.get(f"/v1/billing/invoices/{inv.id}", headers=h1).json()
    assert det["lines"][0]["description"] == "Gold - monthly fee" and det["tax"] == "2.000000"
    page = c.get(f"/v1/billing/invoices/{inv.id}/html", headers=h1)
    assert page.status_code == 200 and "&lt;script&gt;" in page.text and "<script>" not in page.text
    csps = [v for k, v in page.headers.items() if k.lower() == "content-security-policy"]
    assert len(csps) == 1 and "style-src 'unsafe-inline'" in csps[0]
    assert c.post(f"/v1/billing/invoices/{inv.id}/pay-from-wallet", headers=h2).status_code == 404
    assert (
        c.post(f"/v1/billing/invoices/{inv.id}/pay-from-wallet", headers=h1).status_code == 402
    )  # 10 < 12
    pay = c.post("/v1/billing/payments", json={"purpose": "invoice", "invoice_id": inv.id, "amount": "0.01"},
                 headers=h1)  # fmt: skip
    assert (
        pay.status_code == 201
        and pay.json()["amount"] == "12.000000"
        and pay.json()["checkout_url"]
    )
    ov = c.get("/v1/admin/billing/overdue", headers=fin).json()
    assert ov == [] or ov[0]["number"] == inv.number
    assert (
        c.post(
            f"/v1/admin/billing/invoices/{inv.id}/void", json={"reason": "x"}, headers=fin
        ).status_code
        == 422
    )
    actions = {a["action"] for a in c.get("/v1/admin/audit", headers=BOOT).json()}
    assert {
        "plan.create",
        "subscription.assign",
        "billing.profile_admin",
        "payment.start",
    } <= actions


def test_payment_webhook(db, world, raw_client, monkeypatch):  # noqa: F811
    w, _ = world
    monkeypatch.setattr(settings, "dlr_secrets", {"fake": "sec"})
    p = payments.start_payment(db, OWNER, "topup", "12", wallet_id=w.id)
    db.commit()

    def post(body, secret="sec"):
        raw = json.dumps(body).encode()
        sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return raw_client.post("/webhooks/payments/fake", content=raw, headers={"X-Signature": sig})

    ok = {"external_id": p.external_id, "status": "succeeded", "amount": "12", "currency": "EUR"}
    assert post(ok, secret="bad").status_code == 401
    assert post({**ok, "status": "refunded"}).status_code == 422
    assert post({**ok, "external_id": "cs_none"}).status_code == 404
    assert post({**ok, "amount": "13"}).status_code == 409
    db.expire_all()
    assert wallets.balances(db, w.id)[0] == D("10") and p.failure_reason == "amount_mismatch"
    p2 = payments.start_payment(db, OWNER, "topup", "12", wallet_id=w.id)
    db.commit()
    assert post({**ok, "external_id": p2.external_id}).json() == {"outcome": "applied"}
    assert post({**ok, "external_id": p2.external_id}).status_code == 200
    db.expire_all()
    assert wallets.balances(db, w.id)[0] == D("22")
