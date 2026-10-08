# ruff: noqa: F811
"""M9-g4 — Central: importi i faturimit legacy (artifact), idempotencë, evidencë, sekuenca, baseline përdorimi, shadow, autoritet, readiness, rollback."""

import copy
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select, text

from apps.central.core import errors
from apps.central.core.config import settings
from apps.central.models import AuditLog
from apps.central.models.billing import (
    BillingPeriod,
    BillingSubscription,
    CommercialPlan,
    Invoice,
    InvoiceLine,
    InvoiceNumberSequence,
    PlanVersion,
)
from apps.central.models.billing_import import (
    BillingAuthorityState,
    BillingImportBatch,
    BillingImportIssue,
    BillingImportItem,
    BillingShadowComparison,
    BillingUsageBaseline,
)
from apps.central.models.money import CommercialLedgerEntry, CreditGrant, Payment
from apps.central.models.settlement import InvoicePaymentAllocation
from apps.central.services import (
    billing,
    billing_authority,
    billing_import,
    billing_shadow,
    billing_usage,
    pricing,
)
from apps.central.services import enterprise_products as eprod
from apps.central.services import products as prod
from apps.central.tools import billing_authority as cli_auth
from apps.central.tools import billing_authority_readiness as cli_ready
from apps.central.tools import billing_import as cli_import
from apps.central.tools import billing_run as cli_run
from apps.central.tools import billing_shadow as cli_shadow
from packages.contracts.control_plane.billing import legacy_export_v1 as lx
from packages.contracts.control_plane.billing import usage_v1 as bv
from tests.test_central import IS_PG, central_alembic, make_db  # noqa: F401
from tests.test_central_auth import PW, auth_secret, bearer, mk, token_for  # noqa: F401
from tests.test_central_products import cdb  # noqa: F401
from tests.test_m9g1_billing import U, b  # noqa: F401

STARTED = "2029-11-15T12:00:00.000000+00:00"  # ankora legacy; periods_billed=2 ⇒ kufiri 2030-01-15
BOUNDARY = "2030-01-15T12:00:00.000000+00:00"
NOW = datetime(2030, 3, 1, tzinfo=UTC)
EXPORT_AT = "2030-01-20T08:00:00.000000+00:00"


def ts(dt: datetime) -> str:
    return lx.format_ts(dt)


def dec(x) -> str:
    return format(D(x).quantize(D("0.000001")), "f")


def line(sid, desc, qty, price, amount=None, src="legacy_plan", ref=None):
    amount = amount if amount is not None else D(qty) * D(price)
    return {"source_id": sid, "description": desc, "quantity": dec(qty), "unit_price": dec(price), "amount": dec(amount.quantize(D("0.01")) if isinstance(amount, D) else amount),
            "pricing_source": src, "pricing_version_ref": ref}  # fmt: skip


def invoice(
    sid,
    number,
    eid,
    sub_sid,
    k,
    lines,
    *,
    status="open",
    paid_at=None,
    paid_via=None,
    voided_reason=None,
    vat="0.2",
    started=STARTED,
):
    from datetime import datetime as dt

    s = dt.fromisoformat(started)
    start, end = billing.add_months(s, k), billing.add_months(s, k + 1)
    subtotal = sum((D(ln["amount"]) for ln in lines), D(0))
    tax = (subtotal * D(vat)).quantize(D("0.01"))
    issued = end
    return {"source_id": sid, "number": number, "enterprise_id": str(eid), "owner_ref": "acme", "subscription_source_id": sub_sid, "period_start": ts(start), "period_end": ts(end),
            "currency": "EUR", "subtotal": dec(subtotal), "vat_rate": dec(vat), "tax": dec(tax), "total": dec(subtotal + tax), "status": status,
            "bill_to": json.dumps({"legal_name": "Acme Sh.p.k.", "address": "Rr. 1", "country": "AL", "tax_id": "K1", "email": "billing@acme.example"}),
            "issued_at": ts(issued), "due_at": ts(issued + timedelta(days=14)), "paid_at": paid_at, "paid_via": paid_via, "voided_reason": voided_reason, "lines": lines}  # fmt: skip


def seal(doc):
    doc = copy.deepcopy(doc)
    doc["counts"] = {k: len(doc[k]) for k in lx.ENTITIES}
    return lx.seal(doc)


def base_doc(eid, product_id, *, included=100, overage_price="0.5", authority="central"):
    inv0 = invoice(
        1,
        "INV-2029-000007",
        eid,
        1,
        0,
        [line(1, "Standard - monthly fee", 1, 20, D("20.00"))],
        status="paid",
        paid_at="2029-12-20T10:00:00.000000+00:00",
        paid_via="online",
    )
    inv1 = invoice(
        2,
        "INV-2030-000003",
        eid,
        1,
        1,
        [
            line(2, "Standard - monthly fee", 1, 20, D("20.00")),
            line(3, "Email overage (2 above 100 included)", 2, "0.5", D("1.00")),
        ],
    )
    return seal(
        {
            "schema": lx.SCHEMA,
            "export_id": str(uuid.uuid4()),
            "generated_at": EXPORT_AT,
            "source": {"system": "enterprise", "alembic_head": "0027"},
            "authority": {"mode": authority, "due_unbilled_periods": 1},
            "plans": [
                {
                    "source_id": 1,
                    "code": "std",
                    "name": "Standard",
                    "currency": "EUR",
                    "monthly_fee": dec(20),
                    "included_emails": included,
                    "email_overage_price": dec(overage_price),
                    "status": "active",
                    "created_at": "2029-01-01T00:00:00.000000+00:00",
                }
            ],  # fmt: skip
            "profiles": [
                {
                    "source_id": 1,
                    "enterprise_id": str(eid),
                    "owner_ref": "acme",
                    "legal_name": "Acme Sh.p.k.",
                    "address": "Rr. 1",
                    "country": "AL",
                    "tax_id": "K1",
                    "email": "billing@acme.example",
                    "vat_rate": dec("0.2"),
                    "updated_at": "2029-11-01T00:00:00.000000+00:00",
                }
            ],  # fmt: skip
            "subscriptions": [
                {
                    "source_id": 1,
                    "enterprise_id": str(eid),
                    "owner_ref": "acme",
                    "plan_source_id": 1,
                    "pending_plan_source_id": None,
                    "status": "active",
                    "started_at": STARTED,
                    "periods_billed": 2,
                    "cancel_at_period_end": False,
                    "auto_pay": True,
                    "created_at": "2029-11-15T12:00:00.000000+00:00",
                }
            ],  # fmt: skip
            "invoices": [inv0, inv1],
            "payments": [
                {
                    "source_id": 1,
                    "enterprise_id": str(eid),
                    "invoice_source_id": 1,
                    "amount": inv0["total"],
                    "currency": "EUR",
                    "provider": "mock",
                    "external_id": "ch_001",
                    "status": "succeeded",
                    "completed_at": "2029-12-20T10:00:00.000000+00:00",
                }
            ],  # fmt: skip
            "wallet_settlements": [],
            "sequences": {
                "invoice_counters": [
                    {"year": 2029, "last_number": 7},
                    {"year": 2030, "last_number": 3},
                ],
                "credit_note_like": 0,
            },
            "usage": [
                {
                    "enterprise_id": str(eid),
                    "product_id": str(product_id),
                    "boundary": BOUNDARY,
                    "cumulative_before_boundary": 10,
                    "watermark_before_boundary": 14,
                    "capture_active_since": "2029-06-01T00:00:00.000000+00:00",
                    "events_total": 12,
                }
            ],  # fmt: skip
        }
    )


