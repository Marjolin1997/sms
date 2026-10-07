# ruff: noqa: F811
"""M9-b — autoriteti tregtar i parave në Central: llogari, ledger, pagesa, grant-e, ngjarje. Pa Enterprise."""

import ast
import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

from apps.central.core import errors
from apps.central.models import (
    AuditLog,
    CentralUser,
    CommercialLedgerEntry,
    CreditAccount,
    CreditGrant,
    MoneyEvent,
    Payment,
)
from apps.central.models.money import MoneyImmutableError
from apps.central.services import commercial_ledger as ledger
from apps.central.services import credit_accounts as accts
from apps.central.services import enterprises, grants, money_common, money_sequence, payments, users
from apps.central.services import products as prod_svc
from tests.test_central import IS_PG, ROOT, make_db  # noqa: F401
from tests.test_central_auth import PW
from tests.test_central_products import cdb  # noqa: F401  (fixtures)

T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)
_PG = pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")


def PGONLY(fn):  # noqa: N802  (marker + flamur që fixture-i `m` e përdor për parametrin sqlite)
    fn._pgonly = True
    return _PG(fn)


@pytest.fixture
def m(cdb, request):
    url, eng = cdb
    if getattr(request.function, "_pgonly", False) and url.startswith("sqlite"):
        pytest.skip("needs PostgreSQL locks/triggers")
    F = sessionmaker(bind=eng, expire_on_commit=False)
    with F() as s:
        ent = enterprises.create(s, "Acme").id
        sms = prod_svc.create(s, "sms", "SMS", "sms").id
        email = prod_svc.create(s, "email", "Email", "email").id
        a1 = users.create_user(s, "a1@example.com", PW, "admin").id
        a2 = users.create_user(s, "a2@example.com", PW, "admin").id
        op = users.create_user(s, "op@example.com", PW, "operator").id
        s.commit()
    ns = SimpleNamespace(F=F, eng=eng, url=url, ent=ent, sms=sms, email=email, a1=a1, a2=a2, op=op)
    return ns


def U(s, id_):
    return s.get(CentralUser, id_)


def new_account(m, cur="EUR", product=None):
    with m.F() as s:
        a = accts.create(s, m.ent, product or m.sms, cur, U(s, m.a1))
        s.commit()
        return a.id


def fund(m, account_id, amount="100", ref=None):
    """Pagesë e krijuar nga sistemi dhe e miratuar nga admin njeri ⇒ fonde tregtare."""
    with m.F() as s:
        p = payments.create(
            s, account_id, amount, system="system:payment_import", external_reference=ref
        )
        payments.approve(s, p.id, U(s, m.a1))
        s.commit()
        return p.id


def tot(m, account_id):
    with m.F() as s:
        return ledger.totals(s, account_id)


def grant(m, account_id, amount, key="grant-key-0001", **kw):
    with m.F() as s:
        g = grants.issue(s, account_id, amount, idempotency_key=key, actor=U(s, m.a1), **kw)
        s.commit()
        return g.id


def count(m, model, *where):
    with m.F() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def audit_actions(m, like):
    with m.F() as s:
        return list(
            s.scalars(
                select(AuditLog).where(AuditLog.action.like(like)).order_by(AuditLog.created_at)
            )
        )


# --- llogaria + monedha --------------------------------------------------------------------------------


def test_create_account_is_audited_by_a_human_and_has_no_stored_balance(m):
    aid = new_account(m)
    with m.F() as s:
        a = s.get(CreditAccount, aid)
        assert (a.enterprise_id, a.product_id, a.currency, a.status) == (
            m.ent,
            m.sms,
            "EUR",
            "active",
        )
    (au,) = audit_actions(m, "credit_account.create")
    assert (au.actor_kind, au.actor_id) == ("user", m.a1)
    # asnjë kolonë balance e ndryshueshme në asnjë tabelë parash
    for t in (
        "credit_accounts",
        "payments",
        "credit_grants",
        "commercial_ledger_entries",
        "money_events",
    ):
        cols = {c["name"] for c in inspect(m.eng).get_columns(t)}
        assert not {c for c in cols if "balance" in c}, t


def test_duplicate_account_is_idempotent_and_a_second_currency_is_blocked_by_the_db(m):
    a = new_account(m)
    assert new_account(m) == a  # e njëjta (enterprise, produkt, monedhë)
    with m.F() as s:
        with pytest.raises(errors.Conflict):
            accts.create(s, m.ent, m.sms, "USD", U(s, m.a1))  # shërbimi
        s.rollback()
        s.add(CreditAccount(enterprise_id=m.ent, product_id=m.sms, currency="USD"))
        with pytest.raises(IntegrityError):  # DB: UNIQUE(enterprise_id, product_id)
            s.flush()
        s.rollback()
    assert new_account(m, "USD", m.email) != a  # produkt tjetër: monedhë tjetër lejohet
    assert len(audit_actions(m, "credit_account.create")) == 2


@pytest.mark.parametrize("bad", ["", "EU", "EURO", "e1r", "12 ", None, 5])
def test_currency_is_a_three_letter_code(m, bad):
    with m.F() as s:
        with pytest.raises(errors.Invalid):
            accts.create(s, m.ent, m.sms, bad, U(s, m.a1))
    with m.F() as s:  # edhe DB-ja: CHECK portativ
        s.add(CreditAccount(enterprise_id=m.ent, product_id=m.sms, currency="eu"))
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()


def test_currency_normalizes_to_uppercase(m):
    with m.F() as s:
        a = accts.create(s, m.ent, m.sms, " all ", U(s, m.a1))
        assert a.currency == "ALL"
        s.commit()


def test_currency_and_scope_of_an_account_are_immutable(m):
    aid = new_account(m)
    fund(m, aid)
    with m.F() as s:
        a = s.get(CreditAccount, aid)
        a.currency = "USD"
        with pytest.raises(MoneyImmutableError):
            s.flush()
        s.rollback()
        a = s.get(CreditAccount, aid)
        a.product_id = m.email
        with pytest.raises(MoneyImmutableError):
            s.flush()
        s.rollback()


