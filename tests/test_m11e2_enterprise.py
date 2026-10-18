# ruff: noqa: F811
"""M11-E2 — Enterprise: PDF zyrtar i faturës së klientit (tenant-scoped) dhe `has_pending_recalculation` te wallet-et."""

import io
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from pypdf import PdfReader
from sqlalchemy import event

from app.models.billing import Invoice, InvoiceStatus, Subscription
from app.models.money_authority import (
    G_APPLIED,
    G_DEFERRED,
    G_MATCHED,
    G_MISMATCH,
    G_RECON,
    G_REVERSED,
    G_UNMAPPED,
    G_VOIDED,
    MoneyGrant,
)
from app.services import billing, invoice_document
from app.services import wallet as wallets
from tests.test_billing import BOOT, _key, fresh_gateway, raw_client  # noqa: F401
from tests.test_pipeline import world  # noqa: F401


def text_of(pdf: bytes) -> str:
    return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)


@pytest.fixture
def setup(db, world, raw_client):  # noqa: F811
    c = raw_client
    h1, h2, fin = _key(c, "client", "c1"), _key(c, "client", "c2"), _key(c, "finance")
    plan = c.post("/v1/admin/billing/plans", json={"code": "gold", "name": "Gold", "currency": "EUR", "monthly_fee": "10",
                  "included_emails": 5, "email_overage_price": "0.1"}, headers=fin).json()  # fmt: skip
    prof = {"legal_name": "Müller & Söhne – Tiranë Sh.p.k.", "address": "Rruga e Durrësit 1, Tiranë", "country": "AL",
            "email": "a@acme.example", "tax_id": "K1"}  # fmt: skip
    c.put("/v1/admin/billing/c1/profile", json={**prof, "vat_rate": "0.2"}, headers=fin)
    c.put(
        "/v1/admin/billing/c1/subscription",
        json={"plan_id": plan["id"], "auto_pay": False},
        headers=fin,
    )
    s = db.query(Subscription).one()
    inv = billing.generate_invoice(db, s.id, datetime.now(UTC) + timedelta(days=40))
    db.commit()
    return SimpleNamespace(c=c, h1=h1, h2=h2, fin=fin, inv=inv, world=world)


def test_own_invoice_pdf(setup):
    c, inv = setup.c, setup.inv
    r = c.get(f"/v1/billing/invoices/{inv.id}/pdf", headers=setup.h1)
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"] == f'attachment; filename="invoice-{inv.number}.pdf"'
    assert (
        r.headers["cache-control"] == "private, no-store"
        and r.headers["x-content-type-options"] == "nosniff"
    )
    assert r.content.startswith(b"%PDF-")
    t = text_of(r.content)
    assert (
        inv.number in t
        and "Müller & Söhne – Tiranë Sh.p.k." in t
        and "Rruga e Durrësit 1, Tiranë" in t
        and "K1" in t
    )
    assert "Gold - monthly fee" in t and "EUR" in t
    assert (
        f"{D(inv.total):,.2f}" in t and f"{D(inv.tax):,.2f}" in t and f"{D(inv.subtotal):,.2f}" in t
    )
    assert (
        c.get(f"/v1/billing/invoices/{inv.id}/pdf?inline=true", headers=setup.h1)
        .headers["content-disposition"]
        .startswith("inline;")
    )


def test_pdf_is_reproducible_and_uses_the_stored_bill_to_snapshot(setup, db):
    c, inv = setup.c, setup.inv
    a = c.get(f"/v1/billing/invoices/{inv.id}/pdf", headers=setup.h1).content
    c.put("/v1/admin/billing/c1/profile", json={"legal_name": "Renamed Co", "address": "Elsewhere 2", "country": "AL",
          "email": "n@x.example", "tax_id": "K2", "vat_rate": "0.2"}, headers=setup.fin)  # fmt: skip
    b = c.get(f"/v1/billing/invoices/{inv.id}/pdf", headers=setup.h1).content
    assert a == b and "Renamed Co" not in text_of(b)


def test_cross_tenant_pdf_is_blocked_everywhere(setup):
    c, inv = setup.c, setup.inv
    for h in (setup.h2,):
        assert c.get(f"/v1/billing/invoices/{inv.id}/pdf", headers=h).status_code == 404
    assert c.get(f"/v1/billing/invoices/{inv.id}/pdf").status_code in (401, 403)
    assert c.get("/v1/billing/invoices/999999/pdf", headers=setup.h1).status_code == 404
    assert c.get("/v1/billing/invoices/abc/pdf", headers=setup.h1).status_code == 422
    assert (
        c.get(f"/v1/billing/invoices/{inv.id}/pdf?owner_ref=c1", headers=setup.h2).status_code
        == 404
    )  # owner_ref s'e ngre klientin


def test_pdf_failure_is_clean_and_logged(setup, monkeypatch, caplog, db):
    def boom(doc):
        raise RuntimeError("secret /srv/internal")

    monkeypatch.setattr(invoice_document, "render_invoice_pdf", boom)
    with caplog.at_level(logging.ERROR):
        r = setup.c.get(f"/v1/billing/invoices/{setup.inv.id}/pdf", headers=setup.h1)
    assert r.status_code == 500 and r.json()["detail"] == {
        "code": "pdf_render_failed",
        "message": "the invoice PDF could not be generated",
    }
    assert "secret" not in r.text and any(
        "invoice pdf render failed" in x.message for x in caplog.records
    )
    assert db.get(Invoice, setup.inv.id).status == InvoiceStatus.OPEN


