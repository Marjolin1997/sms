# ruff: noqa: F811
"""M9-g3 — shlyerja e faturave në Central: pagesa fature, alokim, paid, credit notes, rakordim/aging, readiness, API/RBAC, PG."""

import inspect
import json
import threading
import uuid
from datetime import timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from apps.central.core import errors
from apps.central.models import AuditLog
from apps.central.models.billing import BillingImmutableError, Invoice
from apps.central.models.money import (
    APPROVED,
    PENDING,
    REJECTED,
    CommercialLedgerEntry,
    CreditGrant,
    MoneyEvent,
    MoneyImmutableError,
    Payment,
)
from apps.central.models.settlement import CreditNote, InvoicePaymentAllocation
from apps.central.services import (
    billing,
    billing_readiness,
    credit_accounts,
    credit_notes,
    invoice_payments,
    payments,
    settlement_reports,
)
from apps.central.services import products as prod
from tests.test_central import IS_PG, central_alembic, make_db  # noqa: F401
from tests.test_central_auth import PW, auth_secret, bearer, mk, token_for  # noqa: F401
from tests.test_central_products import cdb  # noqa: F401
from tests.test_m9g1_billing import (  # noqa: F401
    AFTER1,
    U,
    api,
    b,
    mk_plan,
    process,
    profile,
    subscribe,
)

NOW = AFTER1 + timedelta(days=1)


def make_invoice(b, eid=None, fee="10", vat="0.2", code=None, cur="EUR"):
    """Faturë OPEN reale nga rrjedha g1 (tarifë mujore). → (invoice_id, total)."""
    if eid is None:  # çdo faturë me enterprise të ri (një abonim për enterprise)
        from apps.central.services import enterprises

        with b.F() as s:
            eid = enterprises.create(s, f"Co-{uuid.uuid4().hex[:6]}").id
            s.commit()
    profile(b, eid, vat=vat)
    vid = mk_plan(b, fee=fee, cur=cur, code=code or f"p{uuid.uuid4().hex[:6]}")
    sid = subscribe(b, vid, eid=eid)
    r = process(b, sid)
    assert r.invoice is not None
    return r.invoice.id, D(r.invoice.total)


def inv(b, iid):
    with b.F() as s:
        return s.get(Invoice, iid)


def pay(b, iid, amount, *, ref=None, by=None, currency=None, now=NOW):
    with b.F() as s:
        p = invoice_payments.create(
            s,
            iid,
            amount,
            actor=U(s, by or b.a1),
            external_reference=ref or f"ref-{uuid.uuid4().hex[:8]}",
            currency=currency,
            now=now,
        )
        s.commit()
        return p.id


def approve(b, pid, by=None, now=NOW):
    with b.F() as s:
        p = invoice_payments.approve(s, pid, U(s, by or b.a2), now=now)
        s.commit()
        return p.status


def count(b, model, *where):
    with b.F() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


# =============================================================================================================
# kredi: sjellja ekzistuese e pandryshuar
# =============================================================================================================


def test_credit_payment_behaviour_is_unchanged_and_requires_an_account(b):
    with b.F() as s:
        p = prod.create(s, "sms", "SMS", "sms")
        acct = credit_accounts.create(s, b.e1, p.id, "EUR", U(s, b.a1))
        c = payments.create(s, acct.id, "25", actor=U(s, b.a1), external_reference="credit-ref-1")
        assert (c.purpose, c.account_id, c.invoice_id) == ("credit", acct.id, None)
        payments.approve(s, c.id, U(s, b.a2))
        s.commit()
        assert (
            s.scalar(
                select(func.count())
                .select_from(CommercialLedgerEntry)
                .where(CommercialLedgerEntry.entry_type == "payment_credit")
            )
            == 1
        )
        assert [x.id for x in payments.list_payments(s)] == [
            c.id
        ]  # lista e kredisë s'përfshin pagesat e faturave
    iid, total = make_invoice(b)
    ip = pay(b, iid, str(total))
    with b.F() as s:
        assert payments.list_payments(s, status="pending") == []
        with pytest.raises(
            errors.NotFound
        ):  # pagesa e faturës s'miratohet/refuzohet nga rrjedha e kredisë
            payments.approve(s, ip, U(s, b.a2))
        s.rollback()
        with pytest.raises(errors.NotFound):
            payments.reject(s, ip, U(s, b.a2), "nope")