@PGONLY
def test_pg_currency_cannot_change_even_with_raw_sql(m):
    aid = new_account(m)
    fund(m, aid)
    with m.F() as s:
        with pytest.raises(DBAPIError):
            s.execute(text("update credit_accounts set currency = 'USD' where id = :i"), {"i": aid})
        s.rollback()
        with pytest.raises(DBAPIError):
            s.execute(text("delete from credit_accounts where id = :i"), {"i": aid})
        s.rollback()


def test_suspended_account_blocks_new_money_but_allows_reversal_and_is_audited(m):
    aid = new_account(m)
    fund(m, aid, "100")
    gid = grant(m, aid, "10")
    with m.F() as s:
        accts.set_status(s, aid, "suspended", U(s, m.a1), "compliance hold")
        assert (
            accts.set_status(s, aid, "suspended", U(s, m.a1), "again").status == "suspended"
        )  # no-op
        s.commit()
    with m.F() as s:
        with pytest.raises(errors.Conflict):
            grants.issue(s, aid, "1", idempotency_key="grant-key-0002", actor=U(s, m.a1))
        s.rollback()
        with pytest.raises(errors.Conflict):
            payments.create(s, aid, "1", actor=U(s, m.a1))
        s.rollback()
        with pytest.raises(errors.Conflict):
            accts.adjust(s, aid, "credit", "1", "x", U(s, m.a1), idempotency_key="adj-key-00001")
        s.rollback()
        grants.reverse(s, gid, U(s, m.a1), "undo")  # lejohet
        s.commit()
        with pytest.raises(errors.Invalid):
            accts.set_status(s, aid, "bogus", U(s, m.a1), "x")
        with pytest.raises(errors.Invalid):
            accts.set_status(s, aid, "active", U(s, m.a1), "")
    assert [a.detail["to"] for a in audit_actions(m, "credit_account.status_change")] == [
        "suspended"
    ]


# --- Decimal ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", [1.5, True, "abc", "NaN", "Infinity", "0", "-1", "1.1234567", D("0.0000001"), None, [1]]
)
def test_money_validation_rejects_floats_bools_nan_and_excess_precision(bad):
    with pytest.raises(errors.Invalid):
        money_common.money(bad)


def test_money_accepts_exact_decimals_and_normalizes_to_six_places():
    assert money_common.money("0.000001") == D("0.000001")
    assert money_common.money(3) == D("3.000000") and money_common.money(D("12.5")) == D(
        "12.500000"
    )
    assert money_common.money("9999999999999.999999") == D("9999999999999.999999")
    with pytest.raises(errors.Invalid):
        money_common.money("10000000000000")


def test_ledger_totals_are_exact_for_amounts_binary_floats_cannot_represent(m):
    aid = new_account(m)
    for i, a in enumerate(("0.1", "0.2", "0.000001", "33.333333")):
        with m.F() as s:
            accts.adjust(
                s, aid, "credit", a, "seed", U(s, m.a1), idempotency_key=f"adj-exact-{i:04d}"
            )
            s.commit()
    t = tot(m, aid)
    assert t.funds == D("33.633334") and t.available_to_grant == D("33.633334")
    assert isinstance(t.funds, D) and str(t.funds) == "33.633334"


# --- pagesat ----------------------------------------------------------------------------------------------


def test_create_pending_payment_by_a_human_and_by_a_system_actor(m):
    aid = new_account(m)
    with m.F() as s:
        p = payments.create(s, aid, "25.5", actor=U(s, m.a1), note="wire")
        q = payments.create(
            s, aid, "10", system="system:payment_import", source="import", external_reference="X-1"
        )
        s.commit()
        assert (p.status, p.amount, p.currency, p.enterprise_id) == (
            "pending",
            D("25.500000"),
            "EUR",
            m.ent,
        )
        assert (p.created_by_id, p.created_by_label) == (m.a1, None)
        assert (q.created_by_id, q.created_by_label) == (None, "system:payment_import")
    kinds = {(a.actor_kind, a.actor_label) for a in audit_actions(m, "payment.create")}
    assert kinds == {("user", None), ("system", "system:payment_import")}  # pa përdorues të rremë
    assert tot(m, aid).funds == 0  # pending s'krijon para


def test_payment_inputs_are_validated_and_only_admins_create(m):
    aid = new_account(m)
    with m.F() as s:
        for kw in ({"amount": 1.5}, {"amount": "0"}, {"currency": "USD"}, {"source": "Bad Source"}):
            args = {"amount": "5", "actor": U(s, m.a1)} | kw
            with pytest.raises((errors.Invalid, errors.Conflict)):
                payments.create(s, aid, args.pop("amount"), **args)
        with pytest.raises(errors.Invalid):
            payments.create(s, aid, "5")  # as aktor as sistem
        with pytest.raises(errors.Invalid):
            payments.create(s, aid, "5", actor=U(s, m.a1), system="system:x")
        with pytest.raises(errors.Invalid):
            payments.create(s, aid, "5", system="not-a-system-label")
        with pytest.raises(errors.Forbidden):
            payments.create(s, aid, "5", actor=U(s, m.op))  # operator
        with pytest.raises(errors.Invalid):
            payments.create(s, aid, "5", actor="root")  # string, jo CentralUser


def test_approve_creates_exactly_one_commercial_credit_and_audits(m):
    aid = new_account(m)
    with m.F() as s:
        p = payments.create(s, aid, "100", actor=U(s, m.a1))
        s.commit()
    with m.F() as s:
        r = payments.approve(s, p.id, U(s, m.a2), now=T0)
        s.commit()
        assert (r.status, r.approved_by_id, r.approved_at) == (
            "approved",
            m.a2,
            T0.replace(tzinfo=None) if r.approved_at.tzinfo is None else T0,
        )
    t = tot(m, aid)
    assert (t.funds, t.available_to_grant, t.outstanding_grants) == (D("100"), D("100"), D("0"))
    with m.F() as s:
        (e,) = s.scalars(select(CommercialLedgerEntry)).all()
        assert (
            e.entry_type,
            e.amount,
            e.currency,
            e.source_type,
            e.source_id,
            e.actor_user_id,
        ) == ("payment_credit", D("100.000000"), "EUR", "payment", str(p.id), m.a2)
    (a,) = audit_actions(m, "payment.approve")
    assert (a.actor_id, a.detail["ledger_seq"]) == (m.a2, e.seq)