def edit(doc, fn):
    d = copy.deepcopy(doc)
    fn(d)
    return seal(d)


@pytest.fixture
def w(b):
    """b (g1) + produkt email + caktim produkti dhe çmim email Central (0.5 EUR) për e1."""
    with b.F() as s:
        a = U(s, b.a1)
        em = prod.create(s, "email", "Email", "email")
        eprod.assign_product(s, b.e1, em.id)
        eff = datetime(2029, 1, 1, tzinfo=UTC)
        base = eff - timedelta(days=10)
        book = pricing.create_book(s, a, "em", "EM", "EUR", now=base)
        v = pricing.new_draft(s, a, book.id, now=base)
        pricing.set_rule(s, a, v.id, "email", "0.5", now=base)
        pricing.activate(s, a, v.id, eff, now=base)
        pricing.assign(s, a, b.e1, em.id, book.id, eff, now=base)
        s.commit()
        b.email = em.id
    b.doc = base_doc(b.e1, b.email)
    return b


def apply(w, doc=None, by=None, authority="local", **kw):
    doc = doc or w.doc
    with w.F() as s:
        batch = billing_import.apply(
            s,
            doc,
            U(s, by or w.a1),
            doc["content_hash"],
            authority=authority,
            now=kw.pop("now", NOW),
            **kw,
        )
        s.commit()
        return batch.id


def n(w, model, *where):
    with w.F() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def one(w, model, *where):
    with w.F() as s:
        return s.scalar(select(model).where(*where))


def plan_of(w, doc=None):
    with w.F() as s:
        return billing_import.plan_import(s, doc or w.doc)


# =============================================================================================================
# dry-run, idempotencë, hash
# =============================================================================================================


def test_dry_run_writes_nothing_and_reports_sequence_seeds(w, tmp_path):
    tables = (
        BillingImportBatch,
        BillingImportItem,
        CommercialPlan,
        PlanVersion,
        BillingSubscription,
        Invoice,
        InvoiceNumberSequence,
        BillingUsageBaseline,
        Payment,
    )
    before = [n(w, t) for t in tables]
    plan = plan_of(w)
    rep = plan.report()
    assert rep["sequence_seeds"] == {"2029": 7, "2030": 3} and rep["blocking_total"] == 0
    assert [n(w, t) for t in tables] == before
    f = tmp_path / "a.json"
    f.write_text(json.dumps(w.doc))
    assert cli_import.main(["--artifact", str(f), "--json"], engine=w.eng) == 0
    assert [n(w, t) for t in tables] == before


def test_same_artifact_is_idempotent_and_hash_mismatch_or_tamper_is_rejected(w):
    first = apply(w)
    counts = {
        t.__name__: n(w, t)
        for t in (
            BillingImportItem,
            Invoice,
            Payment,
            InvoicePaymentAllocation,
            BillingSubscription,
            CommercialPlan,
        )
    }
    assert apply(w) == first  # rirunim ⇒ i njëjti batch
    assert {
        t.__name__: n(w, t)
        for t in (
            BillingImportItem,
            Invoice,
            Payment,
            InvoicePaymentAllocation,
            BillingSubscription,
            CommercialPlan,
        )
    } == counts
    with w.F() as s:
        with pytest.raises(errors.Conflict):  # evidence hash i gabuar
            billing_import.apply(s, w.doc, U(s, w.a1), "0" * 64, now=NOW)
        tampered = copy.deepcopy(w.doc)
        tampered["plans"][0]["monthly_fee"] = dec(99)
        with pytest.raises(errors.Invalid):  # përmbajtja s'përputhet me content_hash
            billing_import.apply(s, tampered, U(s, w.a1), tampered["content_hash"], now=NOW)
        modified = edit(
            w.doc, lambda d: d["plans"][0].update(name="Standard 2")
        )  # i njëjti export_id, përmbajtje tjetër
        with pytest.raises(errors.Conflict):
            billing_import.apply(s, modified, U(s, w.a1), modified["content_hash"], now=NOW)


def test_contract_rejects_truncation_unknown_fields_and_float_amounts(w):
    for mutate in (lambda d: d["invoices"].pop(), lambda d: d.update(extra=1), lambda d: d["plans"][0].update(monthly_fee=20.0), lambda d: d["plans"][0].update(monthly_fee="1e2"),
                   lambda d: d["invoices"].reverse(), lambda d: d["plans"][0].update(created_at="2030-01-01")):  # fmt: skip
        d = copy.deepcopy(w.doc)
        mutate(d)
        with pytest.raises(lx.ContractError):
            lx.parse(d)


# =============================================================================================================
# plane, abonime, periudha
# =============================================================================================================


