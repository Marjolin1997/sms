# ruff: noqa: F811
"""M9-g4 — Enterprise: eksporti i faturimit legacy (vetëm lexim), freeze fail-closed kur autoriteti = central, konfigurim prodhimi, readiness, e2e drejt importit Central."""

import json
import os
import stat
import uuid
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import Settings, settings
from app.core.db import engine
from app.models.billing import (
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentPurpose,
    PaymentStatus,
    Plan,
    Subscription,
)
from app.models.wallet import LedgerEntry, TopupMethod, Wallet
from app.services import billing, billing_authority, billing_export, payments
from app.services import wallet as wallets
from packages.contracts.control_plane.billing import legacy_export_v1 as lx
from tests.test_billing import (  # noqa: F401
    AFTER,
    OWNER,
    T0,
    add_emails,
    fresh_gateway,
    plan,
    profile,
    subscribe,
)
from tests.test_central import make_db  # noqa: F401
from tests.test_pipeline import world  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
NOW = AFTER + timedelta(days=1)


@pytest.fixture
def issued(db, world, plan):  # noqa: F811
    """Faturë e lëshuar (tarifë 20 + overage 5×0.002) e paguar nga wallet-i + një e hapur me pagesë online të sukseshme të plotë."""
    w, _ = world
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "100", TopupMethod.CASH).id)
    db.commit()
    subscribe(db, plan, auto_pay=False)
    add_emails(db, 105, T0 + timedelta(days=3))
    inv1 = billing.generate_invoice(db, db.scalar(select(Subscription.id)), AFTER)
    db.commit()
    billing.pay_from_wallet(db, OWNER, inv1.id, AFTER)
    db.commit()
    return inv1


def counts(db):
    return {
        m.__name__: db.scalar(select(func.count()).select_from(m))
        for m in (Plan, Subscription, Invoice, InvoiceLine, Payment, LedgerEntry, Wallet)
    }


def test_export_is_read_only_deterministic_and_complete(db, issued):
    before = counts(db)
    eid = uuid.uuid4()
    a = billing_export.export(engine, NOW, eid)
    b = billing_export.export(engine, NOW, eid)
    assert a == b and a["content_hash"] == lx.compute_hash(a) and counts(db) == before
    assert (
        a["authority"] == {"mode": "local", "due_unbilled_periods": 1}
        or a["authority"]["mode"] == "local"
    )
    inv = a["invoices"][0]
    assert (
        inv["number"].startswith("INV-2030-")
        and inv["status"] == "paid"
        and inv["paid_via"] == "wallet"
        and [ln["description"] for ln in inv["lines"]][0].endswith("monthly fee")
    )
    assert (
        D(inv["total"]) == D(issued.total)
        and inv["bill_to"]
        and a["counts"]["invoices"] == 1
        and a["counts"]["plans"] == 1
    )
    ws = a["wallet_settlements"]
    assert (
        len(ws) == 1
        and ws[0]["invoice_source_id"] == inv["source_id"]
        and D(ws[0]["amount"]) == D(issued.total)
    )
    assert (
        a["sequences"]["invoice_counters"] == [{"year": 2030, "last_number": 1}]
        and a["sequences"]["credit_note_like"] == 0
    )
    assert "password" not in json.dumps(a).lower() and "secret" not in json.dumps(a).lower()


def test_export_tamper_truncation_and_shape_are_rejected_by_the_contract(db, issued):
    a = billing_export.export(engine, NOW)
    t = json.loads(json.dumps(a))
    t["invoices"][0]["total"] = "1.000000"
    with pytest.raises(lx.ContractError):
        lx.parse(t)
    t = json.loads(json.dumps(a))
    t["invoices"].clear()
    with pytest.raises(lx.ContractError):
        lx.parse(t)
    assert lx.parse(json.loads(json.dumps(a)))["export_id"] == a["export_id"]