def test_maker_cannot_approve_their_own_payment_and_operators_cannot_approve(m):
    aid = new_account(m)
    with m.F() as s:
        p = payments.create(s, aid, "100", actor=U(s, m.a1))
        s.commit()
    with m.F() as s:
        with pytest.raises(errors.Conflict, match="maker-checker"):
            payments.approve(s, p.id, U(s, m.a1))
        s.rollback()
        with pytest.raises(errors.Forbidden):
            payments.approve(s, p.id, U(s, m.op))
        s.rollback()
        with pytest.raises(errors.Invalid):
            payments.approve(s, p.id, None)
    assert count(m, CommercialLedgerEntry) == 0 and tot(m, aid).funds == 0
    # kur krijuesi është sistem, çdo admin njeri mund ta miratojë
    fund(m, aid, "5")
    assert tot(m, aid).funds == D("5")


def test_maker_checker_is_also_enforced_by_a_database_check(m):
    aid = new_account(m)
    with m.F() as s:
        a = accts.get(s, aid)
        s.add(Payment(enterprise_id=m.ent, account_id=aid, currency="EUR", amount=D("1"), status="approved",
                      created_by_id=m.a1, approved_by_id=m.a1, approved_at=T0))  # fmt: skip
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()
        assert a is not None


def test_approve_replay_is_a_noop_and_never_duplicates_the_credit(m):
    aid = new_account(m)
    pid = fund(m, aid, "100")
    with m.F() as s:
        r = payments.approve(s, pid, U(s, m.a2))
        s.commit()
        assert r.status == "approved"
    assert count(m, CommercialLedgerEntry) == 1 and tot(m, aid).funds == D("100")
    assert len(audit_actions(m, "payment.approve")) == 1
    with m.F() as s:  # edhe me SQL të drejtpërdrejtë: UNIQUE(entry_type, source)
        s.add(CommercialLedgerEntry(seq=999, account_id=aid, currency="EUR", entry_type="payment_credit",
                                    amount=D("1"), source_type="payment", source_id=str(pid), actor_label="system:x"))  # fmt: skip
        with pytest.raises(IntegrityError):
            s.flush()


@PGONLY
def test_pg_concurrent_approvals_create_one_credit(m):
    aid = new_account(m)
    with m.F() as s:
        p = payments.create(s, aid, "100", system="system:payment_import")
        s.commit()
    barrier, out = threading.Barrier(2, timeout=20), []

    def go(who):
        def run():
            with m.F() as s:
                barrier.wait()
                out.append(payments.approve(s, p.id, U(s, who)).status)
                s.commit()

        return run

    assert not run_errors([go(m.a1), go(m.a2)])
    assert out == ["approved", "approved"]  # i dyti: no-op idempotent
    assert count(m, CommercialLedgerEntry) == 1 and len(audit_actions(m, "payment.approve")) == 1


def run_errors(fns):
    errs = []

    def wrap(fn):
        def go():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                errs.append(e)

        return go

    ts = [threading.Thread(target=wrap(f)) for f in fns]
    [t.start() for t in ts]
    [t.join(40) for t in ts]
    assert not [t for t in ts if t.is_alive()], "thread i varur"
    return errs


def test_reject_pending_requires_a_reason_and_creates_no_credit(m):
    aid = new_account(m)
    with m.F() as s:
        p = payments.create(s, aid, "50", actor=U(s, m.a1))
        s.commit()
    with m.F() as s:
        for bad in ("", "  ", None, "x" * 501):
            with pytest.raises(errors.Invalid):
                payments.reject(s, p.id, U(s, m.a2), bad)
        r = payments.reject(s, p.id, U(s, m.a1), "invalid proof")  # krijuesi mund ta anulojë
        s.commit()
        assert (r.status, r.rejection_reason, r.rejected_by_id) == (
            "rejected",
            "invalid proof",
            m.a1,
        )
        assert (
            payments.reject(s, p.id, U(s, m.a2), "again").rejection_reason == "invalid proof"
        )  # no-op
    assert count(m, CommercialLedgerEntry) == 0
    assert len(audit_actions(m, "payment.reject")) == 1


def test_rejected_cannot_be_approved_and_approved_cannot_be_rejected(m):
    aid = new_account(m)
    with m.F() as s:
        p = payments.create(s, aid, "50", system="system:payment_import")
        payments.reject(s, p.id, U(s, m.a1), "no")
        s.commit()
        with pytest.raises(errors.Conflict):
            payments.approve(s, p.id, U(s, m.a2))
        s.rollback()
    ok = fund(m, aid, "20")
    with m.F() as s:
        with pytest.raises(errors.Conflict, match="reversal"):
            payments.reject(s, ok, U(s, m.a2), "oops")
        s.rollback()
        with pytest.raises(MoneyImmutableError):
            s.delete(s.get(Payment, ok))
            s.flush()
        s.rollback()
    assert tot(m, aid).funds == D("20")


def test_payment_money_fields_are_immutable(m):
    aid = new_account(m)
    pid = fund(m, aid, "20")
    for field, val in (
        ("amount", D("21")),
        ("currency", "USD"),
        ("account_id", uuid.uuid4()),
        ("external_reference", "z"),
    ):
        with m.F() as s:
            p = s.get(Payment, pid)
            setattr(p, field, val)
            with pytest.raises(MoneyImmutableError):
                s.flush()
            s.rollback()