def test_plan_import_freezes_fee_included_currency_and_keeps_overage_price_out_of_the_plan(w):
    apply(w)
    with w.F() as s:
        p = s.scalar(select(CommercialPlan).where(CommercialPlan.code == "std"))
        v = billing.billing_plans.versions_of(s, p.id)
        assert len(v) == 1 and (
            v[0].status,
            v[0].currency,
            D(v[0].monthly_fee),
            v[0].included_emails,
        ) == ("active", "EUR", D(20), 100)
        item = s.scalar(select(BillingImportItem).where(BillingImportItem.source_table == "plans"))
        assert (
            item.detail["legacy_email_overage_price"] == dec("0.5")
            and item.source_system == "enterprise"
            and item.batch_id
            and len(item.source_hash) == 64
        )


def test_retired_legacy_plan_stays_historical_and_equivalent_import_does_not_duplicate_versions(w):
    d2 = edit(w.doc, lambda d: d["plans"][0].update(status="retired"))
    apply(w, d2)
    with w.F() as s:
        v = s.scalar(select(PlanVersion))
        assert v.status == "retired" and v.retire_reason
    # eksport i ri (export_id i ri): i njëjti plan ⇒ asnjë version i ri
    d3 = edit(d2, lambda d: d.update(export_id=str(uuid.uuid4())))
    apply(w, d3)
    assert n(w, PlanVersion) == 1 and n(w, CommercialPlan) == 1


def test_subscription_period_index_matches_legacy_periods_billed_exactly(w):
    apply(w)
    with w.F() as s:
        sub = s.scalar(select(BillingSubscription))
        assert (sub.next_period_index, sub.anchor_period_index, sub.status) == (
            2,
            0,
            "active",
        ) and billing.utc(sub.anchor_started_at) == datetime(2029, 11, 15, 12, tzinfo=UTC)
        start, end = billing.period_bounds(sub, sub.next_period_index)
        assert start == datetime(
            2030, 1, 15, 12, tzinfo=UTC
        )  # asnjë mbivendosje, asnjë periudhë e anashkaluar
        invs = list(s.scalars(select(Invoice).order_by(Invoice.period_index)))
        assert [i.period_index for i in invs] == [0, 1] and billing.utc(invs[1].period_end) == start
        ps = list(s.scalars(select(BillingPeriod).order_by(BillingPeriod.period_index)))
        assert [(p.period_index, p.provenance, p.status, p.plan_version_id) for p in ps] == [
            (0, "legacy_import", "invoiced", None),
            (1, "legacy_import", "invoiced", None),
        ]


def test_unknown_history_is_not_fabricated_as_no_charge(w):
    d2 = edit(
        w.doc, lambda d: d["subscriptions"][0].update(periods_billed=5)
    )  # periudha 2..4 pa fatura ⇒ s'ka prova
    d2 = edit(d2, lambda d: d["usage"].clear())
    apply(w, d2)
    with w.F() as s:
        assert [
            p.period_index
            for p in s.scalars(select(BillingPeriod).order_by(BillingPeriod.period_index))
        ] == [0, 1]
        assert s.scalar(select(BillingPeriod).where(BillingPeriod.status == "no_charge")) is None


def test_reactivated_subscription_old_segment_invoices_keep_a_negative_legacy_index(w):
    def f(d):
        old = invoice(
            9,
            "INV-2029-000005",
            w.e1,
            1,
            0,
            [line(9, "Standard - monthly fee", 1, 20, D("20.00"))],
            status="void",
            voided_reason="duplicate",
            started="2029-08-01T12:00:00.000000+00:00",
        )
        d["invoices"].append(old)
        d["invoices"].sort(key=lambda r: r["source_id"])

    apply(w, edit(w.doc, f))
    with w.F() as s:
        old = s.scalar(select(Invoice).where(Invoice.number == "INV-2029-000005"))
        assert old.period_index == -9 and old.status == "void" and old.voided_reason == "duplicate"
        assert (
            n(w, BillingPeriod) == 2
        )  # s'ka rresht periudhe për segmentin e vjetër (ankorë e mëparshme)


# =============================================================================================================
# faturat, linjat, shlyerjet
# =============================================================================================================


def test_invoice_import_preserves_arithmetic_provenance_and_unknown_issuer(w):
    apply(w)
    with w.F() as s:
        invs = {i.number: i for i in s.scalars(select(Invoice))}
        assert set(invs) == {"INV-2029-000007", "INV-2030-000003"}
        for i in invs.values():
            assert billing.verify_invoice(s, i) == []
            assert (i.provenance, i.plan_version_id, i.issuer) == (
                "legacy_import",
                None,
                {"provenance": "unknown"},
            ) and i.bill_to["legal_name"] == "Acme Sh.p.k."
        o = invs["INV-2030-000003"]
        assert (D(o.subtotal), D(o.tax), D(o.total), o.status, o.paid_at) == (
            D("21"),
            D("4.2"),
            D("25.2"),
            "open",
            None,
        )
        item = s.scalar(
            select(BillingImportItem).where(
                BillingImportItem.source_table == "invoices", BillingImportItem.source_id == "2"
            )
        )
        assert item.detail["issuer"] == "unknown" and item.detail["plan_version"] == "unknown"


def test_line_classification_and_unmapped_lines_keep_their_amount(w):
    def f(d):
        d["invoices"][1] = invoice(2, "INV-2030-000003", w.e1, 1, 1, [line(2, "Standard - monthly fee", 1, 20, D("20.00")), line(3, "Email overage (50 above 100 included)", 50, "0.02", D("1.00")),
                                                                        line(4, "Setup fee (manual)", 1, "5.00", D("5.00"))])  # fmt: skip

    apply(w, edit(w.doc, f))
    with w.F() as s:
        inv = s.scalar(select(Invoice).where(Invoice.number == "INV-2030-000003"))
        lines = list(
            s.scalars(
                select(InvoiceLine)
                .where(InvoiceLine.invoice_id == inv.id)
                .order_by(InvoiceLine.line_no)
            )
        )
        assert [ln.line_type for ln in lines] == ["monthly_fee", "email_overage", "legacy"] and D(
            inv.subtotal
        ) == sum((D(ln.amount) for ln in lines), D(0))
    assert (
        billing_import.line_type("Email overage (50 above 100 included)", D(49)) == "legacy"
    )  # sasia s'përputhet ⇒ s'nxirret kuptim nga teksti