def test_purpose_shape_is_enforced_by_the_database(b):
    iid, total = make_invoice(b)
    with b.F() as s:
        i = s.get(Invoice, iid)
        base = dict(
            enterprise_id=i.enterprise_id,
            currency=i.currency,
            amount=total,
            source="manual",
            status="pending",
            created_by_id=b.a1,
        )
        for bad in (
            dict(purpose="invoice", invoice_id=None, account_id=None),  # faturë e munguar
            dict(purpose="invoice", invoice_id=iid, account_id=uuid.uuid4()),  # llogari e ndaluar
            dict(purpose="credit", invoice_id=None, account_id=None),  # llogari e munguar
            dict(purpose="credit", invoice_id=iid, account_id=uuid.uuid4()),
            dict(purpose="other", invoice_id=iid, account_id=None),
        ):
            with pytest.raises(IntegrityError):
                with s.begin_nested():
                    s.add(Payment(**base, **bad, created_at=NOW, updated_at=NOW))
                    s.flush()
            s.rollback()
            i = s.get(Invoice, iid)


# =============================================================================================================
# pagesa e faturës: validime
# =============================================================================================================


def test_invoice_payment_needs_exact_amount_currency_open_invoice_and_no_account(b):
    iid, total = make_invoice(b)
    with b.F() as s:
        a = U(s, b.a1)
        with pytest.raises(errors.Conflict):  # shumë e pasaktë (nënpagesë)
            invoice_payments.create(
                s, iid, str(total - D("0.01")), actor=a, external_reference="r1"
            )
        with pytest.raises(errors.Conflict):  # mbipagesë
            invoice_payments.create(
                s, iid, str(total + D("0.01")), actor=a, external_reference="r2"
            )
        with pytest.raises(errors.Conflict):  # monedhë tjetër
            invoice_payments.create(
                s, iid, str(total), actor=a, external_reference="r3", currency="USD"
            )
        for bad in (10.5, True, "-1", "0", None, D("NaN")):
            with pytest.raises(errors.Invalid):
                invoice_payments.create(s, iid, bad, actor=a, external_reference="r4")
        with pytest.raises(errors.NotFound):
            invoice_payments.create(s, uuid.uuid4(), str(total), actor=a, external_reference="r5")
        with pytest.raises(errors.Invalid):  # aktor: ose njeri ose sistem
            invoice_payments.create(s, iid, str(total), external_reference="r6")
        s.rollback()
    pid = pay(b, iid, str(total), ref="exact-1")
    with b.F() as s:
        p = invoice_payments.get(s, pid)
        assert (p.purpose, p.account_id, p.invoice_id, p.status, p.currency) == (
            "invoice",
            None,
            iid,
            "pending",
            "EUR",
        ) and D(p.amount) == total
        assert [
            x.action for x in s.scalars(select(AuditLog).where(AuditLog.resource_id == str(pid)))
        ] == ["payment.create"]


def test_duplicate_external_reference_is_idempotent_and_conflicting_reuse_is_rejected(b):
    iid, total = make_invoice(b)
    p1 = pay(b, iid, str(total), ref="dup-ref-1")
    p2 = pay(b, iid, str(total), ref="dup-ref-1")
    assert p1 == p2 and count(b, Payment, Payment.purpose == "invoice") == 1
    iid2, total2 = make_invoice(b)
    with b.F() as s:
        with pytest.raises(errors.Conflict):
            invoice_payments.create(
                s, iid2, str(total2), actor=U(s, b.a1), external_reference="dup-ref-1"
            )


# =============================================================================================================
# miratimi: alokim + paid, pa ledger/wallet
# =============================================================================================================


def test_approval_allocates_marks_paid_and_touches_no_ledger_grant_or_event(b):
    iid, total = make_invoice(b)
    pid = pay(b, iid, str(total))
    before = (count(b, CommercialLedgerEntry), count(b, CreditGrant), count(b, MoneyEvent))
    assert approve(b, pid) == "approved"
    assert (
        (count(b, CommercialLedgerEntry), count(b, CreditGrant), count(b, MoneyEvent))
        == before
        == (0, 0, 0)
    )
    with b.F() as s:
        i = s.get(Invoice, iid)
        a = s.scalar(select(InvoicePaymentAllocation))
        assert i.status == "paid" and i.paid_at is not None
        assert (a.payment_id, a.invoice_id, a.currency) == (pid, iid, "EUR") and D(
            a.amount
        ) == total == D(i.total)
        acts = [
            x.action
            for x in s.scalars(
                select(AuditLog)
                .where(AuditLog.action.in_(["payment.approve", "invoice.settle"]))
                .order_by(AuditLog.created_at)
            )
        ]
        assert sorted(acts) == ["invoice.settle", "payment.approve"]
        detail = json.dumps([x.detail for x in s.scalars(select(AuditLog))])
        assert "@" not in detail  # pa email/PII në audit