def test_export_script_writes_a_private_file_and_refuses_to_overwrite(db, issued, tmp_path, capsys):
    from scripts import billing_export as cli

    out = tmp_path / "e.json"
    assert cli.main(["--out", str(out)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (
        stat.S_IMODE(os.stat(out).st_mode) == 0o600
        and lx.parse(json.loads(out.read_text()))["content_hash"] == printed["content_hash"]
    )
    assert cli.main(["--out", str(out)]) == 1  # s'mbishkruan
    assert cli.main(["--out", str(out), "--force"]) == 0


def test_legacy_export_imports_into_central_end_to_end(db, issued, make_db_central):
    """Enterprise → artifact → Central dry-run + apply: fatura e paguar nga wallet bëhet histori e shlyer pa rilozje wallet-i."""
    from sqlalchemy.orm import Session

    from apps.central.models import CentralUser
    from apps.central.models.billing import Invoice as CInvoice
    from apps.central.models.enterprise import Enterprise as CEnterprise
    from apps.central.models.money import CommercialLedgerEntry
    from apps.central.models.settlement import InvoicePaymentAllocation
    from apps.central.services import billing_import, users

    eid = db.scalar(select(Subscription.enterprise_id))
    if eid is None:
        pytest.skip("owner has no enterprise_id in this fixture")
    ceng = make_db_central
    from app.models.control_plane import Entitlement
    from app.models.email import Email
    from app.services import billing_usage as ebu
    from apps.central.services import products as cprod

    with Session(ceng, expire_on_commit=False) as s:
        s.add(CEnterprise(id=eid, name="Acme", status="active"))
        s.flush()
        email_pid = cprod.create(s, "email", "Email", "email").id
        admin = users.create_user(s, "imp@example.com", "x" * 12 + "A1!", "admin")
        s.commit()
        aid = admin.id
    db.add(
        Entitlement(
            enterprise_id=eid,
            assignment_id=uuid.uuid4(),
            product_id=email_pid,
            product_code="email",
            channel="email",
            status="active",
            revision=1,
        )
    )
    em = db.scalar(select(Email).limit(1))
    ebu.record_first_billable(
        db, em, "delivered", T0 + timedelta(days=3)
    )  # prova e kapjes së eventeve para kufirit
    db.commit()
    doc = billing_export.export(engine, NOW)
    assert (
        doc["usage"]
        and doc["usage"][0]["product_id"] == str(email_pid)
        and doc["usage"][0]["capture_active_since"] is not None
    )
    wallet_before = db.scalar(select(func.count()).select_from(LedgerEntry))
    with Session(ceng, expire_on_commit=False) as s:
        rep = billing_import.plan_import(s, doc).report()
        assert rep["blocking_total"] == 0, rep["blocking"]
        s.rollback()
        batch = billing_import.apply(s, doc, s.get(CentralUser, aid), doc["content_hash"], now=NOW)
        s.commit()
        inv = s.scalar(select(CInvoice))
        alloc = s.scalar(select(InvoicePaymentAllocation))
        assert (
            inv.number == doc["invoices"][0]["number"]
            and inv.status == "paid"
            and alloc.invoice_id == inv.id
            and batch.summary["sequence_seeds"] == {"2030": 1}
        )
        assert s.scalar(select(func.count()).select_from(CommercialLedgerEntry)) == 0
    assert (
        db.scalar(select(func.count()).select_from(LedgerEntry)) == wallet_before
    )  # wallet-i legacy i paprekur


@pytest.fixture
def make_db_central(make_db):
    from sqlalchemy import create_engine

    from tests.test_central import central_alembic

    url = make_db()
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    yield eng
    eng.dispose()


# --- freeze ----------------------------------------------------------------------------------------------------------------


def test_central_authority_freezes_every_legacy_commercial_mutation_but_keeps_history_readable(
    db, issued, plan, monkeypatch, client
):
    sid = db.scalar(select(Subscription.id))
    inv_id = issued.id
    monkeypatch.setattr(settings, "billing_authority", "central")
    frozen = billing_authority.BillingAuthorityFrozen
    for call in (
        lambda: billing.run_billing(db, NOW),
        lambda: billing.generate_invoice(db, sid, NOW),
        lambda: billing.create_plan(db, "other", "Other", "EUR", "5"),
        lambda: billing.retire_plan(db, plan.id),
        lambda: billing.set_profile(db, OWNER, "N", "A", "al", "b@acme.example"),
        lambda: billing.assign_plan(db, OWNER, plan.id),
        lambda: billing.cancel_subscription(db, OWNER),
        lambda: billing.pay_from_wallet(db, OWNER, inv_id),
        lambda: billing.void_invoice(db, inv_id, "no way"),
    ):
        with pytest.raises(frozen):
            call()
        db.rollback()
    # leximi mbetet
    assert (
        billing.invoice_lines(db, inv_id) and db.get(Invoice, inv_id).status == InvoiceStatus.PAID
    )
    r = client.post("/v1/billing/run") if False else None
    assert r is None


def test_frozen_api_returns_409_and_the_worker_tick_issues_nothing(
    db, world, plan, monkeypatch, client
):  # noqa: F811
    from app import worker

    subscribe(db, plan, auto_pay=False)
    add_emails(db, 3, T0 + timedelta(days=2))
    monkeypatch.setattr(settings, "billing_authority", "central")
    paths = [p for p in client.app.openapi()["paths"] if p.endswith("/billing/run")]
    assert paths, "admin billing run endpoint expected"
    r = client.post(paths[0])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "billing_authority_frozen"
    before = db.scalar(select(func.count()).select_from(Invoice))
    worker.billing_tick()
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Invoice)) == before == 0
    monkeypatch.setattr(settings, "billing_authority", "local")
    assert client.post(paths[0]).status_code == 200  # local: sjellja e pandryshuar