@PGONLY
def test_pg_payment_amount_and_final_status_are_protected_from_raw_sql(m):
    aid = new_account(m)
    pid = fund(m, aid, "20")
    for stmt in ("update payments set amount = 999 where id = :i", "update payments set status = 'rejected' where id = :i",
                 "delete from payments where id = :i"):  # fmt: skip
        with m.F() as s:
            with pytest.raises(DBAPIError):
                s.execute(text(stmt), {"i": pid})
            s.rollback()


def test_external_reference_uniqueness_is_scoped_by_source(m):
    aid = new_account(m)
    with m.F() as s:
        a = payments.create(
            s, aid, "5", system="system:payment_import", source="bank", external_reference="R-1"
        )
        b = payments.create(
            s, aid, "5", system="system:payment_import", source="bank", external_reference="R-1"
        )
        c = payments.create(
            s, aid, "5", system="system:payment_import", source="card", external_reference="R-1"
        )
        s.commit()
        assert a.id == b.id and c.id != a.id
        with pytest.raises(errors.Conflict):
            payments.create(
                s, aid, "6", system="system:payment_import", source="bank", external_reference="R-1"
            )
        s.rollback()
    assert count(m, Payment) == 2


def test_payment_approval_audit_is_atomic_failure_rolls_everything_back(m, monkeypatch):
    aid = new_account(m)
    with m.F() as s:
        p = payments.create(s, aid, "100", system="system:payment_import")
        s.commit()
    with m.F() as s:
        seq_before = money_sequence.current(s)[1]

    def boom(*a, **k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(payments.audit, "record", boom)
    with m.F() as s:
        with pytest.raises(RuntimeError):
            payments.approve(s, p.id, U(s, m.a1))
        s.rollback()
    with m.F() as s:
        assert s.get(Payment, p.id).status == "pending"
        assert money_sequence.current(s)[1] == seq_before
    assert count(m, CommercialLedgerEntry) == 0


# --- formula kanonike ---------------------------------------------------------------------------------------


def test_canonical_formula_payment_100_grant_40_is_not_140(m):
    aid = new_account(m)
    fund(m, aid, "100")
    grant(m, aid, "40")
    t = tot(m, aid)
    assert (t.funds, t.outstanding_grants, t.available_to_grant) == (D("100"), D("40"), D("60"))
    assert t.funds == t.payment_credits + t.credit_adjustments - t.debit_adjustments
    assert t.available_to_grant == t.funds - (t.grants_issued - t.grants_reversed)


def test_formula_over_a_mixed_history_matches_the_hand_computed_value(m):
    aid = new_account(m)
    fund(m, aid, "100")
    with m.F() as s:
        accts.adjust(s, aid, "credit", "20", "bonus", U(s, m.a1), idempotency_key="adj-mixed-0001")
        accts.adjust(s, aid, "debit", "5", "fee", U(s, m.a1), idempotency_key="adj-mixed-0002")
        s.commit()
    g1 = grant(m, aid, "50", "grant-mixed-01")
    grant(m, aid, "30", "grant-mixed-02")
    with m.F() as s:
        grants.reverse(s, g1, U(s, m.a1), "mistake")
        s.commit()
    t = tot(m, aid)
    assert t.funds == D("115")  # 100 + 20 - 5
    assert t.outstanding_grants == D("30")  # 50 + 30 - 50
    assert t.available_to_grant == D("85") and t.entries == 6


# --- grant-et ----------------------------------------------------------------------------------------------------


def test_issue_grant_from_available_funds_with_exact_decimal_and_audit(m):
    aid = new_account(m)
    fund(m, aid, "100")
    gid = grant(m, aid, "40.123456", note="first tranche")
    with m.F() as s:
        g = s.get(CreditGrant, gid)
        assert (g.amount, g.currency, g.status, g.enterprise_id, g.product_id, g.account_id) == (
            D("40.123456"), "EUR", "active", m.ent, m.sms, aid)  # fmt: skip
        assert (g.created_by_id, g.created_by_label) == (m.a1, None)
    assert tot(m, aid).available_to_grant == D("59.876544")
    (a,) = audit_actions(m, "credit_grant.create")
    assert (a.actor_id, a.detail["grant_id"], a.detail["amount"]) == (m.a1, str(gid), "40.123456")


def test_grant_can_be_issued_by_a_system_actor_with_an_explicit_label(m):
    aid = new_account(m)
    fund(m, aid, "10")
    with m.F() as s:
        g = grants.issue(
            s, aid, "3", idempotency_key="sys-grant-0001", system="system:grant_scheduler"
        )
        s.commit()
        assert (g.created_by_id, g.created_by_label) == (None, "system:grant_scheduler")
    (a,) = audit_actions(m, "credit_grant.create")
    assert (a.actor_kind, a.actor_label) == ("system", "system:grant_scheduler")


def test_insufficient_funds_is_blocked_and_the_exact_available_amount_succeeds(m):
    aid = new_account(m)
    fund(m, aid, "100")
    with m.F() as s:
        with pytest.raises(errors.InsufficientFunds):
            grants.issue(s, aid, "100.000001", idempotency_key="grant-key-over1", actor=U(s, m.a1))
        s.rollback()
    assert count(m, CreditGrant) == 0 and count(m, MoneyEvent) == 0
    grant(m, aid, "100", "grant-key-exact")
    assert tot(m, aid).available_to_grant == 0
    with m.F() as s:
        with pytest.raises(errors.InsufficientFunds):
            grants.issue(s, aid, "0.000001", idempotency_key="grant-key-over2", actor=U(s, m.a1))
        with pytest.raises(errors.Invalid):
            grants.issue(s, aid, 1.5, idempotency_key="grant-key-float", actor=U(s, m.a1))


def test_grant_may_reference_an_approved_payment_without_being_capped_to_it(m):
    aid = new_account(m)
    pid = fund(m, aid, "100")
    grant(m, aid, "40", "grant-pay-0001", source_payment_id=pid)
    grant(m, aid, "60", "grant-pay-0002", source_payment_id=pid)  # 40 + 60 nga një pagesë
    with m.F() as s:
        pending = payments.create(s, aid, "5", actor=U(s, m.a1))
        s.commit()
        with pytest.raises(errors.Conflict):
            grants.issue(
                s,
                aid,
                "1",
                idempotency_key="grant-pay-0003",
                actor=U(s, m.a1),
                source_payment_id=pending.id,
            )
        s.rollback()
        with pytest.raises(errors.NotFound):
            grants.issue(
                s,
                aid,
                "1",
                idempotency_key="grant-pay-0004",
                actor=U(s, m.a1),
                source_payment_id=uuid.uuid4(),
            )


def test_grant_idempotency_same_payload_returns_the_same_grant_changed_payload_conflicts(m):
    aid = new_account(m)
    fund(m, aid, "100")
    g1 = grant(m, aid, "10", "idem-key-0001", note="a")
    g2 = grant(m, aid, "10", "idem-key-0001", note="a")
    assert g1 == g2
    assert count(m, CreditGrant) == 1 and count(m, MoneyEvent) == 1
    assert count(m, CommercialLedgerEntry, CommercialLedgerEntry.entry_type == "grant_issued") == 1
    assert len(audit_actions(m, "credit_grant.create")) == 1
    for kw in ({"amount": "11"}, {"note": "other"}):
        with m.F() as s:
            args = {"amount": "10", "note": "a"} | kw
            with pytest.raises(errors.Conflict):
                grants.issue(
                    s,
                    aid,
                    args["amount"],
                    idempotency_key="idem-key-0001",
                    actor=U(s, m.a1),
                    note=args["note"],
                )
    assert tot(m, aid).outstanding_grants == D("10")
    with m.F() as s:  # replay edhe kur fondet u shterrën: s'bëhet InsufficientFunds
        accts.adjust(s, aid, "debit", "90", "drain", U(s, m.a1), idempotency_key="adj-drain-0001")
        s.commit()
        assert (
            grants.issue(
                s, aid, "10", idempotency_key="idem-key-0001", actor=U(s, m.a1), note="a"
            ).id
            == g1
        )


def test_grant_fields_are_immutable_but_status_may_move_once(m):
    aid = new_account(m)
    fund(m, aid, "100")
    gid = grant(m, aid, "10")
    for field, val in (("amount", D("11")), ("currency", "USD"), ("account_id", uuid.uuid4()),
                       ("product_id", uuid.uuid4()), ("idempotency_key", "other-key-0001")):  # fmt: skip
        with m.F() as s:
            g = s.get(CreditGrant, gid)
            setattr(g, field, val)
            with pytest.raises(MoneyImmutableError):
                s.flush()
            s.rollback()
    with m.F() as s:
        with pytest.raises(MoneyImmutableError):
            s.delete(s.get(CreditGrant, gid))
            s.flush()


@PGONLY
def test_pg_grant_amount_cannot_change_and_reversed_status_is_final(m):
    aid = new_account(m)
    fund(m, aid, "100")
    gid = grant(m, aid, "10")
    with m.F() as s:
        with pytest.raises(DBAPIError):
            s.execute(text("update credit_grants set amount = 99 where id = :i"), {"i": gid})
        s.rollback()
        grants.reverse(s, gid, U(s, m.a1), "undo")
        s.commit()
    with m.F() as s:
        with pytest.raises(DBAPIError):  # reversed → active s'lejohet
            s.execute(
                text(
                    "update credit_grants set status='active', reversed_at=null, reversed_by_id=null, reversal_reason=null where id=:i"
                ),
                {"i": gid},
            )
        s.rollback()


def test_grant_emits_an_immutable_money_event_with_a_frozen_payload(m):
    aid = new_account(m)
    fund(m, aid, "100")
    gid = grant(m, aid, "40", note="internal note")
    with m.F() as s:
        (ev,) = s.scalars(select(MoneyEvent)).all()
        assert (ev.event_type, ev.enterprise_id, ev.account_id, ev.entity_id, ev.entity_type) == (
            "credit_grant.issued", m.ent, aid, gid, "credit_grant")  # fmt: skip
        assert ev.payload == {
            "grant_id": str(gid), "account_id": str(aid), "enterprise_id": str(m.ent),
            "product_id": str(m.sms), "amount": "40.000000", "currency": "EUR", "status": "active",
            "source_payment_id": None, "created_at": ev.payload["created_at"],
            "purpose": "standard", "baseline_ref": None,
        }  # fmt: skip
        assert "internal note" not in str(ev.payload)  # pa shënime/arsye të brendshme
        frozen = dict(ev.payload)
    with m.F() as s:
        grants.reverse(s, gid, U(s, m.a1), "undo")
        s.commit()
    with m.F() as s:
        issued = s.scalar(select(MoneyEvent).where(MoneyEvent.event_type == "credit_grant.issued"))
        assert issued.payload == frozen and issued.payload["status"] == "active"  # s'u ndryshua
        rev = s.scalar(select(MoneyEvent).where(MoneyEvent.event_type == "credit_grant.reversed"))
        assert rev.payload["status"] == "reversed" and rev.payload["amount"] == "40.000000"
        assert "undo" not in str(rev.payload)


def test_grant_transaction_rollback_removes_grant_event_ledger_and_audit_together(m, monkeypatch):
    aid = new_account(m)
    fund(m, aid, "100")
    with m.F() as s:
        seq_before = money_sequence.current(s)[1]
    n_audit = len(audit_actions(m, "%"))

    def boom(*a, **k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(grants.audit, "record", boom)
    with m.F() as s:
        with pytest.raises(RuntimeError):
            grants.issue(s, aid, "10", idempotency_key="grant-rollback1", actor=U(s, m.a1))
        s.rollback()
    assert count(m, CreditGrant) == 0 and count(m, MoneyEvent) == 0
    assert count(m, CommercialLedgerEntry, CommercialLedgerEntry.entry_type == "grant_issued") == 0
    assert len(audit_actions(m, "%")) == n_audit
    with m.F() as s:
        assert money_sequence.current(s)[1] == seq_before  # pa seq fantazmë
    assert tot(m, aid).available_to_grant == D("100")


@PGONLY
def test_pg_concurrent_grants_cannot_overspend(m):
    aid = new_account(m)
    fund(m, aid, "100")
    n = 6
    barrier, res = threading.Barrier(n, timeout=30), []

    def go(i):
        def run():
            with m.F() as s:
                barrier.wait()
                try:
                    grants.issue(
                        s, aid, "30", idempotency_key=f"race-grant-{i:04d}", actor=U(s, m.a1)
                    )
                    s.commit()
                    res.append("ok")
                except errors.InsufficientFunds:
                    s.rollback()
                    res.append("denied")

        return run

    assert not run_errors([go(i) for i in range(n)])
    assert res.count("ok") == 3 and res.count("denied") == 3  # 3×30 ≤ 100 < 4×30
    t = tot(m, aid)
    assert (t.outstanding_grants, t.available_to_grant) == (D("90"), D("10"))
    assert count(m, CreditGrant) == 3 and count(m, MoneyEvent) == 3


@PGONLY
def test_pg_concurrent_same_idempotency_key_creates_one_grant(m):
    aid = new_account(m)
    fund(m, aid, "100")
    barrier, ids = threading.Barrier(3, timeout=30), []

    def run():
        with m.F() as s:
            barrier.wait()
            g = grants.issue(s, aid, "10", idempotency_key="same-key-0001", actor=U(s, m.a1))
            s.commit()
            ids.append(g.id)

    assert not run_errors([run, run, run])
    assert len(set(ids)) == 1 and count(m, CreditGrant) == 1 and count(m, MoneyEvent) == 1


# --- reversal ----------------------------------------------------------------------------------------------------


def test_reverse_once_restores_availability_and_never_deletes_the_grant(m):
    aid = new_account(m)
    fund(m, aid, "100")
    gid = grant(m, aid, "40")
    assert tot(m, aid).available_to_grant == D("60")
    with m.F() as s:
        g = grants.reverse(s, gid, U(s, m.a2), "issued by mistake", now=T0)
        s.commit()
        assert (g.status, g.reversed_by_id, g.reversal_reason, g.amount) == (
            "reversed",
            m.a2,
            "issued by mistake",
            D("40"),
        )
    t = tot(m, aid)
    assert (t.funds, t.outstanding_grants, t.available_to_grant) == (D("100"), D("0"), D("100"))
    assert count(m, CreditGrant) == 1  # origjinali mbetet
    assert count(m, CommercialLedgerEntry, CommercialLedgerEntry.entry_type == "grant_issued") == 1
    (a,) = audit_actions(m, "credit_grant.reverse")
    assert a.actor_id == m.a2 and a.detail["reason"] == "issued by mistake"


def test_duplicate_reversal_is_a_noop_without_duplicate_funds(m):
    aid = new_account(m)
    fund(m, aid, "100")
    gid = grant(m, aid, "40")
    for _ in range(3):
        with m.F() as s:
            grants.reverse(s, gid, U(s, m.a1), "undo")
            s.commit()
    assert (
        count(m, CommercialLedgerEntry, CommercialLedgerEntry.entry_type == "grant_reversal") == 1
    )
    assert count(m, MoneyEvent, MoneyEvent.event_type == "credit_grant.reversed") == 1
    assert len(audit_actions(m, "credit_grant.reverse")) == 1
    assert tot(m, aid).available_to_grant == D("100")  # jo 140


def test_reversal_requires_a_reason_an_existing_grant_and_an_admin(m):
    aid = new_account(m)
    fund(m, aid, "100")
    gid = grant(m, aid, "40")
    with m.F() as s:
        for bad in ("", "   ", None):
            with pytest.raises(errors.Invalid):
                grants.reverse(s, gid, U(s, m.a1), bad)
        with pytest.raises(errors.NotFound):
            grants.reverse(s, uuid.uuid4(), U(s, m.a1), "x")
        with pytest.raises(errors.Invalid):
            grants.reverse(s, "not-a-uuid", U(s, m.a1), "x")
        with pytest.raises(errors.Forbidden):
            grants.reverse(s, gid, U(s, m.op), "x")
        s.rollback()
    assert tot(m, aid).outstanding_grants == D("40")


@PGONLY
def test_pg_concurrent_reversals_restore_funds_once(m):
    aid = new_account(m)
    fund(m, aid, "100")
    gid = grant(m, aid, "40")
    barrier = threading.Barrier(2, timeout=20)

    def run():
        with m.F() as s:
            barrier.wait()
            grants.reverse(s, gid, U(s, m.a1), "race")
            s.commit()

    assert not run_errors([run, run])
    assert tot(m, aid).available_to_grant == D("100") and count(m, MoneyEvent) == 2


# --- rregullime manuale -------------------------------------------------------------------------------------------


def test_manual_credit_and_debit_adjustments_are_ledger_entries_with_reason_and_audit(m):
    aid = new_account(m)
    with m.F() as s:
        c = accts.adjust(
            s, aid, "credit", "50", "goodwill credit", U(s, m.a1), idempotency_key="adj-credit-001"
        )
        d = accts.adjust(
            s, aid, "debit", "20", "chargeback", U(s, m.a2), idempotency_key="adj-debit-0001"
        )
        s.commit()
        assert (c.entry_type, c.reason, c.actor_user_id) == (
            "manual_credit_adjustment",
            "goodwill credit",
            m.a1,
        )
        assert (d.entry_type, d.reason, d.actor_user_id) == (
            "manual_debit_adjustment",
            "chargeback",
            m.a2,
        )
    assert tot(m, aid).funds == D("30")
    assert [a.actor_id for a in audit_actions(m, "credit_adjustment.create")] == [m.a1]
    assert [a.actor_id for a in audit_actions(m, "debit_adjustment.create")] == [m.a2]


def test_adjustments_require_a_reason_admin_valid_kind_and_idempotency(m):
    aid = new_account(m)
    with m.F() as s:
        for bad in ("", "  ", None):
            with pytest.raises(errors.Invalid):
                accts.adjust(
                    s, aid, "credit", "1", bad, U(s, m.a1), idempotency_key="adj-reason-001"
                )
        with pytest.raises(errors.Invalid):
            accts.adjust(s, aid, "bonus", "1", "x", U(s, m.a1), idempotency_key="adj-kind-0001")
        with pytest.raises(errors.Forbidden):
            accts.adjust(s, aid, "credit", "1", "x", U(s, m.op), idempotency_key="adj-oper-0001")
        with pytest.raises(errors.Invalid):
            accts.adjust(s, aid, "credit", "1", "x", U(s, m.a1), idempotency_key="short")
        e1 = accts.adjust(s, aid, "credit", "5", "x", U(s, m.a1), idempotency_key="adj-same-0001")
        e2 = accts.adjust(s, aid, "credit", "5", "x", U(s, m.a1), idempotency_key="adj-same-0001")
        assert e1.id == e2.id
        with pytest.raises(errors.Conflict):
            accts.adjust(s, aid, "debit", "5", "x", U(s, m.a1), idempotency_key="adj-same-0001")
        s.commit()
    assert tot(m, aid).funds == D("5") and len(audit_actions(m, "credit_adjustment.create")) == 1


def test_a_debit_cannot_make_the_grantable_funds_negative(m):
    aid = new_account(m)
    fund(m, aid, "100")
    grant(m, aid, "70")
    with m.F() as s:
        with pytest.raises(errors.Conflict, match="negative"):
            accts.adjust(
                s,
                aid,
                "debit",
                "30.000001",
                "too much",
                U(s, m.a1),
                idempotency_key="adj-neg-00001",
            )
        s.rollback()
        accts.adjust(s, aid, "debit", "30", "exact", U(s, m.a1), idempotency_key="adj-neg-00002")
        s.commit()
    t = tot(m, aid)
    assert (t.funds, t.outstanding_grants, t.available_to_grant) == (D("70"), D("70"), D("0"))


def test_ledger_cannot_be_given_a_nonpositive_amount_or_a_reasonless_adjustment_by_sql(m):
    aid = new_account(m)
    for kw in (
        {"amount": D("0")},
        {"amount": D("-1")},
        {"entry_type": "manual_credit_adjustment", "reason": None},
    ):
        with m.F() as s:
            base = dict(seq=1000, account_id=aid, currency="EUR", entry_type="payment_credit", amount=D("1"),
                        source_type="payment", source_id=str(uuid.uuid4()), actor_label="system:x")  # fmt: skip
            s.add(CommercialLedgerEntry(**(base | kw)))
            with pytest.raises(IntegrityError):
                s.flush()
            s.rollback()
    if m.url.startswith("sqlite"):
        return  # FK-të nuk detyrohen në SQLite; në PG: monedha e hyrjes duhet të përputhet me llogarinë
    with m.F() as s:
        s.add(CommercialLedgerEntry(seq=1001, account_id=aid, currency="USD", entry_type="payment_credit",
                                    amount=D("1"), source_type="payment", source_id="x", actor_label="system:x"))  # fmt: skip
        with pytest.raises(IntegrityError):
            s.flush()


# --- pandryshueshmëria -------------------------------------------------------------------------------------------------


def test_orm_rejects_updating_or_deleting_ledger_entries_and_money_events(m):
    aid = new_account(m)
    fund(m, aid, "100")
    grant(m, aid, "10")
    with m.F() as s:
        e = s.scalar(select(CommercialLedgerEntry))
        e.amount = D("999")
        with pytest.raises(MoneyImmutableError):
            s.flush()
        s.rollback()
        s.delete(s.scalar(select(CommercialLedgerEntry)))
        with pytest.raises(MoneyImmutableError):
            s.flush()
        s.rollback()
        ev = s.scalar(select(MoneyEvent))
        ev.payload = {"x": 1}
        with pytest.raises(MoneyImmutableError):
            s.flush()
        s.rollback()
        s.delete(s.scalar(select(MoneyEvent)))
        with pytest.raises(MoneyImmutableError):
            s.flush()


@PGONLY
@pytest.mark.parametrize("table", ["commercial_ledger_entries", "money_events"])
def test_pg_triggers_reject_raw_update_delete_and_truncate(m, table):
    aid = new_account(m)
    fund(m, aid, "100")
    grant(m, aid, "10")
    pk = "id" if table == "commercial_ledger_entries" else "seq"
    for stmt in (
        f"update {table} set created_at = now()",
        f"delete from {table}",
        f"truncate {table}",
    ):
        with m.F() as s:
            with pytest.raises(DBAPIError) as e:
                s.execute(text(stmt))
            assert "append-only" in str(e.value)
            s.rollback()
    assert count(m, CommercialLedgerEntry) >= 1 and pk


# --- sekuenca + ditari -----------------------------------------------------------------------------------------------------


def all_seqs(m):
    with m.F() as s:
        led = list(s.scalars(select(CommercialLedgerEntry.seq)))
        ev = list(s.scalars(select(MoneyEvent.seq)))
    return led, ev


def test_money_sequence_is_strictly_increasing_unique_and_gapless_for_committed_work(m):
    aid = new_account(m)
    fund(m, aid, "100")
    for i in range(4):
        grant(m, aid, "5", f"seq-grant-{i:04d}")
    led, ev = all_seqs(m)
    union = sorted(led + ev)
    assert union == list(range(1, len(union) + 1))  # pa boshllëqe, pa dublikatë
    assert ev == sorted(ev) and len(set(ev)) == len(ev)
    with m.F() as s:
        epoch, last = money_sequence.current(s)
        assert last == union[-1] and isinstance(epoch, uuid.UUID)
        assert [e.seq for e in grants.events_after(s, 0)] == ev
        assert [e.seq for e in grants.events_after(s, ev[1])] == ev[2:]
        assert [e.seq for e in ledger.history(s, aid, after_seq=0, limit=2)] == sorted(led)[:2]


def test_rolled_back_work_leaves_no_phantom_event_and_no_cursor_gap(m):
    aid = new_account(m)
    fund(m, aid, "100")
    with m.F() as s:
        before = money_sequence.current(s)[1]
        grants.issue(s, aid, "10", idempotency_key="phantom-0001", actor=U(s, m.a1))
        s.rollback()  # e hedh poshtë pas alokimit të seq
    with m.F() as s:
        assert money_sequence.current(s)[1] == before
    assert count(m, MoneyEvent) == 0
    grant(m, aid, "10", "phantom-0002")
    led, ev = all_seqs(m)
    assert sorted(led + ev) == list(range(1, len(led + ev) + 1))


@PGONLY
def test_pg_concurrent_event_creation_gives_unique_ordered_seqs_and_unique_event_ids(m):
    aid = new_account(m)
    fund(m, aid, "1000")
    n = 8
    barrier = threading.Barrier(n, timeout=30)

    def go(i):
        def run():
            with m.F() as s:
                barrier.wait()
                grants.issue(s, aid, "10", idempotency_key=f"conc-grant-{i:04d}", actor=U(s, m.a1))
                s.commit()

        return run

    assert not run_errors([go(i) for i in range(n)])
    led, ev = all_seqs(m)
    assert sorted(led + ev) == list(range(1, len(led + ev) + 1))
    with m.F() as s:
        rows = list(s.scalars(select(MoneyEvent).order_by(MoneyEvent.seq)))
        assert len({r.event_id for r in rows}) == n == len(rows)
        assert len({r.entity_id for r in rows}) == n  # një ngjarje issued për grant
        assert [r.created_at for r in rows] == sorted(
            r.created_at for r in rows
        )  # rendi i seq ≈ commit


def test_semantic_source_uniqueness_is_enforced_for_grants_and_events(m):
    aid = new_account(m)
    fund(m, aid, "100")
    gid = grant(m, aid, "10")
    with m.F() as s:
        s.add(MoneyEvent(seq=500, event_type="credit_grant.issued", enterprise_id=m.ent, account_id=aid,
                         entity_id=gid, payload={}))  # fmt: skip
        with pytest.raises(IntegrityError):
            s.flush()
        s.rollback()
        s.add(CommercialLedgerEntry(seq=501, account_id=aid, currency="EUR", entry_type="grant_issued", amount=D("1"),
                                    source_type="credit_grant", source_id=str(gid), actor_label="system:x"))  # fmt: skip
        with pytest.raises(IntegrityError):
            s.flush()


# --- kufijtë ----------------------------------------------------------------------------------------------------------------------------------


def test_money_modules_are_central_only_and_expose_no_http_routes(m):
    forbidden = {"app", "httpx", "requests", "urllib"}
    for f in ("models/money.py", "services/money_common.py", "services/money_sequence.py", "services/commercial_ledger.py",
              "services/credit_accounts.py", "services/payments.py", "services/grants.py"):  # fmt: skip
        tree = ast.parse((ROOT / "apps/central" / f).read_text())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                mods.add(n.module.split(".")[0])
        assert not mods & forbidden, (f, mods & forbidden)
    from apps.central.main import create_app

    paths = create_app(m.eng).openapi()["paths"]
    # M9-c: feed-i i brendshëm `/internal/money/*`; M9-f: API admin me RBAC (`/admin/money|financial|pricing`).
    # Asnjë API klienti/publik për para: çdo rrugë parash është ose e brendshme ose admin.
    assert not [
        p
        for p in paths
        if not p.startswith(("/internal/money/", "/admin/money/", "/admin/financial/"))
        and any(w in p.lower() for w in ("payment", "grant", "credit", "money", "ledger"))
    ]


def test_enterprise_money_and_authority_are_untouched_by_m9b(m):
    from app.core.db import Base as EnterpriseBase

    assert not {
        t
        for t in EnterpriseBase.metadata.tables
        if t.startswith(("credit_", "money_", "commercial_"))
    }
    versions = sorted(p.name for p in (ROOT / "alembic/versions").glob("0*.py"))
    # M9-b s'preku Enterprise; M9-c shtoi 0023 (autoriteti i parave) — kjo ruan që 0022 ekziston ende pa u ndryshuar
    assert "0022_dispatch_started_at.py" in versions and versions[-1].startswith("0027")


def test_migration_0017_up_down_up_readiness_and_metadata(make_db):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from apps.central.core import readiness
    from apps.central.core.db import Base
    from tests.test_central import central_alembic

    url = make_db("central")
    central_alembic(url, "upgrade", "0016")
    eng = create_engine(url)
    assert "credit_accounts" not in set(inspect(eng).get_table_names())
    assert readiness.check(eng) is not None  # prapa kokës
    central_alembic(url, "upgrade", "head")
    names = set(inspect(eng).get_table_names())
    assert {
        "credit_accounts",
        "money_sequence",
        "commercial_ledger_entries",
        "payments",
        "credit_grants",
        "money_events",
    } <= names
    with eng.connect() as c:
        assert c.execute(text("select last_seq from money_sequence where id = 1")).scalar() == 0
        ctx = MigrationContext.configure(
            c, opts={"compare_type": True, "version_table": "central_alembic_version"}
        )
        assert compare_metadata(ctx, Base.metadata) == []
    assert readiness.check(eng) is None
    central_alembic(url, "downgrade", "0016")
    assert "credit_accounts" not in set(inspect(eng).get_table_names())
    central_alembic(url, "upgrade", "head")
    if eng.dialect.name == "postgresql":
        with eng.connect() as c:
            trg = {
                r[0]
                for r in c.execute(text("select tgname from pg_trigger where not tgisinternal"))
            }
        assert {"trg_commercial_ledger_entries_immutable", "trg_money_events_immutable", "trg_payments_guard",
                "trg_credit_grants_guard", "trg_credit_accounts_guard"} <= trg  # fmt: skip
    eng.dispose()