def test_paid_void_and_open_invoices_import_as_historical_states(w):
    def f(d):
        d["invoices"].append(
            invoice(
                3,
                "INV-2030-000002",
                w.e1,
                1,
                1,
                [line(5, "Standard - monthly fee", 1, 20, D("20.00"))],
                status="void",
                voided_reason="issued by mistake",
            )
        )
        d["invoices"][2]["period_start"], d["invoices"][2]["period_end"] = (
            d["invoices"][1]["period_start"],
            d["invoices"][1]["period_end"],
        )
        d["invoices"].sort(key=lambda r: r["source_id"])

    # period_start i njëjtë me fatura 2 ⇒ UNIQUE (subscription, period_start): kjo duhet të jetë konflikt i shpjeguar nga DB — përdor segment tjetër
    def g(d):
        d["invoices"].append(
            invoice(
                3,
                "INV-2030-000002",
                w.e1,
                1,
                0,
                [line(5, "Standard - monthly fee", 1, 20, D("20.00"))],
                status="void",
                voided_reason="issued by mistake",
                started="2029-07-01T12:00:00.000000+00:00",
            )
        )

    apply(w, edit(w.doc, g))
    with w.F() as s:
        by = {i.number: i for i in s.scalars(select(Invoice))}
        assert by["INV-2029-000007"].status == "paid" and by["INV-2030-000003"].status == "open"
        v = by["INV-2030-000002"]
        assert (
            v.status == "void"
            and v.voided_reason == "issued by mistake"
            and v.voided_at is not None
        )
        assert (
            s.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "credit_note.issue")
            )
            == 0
        )  # pa credit note për void historik
    assert f


def test_full_online_payment_becomes_an_approved_invoice_payment_with_allocation_and_no_ledger(w):
    before = (n(w, CommercialLedgerEntry), n(w, CreditGrant))
    apply(w)
    with w.F() as s:
        pay = s.scalar(select(Payment))
        alloc = s.scalar(select(InvoicePaymentAllocation))
        assert (pay.purpose, pay.status, pay.source, pay.external_reference, pay.account_id) == (
            "invoice",
            "approved",
            "legacy_import",
            "mock:ch_001",
            None,
        )
        assert pay.created_by_label == "system:billing_import" and pay.approved_by_id == w.a1
        assert (
            alloc.payment_id == pay.id
            and D(alloc.amount) == D("24")
            and s.scalar(select(Invoice).where(Invoice.number == "INV-2029-000007")).status
            == "paid"
        )
    assert (n(w, CommercialLedgerEntry), n(w, CreditGrant)) == before == (0, 0)


def test_partial_overpayment_and_multiple_payments_are_unsupported_and_block(w):
    for mutate, why in (
        (lambda d: d["payments"][0].update(amount=dec("10")), "partial"),
        (lambda d: d["payments"][0].update(amount=dec("30")), "over"),
        (
            lambda d: d["payments"].append(
                {**d["payments"][0], "source_id": 2, "external_id": "ch_002"}
            ),
            "double",
        ),
    ):
        plan = plan_of(w, edit(w.doc, mutate))
        inv = next(d for d in plan.by("invoices") if d.source_id == "1")
        assert inv.classification == "unsupported", why


def test_wallet_paid_legacy_invoice_is_preserved_without_wallet_replay(w):
    def f(d):
        i0 = d["invoices"][0]
        i0["paid_via"] = "wallet"
        d["payments"].clear()
        d["wallet_settlements"].append(
            {
                "invoice_source_id": 1,
                "ledger_entry_id": 77,
                "amount": i0["total"],
                "currency": "EUR",
                "created_at": "2029-12-20T09:00:00.000000+00:00",
            }
        )

    apply(w, edit(w.doc, f))
    with w.F() as s:
        pay = s.scalar(select(Payment))
        assert (pay.source, pay.external_reference, pay.status) == (
            "legacy_import",
            "wallet-ledger:77",
            "approved",
        ) and "NOT replayed" in pay.note
        assert s.scalar(select(func.count()).select_from(InvoicePaymentAllocation)) == 1
    assert (n(w, CommercialLedgerEntry), n(w, CreditGrant)) == (0, 0)


def test_wallet_paid_without_ledger_evidence_or_with_other_amount_blocks(w):
    d1 = edit(w.doc, lambda d: (d["invoices"][0].update(paid_via="wallet"), d["payments"].clear()))
    assert (
        next(d for d in plan_of(w, d1).by("invoices") if d.source_id == "1").classification
        == "requires_manual_review"
    )
    d2 = edit(
        d1,
        lambda d: d["wallet_settlements"].append(
            {
                "invoice_source_id": 1,
                "ledger_entry_id": 5,
                "amount": dec(1),
                "currency": "EUR",
                "created_at": "2029-12-20T09:00:00.000000+00:00",
            }
        ),
    )
    assert (
        next(d for d in plan_of(w, d2).by("invoices") if d.source_id == "1").classification
        == "unsupported"
    )


def test_invalid_arithmetic_bad_numbers_and_missing_mappings_are_classified_not_coerced(w):
    cases = {
        "invalid": lambda d: d["invoices"][1].update(total=dec("99")),
        "requires_manual_review": lambda d: d["invoices"][1].update(number="LEGACY-1"),
    }
    for expected, mutate in cases.items():
        plan = plan_of(w, edit(w.doc, mutate))
        assert next(x for x in plan.by("invoices") if x.source_id == "2").classification == expected
    plan = plan_of(w, edit(w.doc, lambda d: d["subscriptions"][0].update(enterprise_id=None)))
    assert next(x for x in plan.by("subscriptions")).classification == "invalid"
    plan = plan_of(w, edit(w.doc, lambda d: d["profiles"][0].update(country="A1")))
    assert next(x for x in plan.by("profiles")).classification == "invalid"


# =============================================================================================================
# sekuenca
# =============================================================================================================