def test_pdf_renders_the_stored_total_even_when_unusual():
    inv = SimpleNamespace(number="INV-X-1", status=InvoiceStatus.OPEN, currency="EUR", issued_at=datetime(2030, 1, 1, tzinfo=UTC), due_at=datetime(2030, 1, 15, tzinfo=UTC),
                          period_start=datetime(2029, 12, 1, tzinfo=UTC), period_end=datetime(2030, 1, 1, tzinfo=UTC), bill_to='{"legal_name": "B"}',
        issuer=None,
                          subtotal=D("5"), vat_rate=D("0.2"), tax=D("1"), total=D("1234567.89"), paid_at=None)  # fmt: skip
    line = SimpleNamespace(description="one", quantity=D("1"), unit_price=D("1"), amount=D("1"))
    t = text_of(invoice_document.render(inv, [line])[0])
    assert "1,234,567.89 EUR" in t and "6.00" not in t


def test_pdf_has_no_mutation_routes_and_is_bounded(setup, db):
    c = setup.c
    spec = c.app.openapi()["paths"]
    assert set(spec["/v1/billing/invoices/{invoice_id}/pdf"]) == {"get"}
    n = []
    listener = lambda conn, cur, stmt, *a: n.append(stmt)  # noqa: E731
    bind = db.get_bind()
    event.listen(bind, "before_cursor_execute", listener)
    try:
        assert (
            c.get(f"/v1/billing/invoices/{setup.inv.id}/pdf", headers=setup.h1).status_code == 200
        )
    finally:
        event.remove(bind, "before_cursor_execute", listener)
    writes = [
        s
        for s in n
        if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) and "sms_api_keys" not in s
    ]  # last_used_at: ekzistues
    assert writes == [] and len(n) <= 12


# ---------------- pending recalculation ----------------


def grant(db, wallet_id, status, seq):
    db.add(MoneyGrant(grant_id=uuid.uuid4(), enterprise_id=uuid.uuid4(), account_id=uuid.uuid4(), product_id=uuid.uuid4(), currency="EUR", amount=D("1"),
                      purpose="standard", wallet_id=wallet_id, status=status, issued_seq=seq, issued_event_id=uuid.uuid4(), issued_payload_hash="0" * 64))  # fmt: skip
    db.commit()


def wallets_of(c, h):
    r = c.get("/v1/wallets", headers=h)
    assert r.status_code == 200
    return r.json()


def test_pending_recalculation_flag(setup, db):
    c, (w, _) = setup.c, setup.world
    before = wallets_of(c, setup.h1)
    assert before[0]["has_pending_recalculation"] is False and "available" in before[0]
    seq = 1
    for status in (
        G_APPLIED,
        G_MATCHED,
        G_DEFERRED,
        G_REVERSED,
        G_VOIDED,
    ):  # statuse që NUK janë pending
        grant(db, w.id, status, seq)
        seq += 1
    assert wallets_of(c, setup.h1)[0]["has_pending_recalculation"] is False
    for status in (G_RECON, G_MISMATCH, G_UNMAPPED):
        db.query(MoneyGrant).filter(MoneyGrant.wallet_id == w.id).delete()
        db.commit()
        grant(db, w.id, status, 100 + seq)
        seq += 1
        got = wallets_of(c, setup.h1)[0]
        assert got["has_pending_recalculation"] is True, status
        # semantikë: vetëm shfaqje — bilanci i serverit mbetet i njëjtë dhe i disponueshëm
        assert (got["available"], got["held"]) == (before[0]["available"], before[0]["held"])
    assert D(wallets.balances(db, w.id)[0]) == D(before[0]["available"])


def test_pending_flag_is_tenant_scoped_and_one_query(setup, db):
    c, (w, _) = setup.c, setup.world
    grant(db, w.id, G_RECON, 1)
    assert wallets_of(c, setup.h2) == []  # tenant-i tjetër s'ka wallet; asnjë rrjedhje
    w2 = wallets.create_wallet(db, "c2", "EUR")
    db.commit()
    other = wallets_of(c, setup.h2)
    assert (
        len(other) == 1
        and other[0]["has_pending_recalculation"] is False
        and other[0]["id"] == w2.id
    )
    assert wallets_of(c, setup.h1)[0]["has_pending_recalculation"] is True
    w3 = wallets.create_wallet(db, "c1", "USD")
    db.commit()

    def count():
        n = []
        listener = lambda conn, cur, stmt, *a: (
            n.append(stmt) if "sms_money_grants" in stmt else None
        )  # noqa: E731
        bind = db.get_bind()
        event.listen(bind, "before_cursor_execute", listener)
        try:
            c.get("/v1/wallets", headers=setup.h1)
        finally:
            event.remove(bind, "before_cursor_execute", listener)
        return len(n)

    assert count() == 1  # një pyetje për sa wallet-e që ka tenant-i (jo N+1)
    assert w3.id in [x["id"] for x in wallets_of(c, setup.h1)]