def test_maker_checker_and_idempotent_second_approval(b):
    iid, total = make_invoice(b)
    pid = pay(b, iid, str(total), by=b.a1)
    with b.F() as s:
        with pytest.raises(errors.Conflict):  # krijuesi s'e miraton
            invoice_payments.approve(s, pid, U(s, b.a1))
        s.rollback()
        with pytest.raises(errors.Forbidden):  # operatori s'miraton
            invoice_payments.approve(s, pid, U(s, b.op))
    assert approve(b, pid) == "approved"
    assert approve(b, pid) == "approved"  # no-op
    assert count(b, InvoicePaymentAllocation) == 1
    assert count(b, AuditLog, AuditLog.action == "invoice.settle") == 1


def test_rejected_void_and_paid_invoices_cannot_be_settled(b):
    # e refuzuar
    i1, t1 = make_invoice(b)
    p1 = pay(b, i1, str(t1))
    with b.F() as s:
        invoice_payments.reject(s, p1, U(s, b.a2), "wrong reference")
        s.commit()
    with b.F() as s:
        with pytest.raises(errors.Conflict):
            invoice_payments.approve(s, p1, U(s, b.a2))
        s.rollback()
        assert s.get(Invoice, i1).status == "open" and s.get(Payment, p1).status == REJECTED
    # faturë void
    i2, t2 = make_invoice(b)
    p2 = pay(b, i2, str(t2))
    with b.F() as s:
        billing.void_invoice(s, U(s, b.a1), i2, "issued by mistake")
        s.commit()
    with b.F() as s:
        with pytest.raises(errors.Conflict):
            invoice_payments.approve(s, p2, U(s, b.a2))
        with pytest.raises(errors.Conflict):  # as pagesë e re për faturë void
            invoice_payments.create(s, i2, str(t2), actor=U(s, b.a1), external_reference="late-1")
    # faturë e paguar: s'paguhet dy herë, s'anulohet
    i3, t3 = make_invoice(b, code="again")
    p3 = pay(b, i3, str(t3))
    approve(b, p3)
    with b.F() as s:
        with pytest.raises(errors.Conflict):
            invoice_payments.create(s, i3, str(t3), actor=U(s, b.a1), external_reference="second-1")
        with pytest.raises(errors.Conflict):
            billing.void_invoice(s, U(s, b.a1), i3, "too late")


def test_two_payments_for_the_same_invoice_only_one_settles(b):
    iid, total = make_invoice(b)
    p1, p2 = pay(b, iid, str(total), ref="first-ref"), pay(b, iid, str(total), ref="second-ref")
    approve(b, p1)
    with b.F() as s:
        with pytest.raises(errors.Conflict):
            invoice_payments.approve(s, p2, U(s, b.a2))
        s.rollback()
    assert (
        count(b, InvoicePaymentAllocation) == 1
        and count(b, Payment, Payment.status == APPROVED) == 1
    )
    with b.F() as s:
        assert s.get(Payment, p2).status == PENDING  # mbetet pending; mund të refuzohet nga stafi
        invoice_payments.reject(s, p2, U(s, b.a2), "duplicate payment")
        s.commit()


def test_allocation_and_approved_payment_are_immutable_and_never_deleted(b):
    iid, total = make_invoice(b)
    pid = pay(b, iid, str(total))
    approve(b, pid)
    with b.F() as s:
        a = s.scalar(select(InvoicePaymentAllocation))
        a.amount = D("1")
        with pytest.raises(BillingImmutableError):
            s.flush()
        s.rollback()
        s.delete(s.scalar(select(InvoicePaymentAllocation)))
        with pytest.raises(BillingImmutableError):
            s.flush()
        s.rollback()
        p = s.get(Payment, pid)
        p.amount = D("1")
        with pytest.raises(MoneyImmutableError):
            s.flush()
        s.rollback()
        p = s.get(Payment, pid)
        p.invoice_id = uuid.uuid4()
        with pytest.raises(MoneyImmutableError):
            s.flush()
        s.rollback()
        s.delete(s.get(Payment, pid))
        with pytest.raises(MoneyImmutableError):
            s.flush()
        s.rollback()
        with pytest.raises(errors.Conflict):  # e miratuar s'refuzohet
            invoice_payments.reject(s, pid, U(s, b.a2), "changed my mind")