def test_invoice_sequence_is_seeded_above_every_legacy_number_and_collisions_are_impossible(w):
    def f(
        d,
    ):  # një faturë e bllokuar me numër më të lartë (nuk importohet) NUK duhet të humbasë nga seed-i
        d["invoices"].append(
            invoice(
                4,
                "INV-2030-000015",
                w.e1,
                1,
                1,
                [line(6, "Standard - monthly fee", 1, 20, D("20.00"))],
                status="paid",
                paid_via="online",
                paid_at=EXPORT_AT,
            )
        )

    apply(w, edit(w.doc, f))
    with w.F() as s:
        assert {r.year: r.last_number for r in s.scalars(select(InvoiceNumberSequence))} == {
            2029: 7,
            2030: 15,
        }
        assert (
            billing.next_number(s, 2030) == "INV-2030-000016"
            and billing.next_number(s, 2029) == "INV-2029-000008"
        )
        assert (
            s.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "billing.sequence_seed")
            )
            == 2
        )


def test_seed_never_lowers_an_existing_sequence_and_is_race_safe_by_row_lock(w):
    with w.F() as s:
        s.add(InvoiceNumberSequence(year=2030, last_number=40))
        s.commit()
    apply(w)
    with w.F() as s:
        assert (
            s.scalar(
                select(InvoiceNumberSequence.last_number).where(InvoiceNumberSequence.year == 2030)
            )
            == 40
        )


def test_credit_note_sequence_is_audited_not_assumed_zero(w):
    assert plan_of(w).report()["notes"] and plan_of(w).blocking() == []
    plan = plan_of(w, edit(w.doc, lambda d: d["sequences"].update(credit_note_like=2)))
    assert [x.table for x in plan.blocking()] == ["credit_note_like"]


# =============================================================================================================
# baseline përdorimi + email i vonuar
# =============================================================================================================


def report(w, count, at, seq, watermark=None):
    doc = {"schema": bv.SCHEMA, "report_id": str(uuid.uuid4()), "report_seq": seq, "enterprise_id": str(w.e1), "product_id": str(w.email), "generated_at": bv.format_ts(at),
           "watermark": watermark if watermark is not None else count + 5, "cumulative_billable_count": count}  # fmt: skip
    return bv.BillingUsageReportV1.parse(doc)


def test_usage_opening_baseline_is_created_immutable_and_audited(w):
    apply(w)
    with w.F() as s:
        bl = s.scalar(select(BillingUsageBaseline))
        assert (
            (bl.cumulative_count, bl.watermark, bl.product_id) == (10, 14, w.email)
            and billing.utc(bl.boundary) == datetime(2030, 1, 15, 12, tzinfo=UTC)
            and len(bl.source_hash) == 64
        )
        bl.cumulative_count = 0
        with pytest.raises(
            billing.BillingImmutableError
            if hasattr(billing, "BillingImmutableError")
            else Exception
        ):
            s.flush()
        s.rollback()
        assert (
            s.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "billing.baseline_create")
            )
            == 1
        )


def test_baseline_with_incomplete_capture_requires_manual_review(w):
    d = edit(
        w.doc,
        lambda x: x["usage"][0].update(capture_active_since="2030-02-01T00:00:00.000000+00:00"),
    )
    assert [x.classification for x in plan_of(w, d).baselines] == ["requires_manual_review"]
    d2 = edit(w.doc, lambda x: x["usage"][0].update(capture_active_since=None))
    assert [x.classification for x in plan_of(w, d2).baselines] == ["requires_manual_review"]


def test_central_delta_starts_from_the_baseline_and_a_late_pre_cutover_email_is_billed_once(w):
    """Baseline 10 në kufi. Një email i krijuar para cutover-it por faturueshëm PAS tij hyn në numërues (13) ⇒ delta 3, saktësisht një herë; nuk groposet në baseline."""
    d = edit(w.doc, lambda x: x["plans"][0].update(included_emails=1))
    apply(w, d)
    boundary_end = datetime(2030, 2, 15, 12, tzinfo=UTC)
    with w.F() as s:
        billing_usage.ingest(s, report(w, 13, boundary_end + timedelta(minutes=1), 1), now=NOW)
        s.commit()
    with w.F() as s:
        sid = s.scalar(select(BillingSubscription.id))
        s.add(BillingAuthorityState(id=1, mode="central", ack=True))
        r = billing.process_period(s, sid, NOW)
        s.commit()
    assert r.kind == "invoiced"
    with w.F() as s:
        ls = list(
            s.scalars(
                select(InvoiceLine)
                .where(InvoiceLine.invoice_id == r.invoice.id)
                .order_by(InvoiceLine.line_no)
            )
        )
        assert [(ln.line_type, D(ln.quantity)) for ln in ls] == [
            ("monthly_fee", D(1)),
            ("email_overage", D(2)),
        ]  # delta 3 − included 1
        p = s.scalar(select(BillingPeriod).where(BillingPeriod.period_index == 2))
        assert (p.usage_from, p.usage_to, p.usage_from_report_id, p.provenance) == (
            10,
            13,
            None,
            "central",
        ) and p.usage_from_baseline_id is not None
        assert r.invoice.number == "INV-2030-000004"  # sekuenca e farëtuar: pa përplasje me legacy
    assert n(w, BillingPeriod) == 3


def test_without_a_baseline_the_first_central_period_waits_or_is_postponed_never_zero_based(w):
    apply(w, edit(w.doc, lambda x: x["usage"].clear()))
    with w.F() as s:
        sid = s.scalar(select(BillingSubscription.id))
        billing_usage.ingest(s, report(w, 13, datetime(2030, 2, 15, 12, 1, tzinfo=UTC), 1), now=NOW)
        r = billing.process_period(s, sid, NOW)
    assert r.kind == "postponed" and r.reason == "usage_baseline_missing"


# =============================================================================================================
# shadow
# =============================================================================================================


def prep_shadow(w, doc=None, cum=102):
    """Raporte rreth kufijve: baseline para periudhës 0, mbyllje e periudhës 0 dhe e periudhës 1 (cum = delta i periudhës 1)."""
    apply(w, doc)
    with w.F() as s:
        billing_usage.ingest(s, report(w, 0, datetime(2029, 11, 1, tzinfo=UTC), 1, 0), now=NOW)
        billing_usage.ingest(s, report(w, 0, datetime(2029, 12, 16, tzinfo=UTC), 2, 0), now=NOW)
        billing_usage.ingest(
            s, report(w, cum, datetime(2030, 1, 16, tzinfo=UTC), 3, cum + 6), now=NOW
        )
        s.commit()