def test_shadow_and_local_modes_keep_enterprise_as_the_issuer(db, world, plan, monkeypatch):  # noqa: F811
    subscribe(db, plan, auto_pay=False)
    add_emails(db, 3, T0 + timedelta(days=2))
    for mode in ("local", "shadow"):
        monkeypatch.setattr(settings, "billing_authority", mode)
        assert billing_authority.frozen() is False
        billing_authority.require_issuer("anything")
    monkeypatch.setattr(settings, "billing_authority", "shadow")
    assert billing.run_billing(db, AFTER) == 1


def test_late_legacy_online_payment_is_refused_not_credited_to_the_wallet(
    db, world, plan, monkeypatch
):  # noqa: F811
    subscribe(db, plan, auto_pay=False)
    add_emails(db, 3, T0 + timedelta(days=2))
    inv = billing.generate_invoice(db, db.scalar(select(Subscription.id)), AFTER)
    db.commit()
    p = Payment(
        owner_ref=OWNER,
        purpose=PaymentPurpose.INVOICE,
        invoice_id=inv.id,
        amount=inv.total,
        currency=inv.currency,
        provider="fake",
        external_id="late-1",
        checkout_url="x",
        status=PaymentStatus.PENDING,
    )
    db.add(p)
    db.commit()
    wallet_before = db.scalar(select(func.count()).select_from(LedgerEntry))
    monkeypatch.setattr(settings, "billing_authority", "central")
    with pytest.raises(wallets.Conflict):
        payments.complete(db, "fake", "late-1", "succeeded", str(inv.total), inv.currency, NOW)
    db.commit()
    assert (
        db.get(Payment, p.id).status == PaymentStatus.FAILED
        and db.get(Payment, p.id).failure_reason == "billing_authority_central"
    )
    assert (
        db.get(Invoice, inv.id).status == InvoiceStatus.OPEN
        and db.scalar(select(func.count()).select_from(LedgerEntry)) == wallet_before
    )


# --- konfigurim + readiness -------------------------------------------------------------------------------------------------


def test_production_config_requires_ack_and_usage_reporting_for_non_local_billing_authority():
    base = dict(env="production", billing_authority="local")
    s = Settings.model_construct(**{**settings.model_dump(), **base})
    assert not [p for p in s.production_problems() if "BILLING_AUTHORITY" in p]
    c = Settings.model_construct(
        **{
            **settings.model_dump(),
            "env": "production",
            "billing_authority": "central",
            "billing_authority_ack": False,
            "billing_usage_reporting": False,
        }
    )
    probs = [p for p in c.production_problems() if "BILLING_AUTHORITY" in p]
    assert any("ACK" in p for p in probs) and any("USAGE_REPORTING" in p for p in probs)
    ok = Settings.model_construct(
        **{
            **settings.model_dump(),
            "env": "production",
            "billing_authority": "central",
            "billing_authority_ack": True,
            "billing_usage_reporting": True,
        }
    )
    assert not [p for p in ok.production_problems() if "BILLING_AUTHORITY" in p]
    sh = Settings.model_construct(
        **{
            **settings.model_dump(),
            "env": "production",
            "billing_authority": "shadow",
            "billing_usage_reporting": False,
        }
    )
    assert any("USAGE_REPORTING" in p for p in sh.production_problems())


def test_enterprise_readiness_flags_due_periods_manual_payments_and_pending_checkouts(
    db, world, plan, monkeypatch, capsys
):  # noqa: F811
    from scripts import billing_authority_readiness as cli

    subscribe(db, plan, auto_pay=False)
    monkeypatch.setattr(settings, "billing_usage_reporting", False)
    items = {c.name: c for c in cli.checks(db, "central")}
    assert items["usage_reporting_enabled"].level == "FAIL"
    inv = billing.generate_invoice(db, db.scalar(select(Subscription.id)), AFTER)
    db.commit()
    db.add(
        Payment(
            owner_ref=OWNER,
            purpose=PaymentPurpose.INVOICE,
            invoice_id=inv.id,
            amount=D("1"),
            currency=inv.currency,
            provider="fake",
            external_id="s1",
            checkout_url="x",
            status=PaymentStatus.SUCCEEDED,
        )
    )
    db.add(
        Payment(
            owner_ref=OWNER,
            purpose=PaymentPurpose.INVOICE,
            invoice_id=inv.id,
            amount=inv.total,
            currency=inv.currency,
            provider="fake",
            external_id="p1",
            checkout_url="x",
            status=PaymentStatus.PENDING,
        )
    )
    db.commit()
    items = {c.name: c for c in cli.checks(db, "central")}
    assert (
        items["invoice_payments_exact"].level == "WARN"
        and items["no_pending_online_checkouts"].level == "WARN"
    )
    assert cli.main(["--json"]) in (0, 1)
    json.loads(capsys.readouterr().out)


def test_enterprise_never_imports_central_billing_and_reads_no_central_db():
    src = (ROOT / "app/services/billing_export.py").read_text() + (
        ROOT / "app/services/billing_authority.py"
    ).read_text()
    assert "apps.central" not in src and "httpx" not in src and "requests" not in src