def test_invoice_settlement_modules_have_no_wallet_ledger_or_network_dependencies(b):
    import ast

    forbidden = {
        "commercial_ledger",
        "credit_accounts",
        "grants",
        "wallet",
        "pay_from_wallet",
        "httpx",
        "requests",
        "urllib",
        "socket",
        "app",
    }
    for mod in (invoice_payments, credit_notes, settlement_reports):
        names = set()
        for node in ast.walk(ast.parse(inspect.getsource(mod))):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names} | {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names |= (
                    {(node.module or "").split(".")[0]}
                    | {a.name for a in node.names}
                    | {(node.module or "").split(".")[-1]}
                )
            elif isinstance(node, ast.Name | ast.Attribute):
                names.add(node.id if isinstance(node, ast.Name) else node.attr)
        assert not (forbidden & names), (mod.__name__, forbidden & names)


# =============================================================================================================
# credit notes
# =============================================================================================================


def paid_invoice(b, eid=None, fee="10", code=None):
    iid, total = make_invoice(b, eid=eid, fee=fee, code=code)
    approve(b, pay(b, iid, str(total)))
    return iid, total


def issue(b, iid, amount, reason="Service credit", key=None, by=None, currency=None, now=NOW):
    with b.F() as s:
        n = credit_notes.issue(
            s,
            U(s, by or b.a1),
            iid,
            amount,
            reason,
            key or f"key-{uuid.uuid4().hex[:10]}",
            currency=currency,
            now=now,
        )
        s.commit()
        return n.id


def test_credit_note_rules_reason_amount_currency_and_paid_only(b):
    iid, total = paid_invoice(b)
    with b.F() as s:
        a = U(s, b.a1)
        for bad_reason in ("", "   ", None, "x" * 501, "a\x00b"):
            with pytest.raises(errors.Invalid):
                credit_notes.issue(s, a, iid, "1", bad_reason, "key-aaaaaaaa")
        for bad_amount in ("0", "-1", 1.5, True, None, "abc"):
            with pytest.raises(errors.Invalid):
                credit_notes.issue(s, a, iid, bad_amount, "ok reason", "key-aaaaaaaa")
        with pytest.raises(errors.Invalid):
            credit_notes.issue(s, a, iid, "1", "ok reason", "short")
        with pytest.raises(errors.Conflict):  # monedhë tjetër
            credit_notes.issue(s, a, iid, "1", "ok reason", "key-aaaaaaaa", currency="USD")
        with pytest.raises(errors.Forbidden):  # vetëm admin
            credit_notes.issue(s, U(s, b.op), iid, "1", "ok reason", "key-aaaaaaaa")
        s.rollback()
    open_id, _ = make_invoice(b)
    with b.F() as s:
        with pytest.raises(errors.Conflict):  # faturë OPEN: void, jo credit note
            credit_notes.issue(s, U(s, b.a1), open_id, "1", "ok reason", "key-bbbbbbbb")
    nid = issue(b, iid, "1.50", "Goodwill")
    with b.F() as s:
        n = credit_notes.get(s, nid)
        assert (n.currency, D(n.amount), n.reason, n.invoice_id) == (
            "EUR",
            D("1.5"),
            "Goodwill",
            iid,
        )
        assert n.number.startswith("CN-") and n.issuer and n.bill_to
        assert s.get(Invoice, iid).status == "paid"  # fatura s'ndryshon


def test_cumulative_credit_cannot_exceed_total_and_idempotency(b):
    iid, total = paid_invoice(b)
    half = (total / 2).quantize(D("0.01"))
    issue(b, iid, str(half), key="credit-key-1")
    assert issue(b, iid, str(half), key="credit-key-1") == issue(
        b, iid, str(half), key="credit-key-1"
    )  # idempotent
    with b.F() as s:
        with pytest.raises(errors.Conflict):  # i njëjti çelës, kërkesë tjetër
            credit_notes.issue(s, U(s, b.a1), iid, str(half), "different reason", "credit-key-1")
        s.rollback()
        rest = total - half
        with pytest.raises(errors.Conflict):  # tejkalon totalin
            credit_notes.issue(
                s, U(s, b.a1), iid, str(rest + D("0.01")), "too much", "credit-key-2"
            )
        s.rollback()
        credit_notes.issue(s, U(s, b.a1), iid, str(rest), "exactly the rest", "credit-key-3")
        s.commit()
        assert credit_notes.credited_total(s, iid) == total
        with pytest.raises(errors.Conflict):
            credit_notes.issue(s, U(s, b.a1), iid, "0.01", "no more", "credit-key-4")


def test_credit_note_numbering_is_sequential_without_gaps_and_rollback_returns_the_number(b):
    iid, _ = paid_invoice(b, fee="100")
    n1, n2 = issue(b, iid, "1"), issue(b, iid, "1")
    with b.F() as s:
        s2 = credit_notes.issue(s, U(s, b.a1), iid, "1", "rolled back", "key-rollback-1", now=NOW)
        s.rollback()  # numri s'konsumohet
        assert s2.number
    n3 = issue(b, iid, "1")
    with b.F() as s:
        nums = [credit_notes.get(s, i).number for i in (n1, n2, n3)]
    assert nums == [f"CN-{NOW.year}-{k:06d}" for k in (1, 2, 3)]


def test_credit_note_is_immutable_issues_no_refund_and_touches_no_ledger(b):
    iid, total = paid_invoice(b)
    before = (
        count(b, CommercialLedgerEntry),
        count(b, CreditGrant),
        count(b, MoneyEvent),
        count(b, Payment),
    )
    nid = issue(b, iid, "2")
    assert (
        count(b, CommercialLedgerEntry),
        count(b, CreditGrant),
        count(b, MoneyEvent),
        count(b, Payment),
    ) == before  # pa rifund/pagesë/wallet
    with b.F() as s:
        n = s.get(CreditNote, nid)
        n.amount = D("1")
        with pytest.raises(BillingImmutableError):
            s.flush()
        s.rollback()
        s.delete(s.get(CreditNote, nid))
        with pytest.raises(BillingImmutableError):
            s.flush()
        s.rollback()
        row = s.scalar(select(AuditLog).where(AuditLog.action == "credit_note.issue"))
        assert row is not None and "@" not in json.dumps(row.detail)


# =============================================================================================================
# rakordim, aging, readiness
# =============================================================================================================


def test_aging_buckets_and_summary_counts(b):
    iid, total = make_invoice(b)
    with b.F() as s:
        due = billing.utc(s.get(Invoice, iid).due_at)
    assert settlement_reports.bucket(due - timedelta(days=1), due) == "current"
    for days, exp in (
        (1, "1-30"),
        (30, "1-30"),
        (31, "31-60"),
        (60, "31-60"),
        (61, "61-90"),
        (90, "61-90"),
        (91, "90+"),
    ):
        assert settlement_reports.bucket(due + timedelta(days=days), due) == exp
    with b.F() as s:
        st = settlement_reports.summary(s, due + timedelta(days=45))
        assert st["invoices"]["open"] == 1 and st["invoices"]["overdue_open"] == 1
        assert (
            st["aging"]["EUR"]["31-60"]["count"] == 1
            and D(st["aging"]["EUR"]["31-60"]["amount"]) == total
        )
        assert all(not v for v in st["anomalies"].values())


def test_readiness_is_green_when_consistent_and_warns_on_overdue_and_stale(b):
    iid, total = make_invoice(b)
    pay(b, iid, str(total))
    with b.F() as s:
        due = billing.utc(s.get(Invoice, iid).due_at)
        items = {c.name: c for c in billing_readiness.checks(s, due + timedelta(days=10))}
        assert items["billing_settlement_integrity"].level == "PASS"
        assert (
            items["billing_invoices_not_overdue"].level == "WARN"
            and items["billing_invoice_payments_not_stale"].level == "WARN"
        )
    with b.F() as s:
        items = {c.name: c for c in billing_readiness.checks(s, NOW)}
        assert items["billing_settlement_integrity"].level == "PASS"


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_database_refuses_paid_without_allocation_and_unbalanced_settlement(b):
    if b.eng.dialect.name != "postgresql":
        pytest.skip("postgres parametrization only")
    iid, total = make_invoice(b)
    pid = pay(b, iid, str(total))
    # paid pa alokim: refuzohet në commit (constraint trigger i shtyrë)
    with pytest.raises(DBAPIError):
        with b.eng.begin() as c:
            c.execute(
                text("UPDATE invoices SET status = 'paid', paid_at = now() WHERE id = :i"),
                {"i": iid},
            )
    # alokim pa faturë të paguar: refuzohet
    with pytest.raises(DBAPIError):
        with b.eng.begin() as c:
            c.execute(
                text(
                    "UPDATE payments SET status='approved', approved_at=now(), approved_by_id=:u WHERE id=:p"
                ),
                {"u": b.a2, "p": pid},
            )
    # pagesa e miratuar pa alokim: refuzohet
    with pytest.raises(DBAPIError):
        with b.eng.begin() as c:
            c.execute(
                text(
                    "UPDATE payments SET status='approved', approved_at=now(), approved_by_id=:u WHERE id=:p"
                ),
                {"u": b.a2, "p": pid},
            )
    with b.F() as s:
        assert s.get(Invoice, iid).status == "open" and s.get(Payment, pid).status == "pending"
    approve(b, pid)
    for sql in (
        "UPDATE invoice_payment_allocations SET amount = amount",
        "DELETE FROM invoice_payment_allocations",
        "TRUNCATE invoice_payment_allocations",
        "DELETE FROM payments",
        "UPDATE payments SET purpose = 'credit'",
        "UPDATE invoices SET status = 'open'",
    ):
        with pytest.raises(DBAPIError):
            with b.eng.begin() as c:
                c.execute(text(sql))
    nid = issue(b, iid, "1")
    for sql in (
        "UPDATE credit_notes SET amount = 5",
        "DELETE FROM credit_notes",
        "TRUNCATE credit_notes",
        "DELETE FROM credit_note_sequence",
        "TRUNCATE credit_note_sequence",
        "UPDATE credit_note_sequence SET last_number = 0",
    ):
        with pytest.raises(DBAPIError):
            with b.eng.begin() as c:
                c.execute(text(sql))
    # Σ credit notes > total: refuzohet nga DB edhe pa shërbimin
    with pytest.raises(DBAPIError):
        with b.eng.begin() as c:
            c.execute(
                text(
                    "INSERT INTO credit_notes (id, number, enterprise_id, invoice_id, currency, amount, reason, idempotency_key, request_hash, issuer, bill_to, issued_at, created_by_id, created_at) "
                    "SELECT gen_random_uuid(), 'CN-9999-000001', enterprise_id, id, currency, total, 'x', 'raw-key-0001', 'h', '{}', '{}', now(), :u, now() FROM invoices WHERE id = :i"
                ),
                {"u": b.a1, "i": iid},
            )
    assert nid


# =============================================================================================================
# PostgreSQL: konkurrencë
# =============================================================================================================


def _race(fns):
    barrier, out = threading.Barrier(len(fns)), []

    def run(fn):
        try:
            barrier.wait()
            out.append(("ok", fn()))
        except Exception as e:  # noqa: BLE001
            out.append(("err", type(e).__name__))

    ts = [threading.Thread(target=run, args=(f,)) for f in fns]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_races_double_approval_two_payments_void_and_credit_notes(b):
    if b.eng.dialect.name != "postgresql":
        pytest.skip("postgres parametrization only")

    def approver(pid, by):
        def go():
            with b.F() as s:
                invoice_payments.approve(s, pid, U(s, by), now=NOW)
                s.commit()
            return "approved"

        return go

    # 1. dy miratime të së njëjtës pagesë ⇒ një alokim
    i1, t1 = make_invoice(b)
    p1 = pay(b, i1, str(t1))
    res = _race([approver(p1, b.a2), approver(p1, b.a2), approver(p1, b.a2)])
    assert (
        all(r[0] == "ok" for r in res)
        and count(b, InvoicePaymentAllocation, InvoicePaymentAllocation.payment_id == p1) == 1
    )
    assert count(b, AuditLog, AuditLog.action == "invoice.settle") == 1
    # 2. dy pagesa për të njëjtën faturë ⇒ vetëm një shlyen
    i2, t2 = make_invoice(b, code="race2")
    pa, pb = pay(b, i2, str(t2), ref="race-a"), pay(b, i2, str(t2), ref="race-b")
    res = _race([approver(pa, b.a2), approver(pb, b.a2)])
    assert sorted(r[0] for r in res) == ["err", "ok"]
    assert count(b, InvoicePaymentAllocation, InvoicePaymentAllocation.invoice_id == i2) == 1
    # 3. pagesë kundrejt void: një rezultat terminal i vlefshëm
    i3, t3 = make_invoice(b, code="race3")
    p3 = pay(b, i3, str(t3))

    def voider():
        with b.F() as s:
            billing.void_invoice(s, U(s, b.a1), i3, "void race")
            s.commit()
        return "void"

    res = _race([approver(p3, b.a2), voider])
    assert sorted(r[0] for r in res) == ["err", "ok"]
    with b.F() as s:
        st = s.get(Invoice, i3).status
        assert st in ("paid", "void")
        assert (
            s.scalar(
                select(func.count())
                .select_from(InvoicePaymentAllocation)
                .where(InvoicePaymentAllocation.invoice_id == i3)
            )
            == 1
        ) == (st == "paid")
    # 4. credit notes paralele ⇒ Σ ≤ total
    i4, t4 = paid_invoice(b, fee="40", code="race4")
    third = (t4 / 3 + D("0.01")).quantize(D("0.01"))

    def noter(k):
        def go():
            with b.F() as s:
                credit_notes.issue(
                    s, U(s, b.a1), i4, str(third), "parallel", f"par-key-{k}", now=NOW
                )
                s.commit()
            return k

        return go

    res = _race([noter(k) for k in range(5)])
    ok = [r for r in res if r[0] == "ok"]
    with b.F() as s:
        assert credit_notes.credited_total(s, i4) <= D(t4) and len(ok) == int(D(t4) // third)
        nums = [n.number for n in credit_notes.list_notes(s, invoice_id=i4)]
        assert len(nums) == len(set(nums))


# =============================================================================================================
# invariantë të thyer (zbulim nga readiness; SQLite s'ka trigger-a, prandaj mund t'i simulojmë me SQL të papërpunuar)
# =============================================================================================================


def test_readiness_detects_broken_settlement_invariants(b):
    if b.eng.dialect.name == "postgresql":
        pytest.skip("PostgreSQL refuses these states at commit (see the trigger test)")
    # 1. paid pa alokim
    i1, t1 = make_invoice(b)
    with b.eng.begin() as c:
        c.execute(
            text("UPDATE invoices SET status='paid', paid_at=:t WHERE id=:i"),
            {"i": str(i1).replace("-", ""), "t": NOW},
        )
    # 2. pagesë fature e miratuar pa alokim
    i2, t2 = make_invoice(b)
    p2 = pay(b, i2, str(t2))
    with b.eng.begin() as c:
        c.execute(text("UPDATE payments SET status='approved', approved_at=:t, approved_by_id=:u WHERE id=:p"),
                  {"p": str(p2).replace("-", ""), "u": str(b.a2).replace("-", ""), "t": NOW})  # fmt: skip
    # 3. credit notes mbi total (e futur drejtpërdrejt)
    i3, t3 = paid_invoice(b)
    with b.eng.begin() as c:
        c.execute(
            text("INSERT INTO credit_notes (id, number, enterprise_id, invoice_id, currency, amount, reason, idempotency_key, request_hash, issuer, bill_to, issued_at, created_by_id, created_at) "
                 "SELECT :n, 'CN-9999-000001', enterprise_id, id, currency, total * 2, 'x', 'raw-key-0001', 'h', '{}', '{}', :t, :u, :t FROM invoices WHERE id = :i"),
            {"n": uuid.uuid4().hex, "i": str(i3).replace("-", ""), "u": str(b.a1).replace("-", ""), "t": NOW},
        )  # fmt: skip
    with b.F() as s:
        st = settlement_reports.anomalies(s)
        assert st["paid_invoice_without_allocation"] == [str(i1)]
        assert st["approved_payment_without_allocation"] == [str(p2)]
        assert st["credit_notes_over_total"] == [str(i3)]
        items = {c.name: c for c in billing_readiness.checks(s, NOW)}
        c = items["billing_settlement_integrity"]
        assert (
            c.level == "FAIL"
            and "paid_invoice_without_allocation=1" in c.reason
            and "credit_notes_over_total=1" in c.reason
        )


# =============================================================================================================
# API + RBAC
# =============================================================================================================


def _hdr(api, email, role):
    mk(api.b.eng, email, role=role)
    return bearer(token_for(api, email))


def test_api_flow_rbac_strict_inputs_and_no_delete(api):
    b = api.b
    iid, total = make_invoice(b)
    ad = api.h["ad"]
    ro = api.h["ro"]
    ad2 = _hdr(api, "ad2@example.com", "admin")
    base = "/admin/billing"
    body = {"invoice_id": str(iid), "amount": str(total), "external_reference": "bank-ref-0001"}
    # RBAC
    assert api.post(f"{base}/invoice-payments", json=body, headers=ro).status_code == 403
    assert api.post(f"{base}/invoice-payments", json=body).status_code == 401
    # hyrje strikte
    for bad in ({**body, "amount": 12.0}, {**body, "amount": "1e2"}, {**body, "extra": 1}, {k: v for k, v in body.items() if k != "external_reference"},
                {**body, "currency": "eur"}, {**body, "invoice_id": "nope"}, {**body, "amount": "-5"}):  # fmt: skip
        assert api.post(f"{base}/invoice-payments", json=bad, headers=ad).status_code == 422, bad
    wrong = api.post(
        f"{base}/invoice-payments",
        json={**body, "amount": "1.00", "external_reference": "bank-ref-0002"},
        headers=ad,
    )
    assert wrong.status_code == 409  # shuma e pasaktë
    created = api.post(f"{base}/invoice-payments", json=body, headers=ad)
    assert (
        created.status_code == 201
        and created.json()["purpose"] == "invoice"
        and created.json()["status"] == "pending"
    )
    pid = created.json()["id"]
    again = api.post(f"{base}/invoice-payments", json=body, headers=ad)
    assert again.status_code == 201 and again.json()["id"] == pid  # idempotent
    # maker-checker + RBAC në miratim
    assert api.post(f"{base}/invoice-payments/{pid}/approve", headers=ro).status_code == 403
    assert (
        api.post(f"{base}/invoice-payments/{pid}/approve", headers=ad).status_code == 409
    )  # krijuesi
    ok = api.post(f"{base}/invoice-payments/{pid}/approve", headers=ad2)
    assert (
        ok.status_code == 200 and ok.json()["status"] == "approved" and ok.json()["allocation_id"]
    )
    assert (
        api.post(f"{base}/invoice-payments/{pid}/approve", headers=ad2).status_code == 200
    )  # idempotent
    # lexim (operator)
    assert (
        api.get(f"{base}/invoice-payments?invoice_id={iid}", headers=ro).json()["items"][0]["id"]
        == pid
    )
    assert (
        api.get(f"{base}/invoice-payments/{pid}", headers=ro).json()["allocation_id"]
        == ok.json()["allocation_id"]
    )
    al = api.get(f"{base}/allocations", headers=ro).json()["items"]
    assert len(al) == 1 and al[0]["payment_id"] == pid and al[0]["amount"] == f"{total:.6f}"
    assert api.get(f"{base}/allocations/{al[0]['id']}", headers=ro).status_code == 200
    # faturë: detaj me shlyerjen + credit note
    assert (
        api.post(
            f"{base}/credit-notes",
            json={
                "invoice_id": str(iid),
                "amount": "1",
                "reason": "r",
                "idempotency_key": "cn-key-0001",
            },
            headers=ro,
        ).status_code
        == 403
    )
    for bad in ({"invoice_id": str(iid), "amount": "1", "reason": "", "idempotency_key": "cn-key-0001"},
                {"invoice_id": str(iid), "amount": 1, "reason": "r", "idempotency_key": "cn-key-0001"},
                {"invoice_id": str(iid), "amount": "1", "reason": "r", "idempotency_key": "x"},
                {"invoice_id": str(iid), "amount": "1", "reason": "r", "idempotency_key": "cn-key-0001", "extra": 1}):  # fmt: skip
        assert api.post(f"{base}/credit-notes", json=bad, headers=ad).status_code == 422, bad
    cn = api.post(
        f"{base}/credit-notes",
        json={
            "invoice_id": str(iid),
            "amount": "2.5",
            "reason": "Goodwill credit",
            "idempotency_key": "cn-key-0001",
        },
        headers=ad,
    )
    assert cn.status_code == 201 and cn.json()["number"].startswith("CN-")
    over = api.post(
        f"{base}/credit-notes",
        json={
            "invoice_id": str(iid),
            "amount": str(total),
            "reason": "too much",
            "idempotency_key": "cn-key-0002",
        },
        headers=ad,
    )
    assert over.status_code == 409
    detail = api.get(f"{base}/invoices/{iid}", headers=ro).json()
    st = detail["settlement"]
    assert (
        detail["status"] == "paid"
        and st["allocation"]["payment_id"] == pid
        and st["credited_total"] == "2.500000"
    )
    assert D(st["net_amount"]) == total - D("2.5") and [
        n["number"] for n in st["credit_notes"]
    ] == [cn.json()["number"]]
    assert (
        api.get(f"{base}/credit-notes?invoice_id={iid}", headers=ro).json()["items"][0]["id"]
        == cn.json()["id"]
    )
    assert api.get(f"{base}/credit-notes/{cn.json()['id']}", headers=ro).status_code == 200
    summ = api.get(f"{base}/settlement", headers=ro).json()
    assert summ["invoices"]["paid"] == 1 and all(not v for v in summ["anomalies"].values())
    # pagesa e faturës nuk del nga API-ja e kredisë dhe s'miratohet aty
    assert api.get("/admin/money/payments", headers=ro).json()["items"] == []
    assert api.post(f"/admin/money/payments/{pid}/approve", headers=ad2).status_code == 404
    # asnjë DELETE/PUT/PATCH
    rs = [
        (m, p)
        for p, ops in api.app.openapi()["paths"].items()
        if p.startswith(base)
        for m in ops
        if "invoice-payments" in p
        or "credit-notes" in p
        or "allocations" in p
        or p.endswith("/settlement")
    ]
    assert rs and {m for m, _ in rs} <= {"get", "post"}
    assert not [r for r in rs if r[0] == "post" and "/allocations" in r[1]]  # alokimet vetëm-lexim


def test_api_reject_flow_and_audit_has_no_secrets(api):
    b = api.b
    iid, total = make_invoice(b)
    ad, ad2 = api.h["ad"], _hdr(api, "ad3@example.com", "admin")
    pid = api.post(
        "/admin/billing/invoice-payments",
        json={"invoice_id": str(iid), "amount": str(total), "external_reference": "bank-ref-rj01"},
        headers=ad,
    ).json()["id"]
    assert (
        api.post(
            f"/admin/billing/invoice-payments/{pid}/reject", json={"reason": ""}, headers=ad2
        ).status_code
        == 422
    )
    r = api.post(
        f"/admin/billing/invoice-payments/{pid}/reject",
        json={"reason": "bank said no"},
        headers=ad2,
    )
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert (
        api.post(f"/admin/billing/invoice-payments/{pid}/approve", headers=ad2).status_code == 409
    )
    with b.F() as s:
        blob = json.dumps(
            [x.detail for x in s.scalars(select(AuditLog).where(AuditLog.action.like("payment.%")))]
        )
        assert "@" not in blob and "password" not in blob.lower()
    assert api.get(f"/admin/billing/invoices/{iid}", headers=api.h["ro"]).json()["status"] == "open"