def run_shadow(w, recent=3):
    with w.F() as s:
        rows = billing_shadow.run(s, NOW, recent=recent)
        s.commit()
        return {r.period_index: r.category for r in rows}


def test_shadow_reports_exact_when_central_would_issue_the_same_invoice(w):
    d = edit(w.doc, lambda x: x["usage"].clear())
    prep_shadow(w, d)
    res = run_shadow(w)
    assert res == {0: "exact", 1: "exact"}


def test_shadow_mismatch_categories(w):
    prep_shadow(w, cum=52)
    # usage: legacy faturoi 50 mbi 100 të përfshira; Central sheh delta 50 ⇒ 0 extra ⇒ usage_mismatch
    assert run_shadow(w)[1] == "usage_mismatch"
    with w.F() as s:
        assert s.scalar(select(func.count()).select_from(BillingShadowComparison)) == 2
    # rirunim pa ndryshim ⇒ s'shton krahasime
    assert run_shadow(w) == {}


def test_shadow_other_categories(w):
    # pricing_mismatch: çmimi legacy ≠ çmimi Central
    def pricing(d):
        d["invoices"][1] = invoice(
            2,
            "INV-2030-000003",
            w.e1,
            1,
            1,
            [
                line(2, "Standard - monthly fee", 1, 20, D("20.00")),
                line(3, "Email overage (2 above 100 included)", 2, "0.03", D("0.06")),
            ],
        )
        d["usage"].clear()

    d = edit(w.doc, pricing)
    prep_shadow(w, d)
    assert run_shadow(w)[1] == "pricing_mismatch"


def test_shadow_classify_unit_matrix():
    base_c = {"period_index": 1, "start": "S", "end": "E", "currency": "EUR", "monthly_fee": dec(20), "included_emails": 100, "lines": [{"type": "monthly_fee", "quantity": "1", "unit_price": dec(20), "amount": dec(20)}],
              "subtotal": dec(20), "vat_rate": dec("0.2"), "tax": dec(4), "total": dec(24), "decision": "invoice", "usage": {"state": "ok", "extra": 0}}  # fmt: skip
    base_l = {"present": True, "start": "S", "end": "E", "currency": "EUR", "monthly_fee": dec(20), "overage_quantity": None, "overage_unit_price": None, "included_emails": None,
              "subtotal": dec(20), "vat_rate": dec("0.2"), "tax": dec(4), "total": dec(24)}  # fmt: skip
    cl = billing_shadow.classify
    assert cl(base_c, base_l)[0] == "exact"
    assert cl(base_c, {**base_l, "end": "X"})[0] == "period_mismatch"
    assert cl(base_c, {**base_l, "currency": "USD"})[0] == "currency_mismatch"
    assert (
        cl(
            base_c,
            {
                **base_l,
                "monthly_fee": dec(25),
                "subtotal": dec(25),
                "tax": dec(5),
                "total": dec(30),
            },
        )[0]
        == "plan_mismatch"
    )
    assert (
        cl(base_c, {**base_l, "vat_rate": dec("0.1"), "tax": dec(2), "total": dec(22)})[0]
        == "tax_mismatch"
    )
    assert (
        cl(
            base_c,
            {
                **base_l,
                "overage_quantity": dec(5),
                "overage_unit_price": dec("0.02"),
                "subtotal": dec("20.1"),
                "total": dec("24.1"),
                "tax": dec("4.02"),
            },
        )[0]
        == "usage_mismatch"
    )
    assert (
        cl({**base_c, "usage": {"state": "waiting", "reason": "usage_report_missing"}}, base_l)[0]
        == "insufficient_usage"
    )
    assert (
        cl(
            {
                **base_c,
                "decision": "no_charge",
                "lines": [],
                "subtotal": dec(0),
                "tax": dec(0),
                "total": dec(0),
            },
            base_l,
        )[0]
        == "legacy_only"
    )
    assert cl(base_c, {"present": False})[0] == "central_only"
    assert cl({**base_c, "decision": "no_charge"}, {"present": False})[0] == "exact"
    assert cl(base_c, {**base_l, "total": dec(25), "subtotal": dec(21)})[0] == "amount_mismatch"
    cc = {
        **base_c,
        "lines": base_c["lines"]
        + [
            {
                "type": "email_overage",
                "quantity": "5",
                "unit_price": dec("0.02"),
                "amount": dec("0.1"),
            }
        ],
    }
    assert (
        cl(cc, {**base_l, "overage_quantity": dec(5), "overage_unit_price": dec("0.05")})[0]
        == "pricing_mismatch"
    )


def test_shadow_consumes_no_invoice_number_and_never_advances_the_cursor_or_periods(w):
    prep_shadow(w, edit(w.doc, lambda x: x["usage"].clear()))
    with w.F() as s:
        seq = {r.year: r.last_number for r in s.scalars(select(InvoiceNumberSequence))}
        cursor = s.scalar(select(BillingSubscription.next_period_index))
    counts = (n(w, Invoice), n(w, BillingPeriod), n(w, Payment))
    run_shadow(w)
    run_shadow(w, recent=5)
    with w.F() as s:
        assert {r.year: r.last_number for r in s.scalars(select(InvoiceNumberSequence))} == seq
        assert s.scalar(select(BillingSubscription.next_period_index)) == cursor
    assert (n(w, Invoice), n(w, BillingPeriod), n(w, Payment)) == counts


def test_shadow_projection_equals_the_real_period_processing(w):
    """Mbrojtje kundër drifteve: projeksioni shadow = ajo që `process_period` lëshon realisht."""
    apply(w, edit(w.doc, lambda x: x["plans"][0].update(included_emails=1)))
    end = datetime(2030, 2, 15, 12, 1, tzinfo=UTC)
    with w.F() as s:
        billing_usage.ingest(s, report(w, 14, end, 1), now=NOW)
        s.commit()
    with w.F() as s:
        sub = s.scalar(select(BillingSubscription))
        proj = billing_shadow.project(s, sub, sub.next_period_index)
        r = billing.process_period(s, sub.id, NOW)
        s.commit()
        assert (
            proj["decision"] == "invoice"
            and D(proj["total"]) == D(r.invoice.total)
            and D(proj["subtotal"]) == D(r.invoice.subtotal)
            and D(proj["tax"]) == D(r.invoice.tax)
        )


# =============================================================================================================
# autoritet, readiness, rollback, ACK
# =============================================================================================================


def test_local_mode_is_default_and_central_billing_run_refuses_until_central(w, capsys):
    with w.F() as s:
        assert billing_authority.mode(s) == "local"
    assert cli_run.main(["--json"], engine=w.eng) == 3
    capsys.readouterr()


def test_authority_transitions_require_shadow_ack_and_passing_readiness(w):
    with w.F() as s:
        a = U(s, w.a1)
        with pytest.raises(errors.Conflict):  # local → central direct
            billing_authority.set_mode(s, a, "central", ack=True, reason="skip shadow", now=NOW)
        billing_authority.set_mode(s, a, "shadow", ack=False, reason="start shadow", now=NOW)
        with pytest.raises(errors.Conflict):  # pa ACK
            billing_authority.set_mode(s, a, "central", ack=False, reason="no ack", now=NOW)
        with pytest.raises(errors.Conflict) as e:  # readiness FAIL (s'ka import)
            billing_authority.set_mode(s, a, "central", ack=True, reason="too early", now=NOW)
        assert "legacy_import_complete" in str(e.value)
        with pytest.raises(errors.Forbidden):
            billing_authority.set_mode(
                s, U(s, w.op), "shadow", ack=False, reason="operator", now=NOW
            )
        s.commit()
        assert billing_authority.mode(s) == "shadow"


def ready_world(w):
    """Import i pastër + shadow me raporte + konfigurim ⇒ gati për cutover."""
    d = edit(w.doc, lambda x: x["usage"].clear())
    d = edit(d, lambda x: x["usage"].append({"enterprise_id": str(w.e1), "product_id": str(w.email), "boundary": BOUNDARY, "cumulative_before_boundary": 0, "watermark_before_boundary": 0,
                                             "capture_active_since": "2029-06-01T00:00:00.000000+00:00", "events_total": 0}))  # fmt: skip
    w.doc = d
    prep_shadow(w, d)
    with w.F() as s:
        billing_usage.ingest(s, report(w, 102, NOW - timedelta(minutes=2), 4, 108), now=NOW)
        billing_authority.set_mode(
            s, U(s, w.a1), "shadow", ack=False, reason="start shadow", now=NOW
        )
        s.commit()
    run_shadow(w)
    settings.billing_worker_configured = True


def levels(w, **kw):
    with w.F() as s:
        return {c.name: c for c in billing_authority.readiness(s, NOW, **kw)}


def test_readiness_is_green_for_a_clean_prepared_cutover_and_flags_each_hard_gate(w, monkeypatch):
    monkeypatch.setattr(settings, "billing_worker_configured", True)
    ready_world(w)
    monkeypatch.setattr(settings, "billing_worker_configured", True)
    ok = levels(w, prod_ack=True)
    assert {k: c.level for k, c in ok.items() if c.level == "FAIL"} == {}, {
        k: c.reason for k, c in ok.items() if c.level != "PASS"
    }
    # secili gate hard
    monkeypatch.setattr(settings, "billing_worker_configured", False)
    assert levels(w, prod_ack=True)["central_billing_worker_configured"].level == "FAIL"
    monkeypatch.setattr(settings, "billing_worker_configured", True)
    assert levels(w, prod_ack=False)["cutover_ack_present"].level in ("WARN", "FAIL")
    with w.F() as s:
        s.add(
            BillingImportIssue(
                batch_id=s.scalar(select(BillingImportBatch.id)),
                source_table="invoices",
                source_id="9",
                classification="unsupported",
                reason="partial",
                created_at=NOW,
            )
        )
        s.commit()
    lv = levels(w, prod_ack=True)
    assert (
        lv["import_conflicts_resolved"].level == "FAIL"
        and lv["no_unresolved_partial_or_overpayment"].level == "FAIL"
    )


def test_readiness_fails_when_enterprise_was_not_frozen_at_export_and_without_baseline(
    w, monkeypatch
):
    monkeypatch.setattr(settings, "billing_worker_configured", True)
    apply(w, edit(w.doc, lambda x: (x["authority"].update(mode="shadow"), x["usage"].clear())))
    lv = levels(w, prod_ack=True)
    assert (
        lv["enterprise_billing_frozen"].level == "FAIL"
        and lv["no_dual_issuer"].level == "FAIL"
        and lv["usage_opening_baseline"].level == "FAIL"
    )


def test_readiness_flags_legacy_overage_without_a_central_price(w, monkeypatch):
    monkeypatch.setattr(settings, "billing_worker_configured", True)
    apply(
        w, base_doc(w.e2, w.email)
    )  # e2 s'ka caktim produkti/çmimi email Central, por plani legacy faturonte overage (0.5)
    assert levels(w, prod_ack=True)["legacy_overage_has_central_price"].level == "FAIL"


def test_central_enable_then_rollback_is_possible_before_the_first_central_invoice_only(
    w, monkeypatch
):
    ready_world(w)
    with w.F() as s:
        a = U(s, w.a1)
        billing_authority.set_mode(s, a, "central", ack=True, reason="cutover", now=NOW)
        s.commit()
        assert billing_authority.mode(s) == "central"
        with pytest.raises(errors.Conflict):  # rollback pa ACK
            billing_authority.set_mode(s, a, "shadow", ack=False, reason="oops", now=NOW)
        billing_authority.set_mode(
            s, a, "shadow", ack=True, reason="rollback before any central invoice", now=NOW
        )
        s.commit()
        assert billing_authority.mode(s) == "shadow"
        billing_authority.set_mode(s, a, "central", ack=True, reason="cutover again", now=NOW)
        sid = s.scalar(select(BillingSubscription.id))
        s.commit()
    with w.F() as s:
        r = billing.process_period(s, sid, NOW)
        s.commit()
        assert r.kind == "invoiced" and billing_authority.central_invoice_count(s) == 1
    with w.F() as s:
        with pytest.raises(errors.Conflict) as e:
            billing_authority.set_mode(
                s, U(s, w.a1), "shadow", ack=True, reason="late rollback", now=NOW
            )
        assert "rollback to Enterprise is blocked" in str(e.value)
        with pytest.raises(errors.Conflict):
            billing_authority.set_mode(
                s, U(s, w.a1), "local", ack=True, reason="late rollback", now=NOW
            )


def test_audit_covers_import_seed_baseline_authority_and_issue_resolution(w):
    plan = plan_of(w, edit(w.doc, lambda d: d["invoices"][0].update(paid_via="odd")))
    assert [x.classification for x in plan.blocking()] == ["requires_manual_review"]
    apply(w, edit(w.doc, lambda d: d["invoices"][0].update(paid_via="odd")))
    with w.F() as s:
        issue = billing_import.unresolved_issues(s)[0]
        with pytest.raises(errors.Invalid):
            billing_import.resolve_issue(s, U(s, w.a1), issue.id, "   ")
        s.rollback()
        issue = billing_import.unresolved_issues(s)[0]
        billing_import.resolve_issue(
            s, U(s, w.a1), issue.id, "settled manually off-system, evidence in ticket", now=NOW
        )
        s.commit()
        with pytest.raises(errors.Conflict):
            billing_import.resolve_issue(s, U(s, w.a1), issue.id, "again", now=NOW)
        billing_authority.set_mode(s, U(s, w.a1), "shadow", ack=False, reason="shadow", now=NOW)
        s.commit()
        actions = {r.action for r in s.scalars(select(AuditLog))}
        assert {
            "billing.import_apply",
            "billing.import_items",
            "billing.sequence_seed",
            "billing.baseline_create",
            "billing.import_issue_resolve",
            "billing.authority_change",
        } <= actions
        blob = json.dumps([r.detail for r in s.scalars(select(AuditLog))])
        assert "@" not in blob and "Rr. 1" not in blob


def test_cli_tools_import_authority_readiness_and_shadow(w, tmp_path, capsys):
    f = tmp_path / "a.json"
    f.write_text(json.dumps(w.doc))
    assert (
        cli_import.main(
            [
                "--artifact",
                str(f),
                "--apply",
                "--evidence-hash",
                "bad",
                "--actor-email",
                "a1@example.com",
            ],
            engine=w.eng,
        )
        == 1
    )
    out = capsys.readouterr()
    assert "hash" in out.err
    assert (
        cli_import.main(
            [
                "--artifact",
                str(f),
                "--apply",
                "--evidence-hash",
                w.doc["content_hash"],
                "--actor-email",
                "a1@example.com",
                "--json",
            ],
            engine=w.eng,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["export_id"] == w.doc["export_id"]
    assert (
        cli_import.main(
            [
                "--artifact",
                str(f),
                "--apply",
                "--evidence-hash",
                w.doc["content_hash"],
                "--actor-email",
                "a1@example.com",
            ],
            engine=w.eng,
        )
        == 0
    )  # idempotent
    capsys.readouterr()
    assert cli_import.main(["--artifact", str(tmp_path / "missing.json")], engine=w.eng) == 2
    assert (
        cli_auth.main(["status"], engine=w.eng) == 0
        and json.loads(capsys.readouterr().out)["mode"] == "local"
    )
    assert (
        cli_auth.main(
            [
                "set",
                "--mode",
                "central",
                "--reason",
                "x",
                "--actor-email",
                "a1@example.com",
                "--ack",
            ],
            engine=w.eng,
        )
        == 1
    )
    assert (
        cli_auth.main(
            ["set", "--mode", "shadow", "--reason", "start", "--actor-email", "a1@example.com"],
            engine=w.eng,
        )
        == 0
    )
    capsys.readouterr()
    assert (
        cli_ready.main(["--json"], engine=w.eng) == 1
        and json.loads(capsys.readouterr().out)["status"] == "FAIL"
    )
    assert cli_shadow.main(["--json"], engine=w.eng) == 0


# =============================================================================================================
# PostgreSQL
# =============================================================================================================


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_concurrent_apply_of_the_same_artifact_creates_one_batch_and_one_invoice_set(w):
    import threading

    if w.eng.dialect.name != "postgresql":
        pytest.skip("postgres parametrization only")
    barrier, out = threading.Barrier(4), []

    def go():
        try:
            barrier.wait()
            with w.F() as s:
                bt = billing_import.apply(s, w.doc, U(s, w.a1), w.doc["content_hash"], now=NOW)
                s.commit()
                out.append(("ok", str(bt.id)))
        except Exception as e:  # noqa: BLE001
            out.append(("err", type(e).__name__))

    ts_ = [threading.Thread(target=go) for _ in range(4)]
    [t.start() for t in ts_]
    [t.join() for t in ts_]
    assert (
        n(w, BillingImportBatch) == 1 and n(w, Invoice) == 2 and n(w, InvoicePaymentAllocation) == 1
    )
    assert any(r[0] == "ok" for r in out)


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_triggers_protect_import_evidence_baselines_comparisons_and_authority(w):
    from sqlalchemy.exc import DBAPIError

    if w.eng.dialect.name != "postgresql":
        pytest.skip("postgres parametrization only")
    prep_shadow(w, edit(w.doc, lambda x: x["usage"].clear()))
    run_shadow(w)
    with w.F() as s:
        s.add(BillingAuthorityState(id=1, mode="shadow", ack=False))
        s.commit()
    for sql in ("UPDATE billing_import_batches SET content_hash = 'x'", "DELETE FROM billing_import_batches", "TRUNCATE billing_import_items", "DELETE FROM billing_import_items",
                "UPDATE billing_import_items SET source_hash = 'x'", "UPDATE billing_shadow_comparisons SET category = 'exact'", "DELETE FROM billing_shadow_comparisons",
                "DELETE FROM billing_authority_state", "UPDATE invoices SET provenance = 'central'"):  # fmt: skip
        with pytest.raises(DBAPIError):
            with w.eng.begin() as c:
                c.execute(text(sql))
    with pytest.raises(DBAPIError):  # fatura e Central pa version plani
        with w.eng.begin() as c:
            c.execute(
                text("UPDATE invoices SET plan_version_id = NULL WHERE provenance = 'central'")
            )
            c.execute(text("INSERT INTO invoices (id) VALUES (gen_random_uuid())"))
