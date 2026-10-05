# ruff: noqa: F811
"""M9-c — Enterprise: porta e mint-it lokal, baseline, aplikuesi i `cp.money.v1`, reversal, kursori, poller,
readiness. Pa HTTP (përveç E2E në skedarin tjetër); ngjarjet ndërtohen me kontratën reale."""

import itertools
import uuid
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models.control_plane import Entitlement
from app.models.enterprise import Enterprise
from app.models.money_authority import (
    G_APPLIED,
    G_DEFERRED,
    G_MATCHED,
    G_MISMATCH,
    G_RECON,
    G_REVERSED,
    G_UNMAPPED,
    G_VOIDED,
    MoneyAuthorityImmutableError,
    MoneyBaseline,
    MoneyGrant,
)
from app.models.wallet import EntryType, Hold, LedgerEntry, Wallet
from app.services import money_authority as ma
from app.services import money_sync as ms
from app.services import wallet as wallets
from app.services.wallet import MoneyAuthorityFrozen, TopupMethod
from packages.contracts.control_plane.money import v1

EID = uuid.UUID(int=0xE1)
SMS_P, EMAIL_P, ACCT = uuid.UUID(int=0xA1), uuid.UUID(int=0xA2), uuid.UUID(int=0xAC)
EPOCH = uuid.UUID("11111111-1111-4111-8111-111111111111")
NOW = datetime(2030, 1, 1, tzinfo=UTC)
_seq = itertools.count(1)


def mode(monkeypatch, value):
    monkeypatch.setattr(settings, "money_authority", value)


def mk_world(db, balance="12", hold="2", sms_entitlement=True, owner="acme"):
    """Enterprise + entitlement SMS + wallet EUR me bilanc lokal (kredituar në mode local)."""
    db.add(Enterprise(id=EID, owner_ref=owner))
    if sms_entitlement:
        db.add(Entitlement(enterprise_id=EID, assignment_id=uuid.uuid4(), product_id=SMS_P,
                           product_code="sms", channel="sms", status="active", revision=1))  # fmt: skip
    db.flush()
    w = wallets.create_wallet(db, owner, "EUR")
    w.enterprise_id = EID
    if balance:
        wallets.confirm_topup(db, wallets.create_topup(db, w.id, balance, TopupMethod.CASH).id)
    if hold:
        wallets.reserve(db, w.id, hold, "seed-hold")
    db.commit()
    return w


def bal(db, w):
    db.expire_all()
    return wallets.balances(db, w.id)


def ledger_count(db, w):
    return db.scalar(
        select(func.count()).select_from(LedgerEntry).where(LedgerEntry.wallet_id == w.id)
    )


def gev(kind, gid, amount, *, seq=None, purpose="standard", ref=None, ent=EID, prod=SMS_P, cur="EUR",
        event_id=None):  # fmt: skip
    seq = seq or next(_seq)
    return v1.MoneyEventV1(
        event_id=str(event_id or uuid.uuid4()), seq=seq,
        event_type="credit_grant.issued" if kind == "issued" else "credit_grant.reversed",
        enterprise_id=str(ent), grant_id=str(gid), occurred_at=NOW,
        data=v1.GrantDataV1(str(ACCT), str(prod), cur, D(amount), purpose, ref),
    )  # fmt: skip


def apply(db, events, next_seq=None, init=True):
    if init and ms.get_cursor(db).epoch is None:
        ms.init_cursor(db, EPOCH, 1)
        db.flush()
    nxt = next_seq or max((e.seq for e in events), default=ms.get_cursor(db).last_seq)
    r = ms.apply_batch(
        db, epoch=EPOCH, authorization_generation=1, events=events, next_seq=nxt, now=NOW
    )
    db.commit()
    return r


def grant_row(db, gid):
    db.expire_all()
    return db.get(MoneyGrant, gid)


def baseline_world(db, monkeypatch):
    """12 EUR (10 available + 2 held) → shadow → baseline."""
    w = mk_world(db)
    mode(monkeypatch, "shadow")
    b = ma.create_baseline(db, w.id, "op-1")
    db.commit()
    return w, b


# =============================================================================================================
# A. porta e mint-it lokal
# =============================================================================================================


def test_local_mode_is_unchanged(db):
    w = mk_world(db, "5", None)
    wallets.adjustment(db, w.id, "1", "k1", "n")
    h = wallets.reserve(db, w.id, "2", "r1")
    wallets.capture(db, h.id)
    wallets.refund(db, h.id, "1", "rf")
    db.commit()
    assert wallets.balances(db, w.id) == (D("5"), D("0"))


@pytest.mark.parametrize("m", ["shadow", "central"])
def test_positive_local_mint_is_blocked_everywhere_but_operational_traffic_continues(
    db, monkeypatch, m
):
    w = mk_world(db, "10", "1")
    h_active = db.scalar(select(Hold))
    h2 = wallets.reserve(db, w.id, "2", "to-capture")
    h3 = wallets.reserve(db, w.id, "2", "to-release")
    db.commit()
    mode(monkeypatch, m)
    t = wallets.create_topup(db, w.id, "5", TopupMethod.CASH)
    with pytest.raises(MoneyAuthorityFrozen):
        wallets.confirm_topup(db, t.id)
    db.rollback()
    with pytest.raises(MoneyAuthorityFrozen):
        wallets.adjustment(db, w.id, "3", "k", "n")
    db.rollback()
    wallets.capture(db, h2.id)  # normal operational traffic
    wallets.release(db, h3.id)
    wallets.reserve(db, w.id, "1", "new")
    wallets.adjustment(db, w.id, "-1", "neg", "valid negative")
    db.commit()
    assert wallets.verify_wallet(db, w.id)
    assert h_active.status.value == "active"


def test_refund_is_bounded_by_the_captured_amount_and_blocked_under_shadow_and_central(
    db, monkeypatch
):
    w = mk_world(db, "10", None)
    h = wallets.reserve(db, w.id, "4", "m")
    open_hold = wallets.reserve(db, w.id, "1", "open")
    wallets.capture(db, h.id)
    with pytest.raises(wallets.Conflict):  # hold i pakapur
        wallets.refund(db, open_hold.id, "1", "x")
    with pytest.raises(wallets.InvalidAmount):  # më shumë se e kapura
        wallets.refund(db, h.id, "4.000001", "x")
    wallets.refund(db, h.id, "3", "a")
    wallets.refund(db, h.id, "3", "a")  # idempotent
    with pytest.raises(wallets.InvalidAmount):  # kumulative > e kapura
        wallets.refund(db, h.id, "1.5", "b")
    wallets.refund(db, h.id, "1", "c")
    caps = {}
    for m in ("shadow", "central"):
        caps[m] = wallets.reserve(db, w.id, "0.5", f"cap-{m}")
        wallets.capture(db, caps[m].id)
    db.commit()
    for m in ("shadow", "central"):
        mode(monkeypatch, m)
        with pytest.raises(MoneyAuthorityFrozen):
            wallets.refund(db, caps[m].id, "0.5", f"r-{m}")
        db.rollback()


def test_refund_has_no_arbitrary_amount_entry_point():
    import inspect

    sig = inspect.signature(wallets.refund)
    assert list(sig.parameters)[:2] == ["db", "hold_id"]


def test_direct_ledger_insert_is_blocked_by_the_orm_guard(db, monkeypatch):
    w = mk_world(db, "1", None)
    mode(monkeypatch, "shadow")
    db.add(LedgerEntry(wallet_id=w.id, entry_type=EntryType.TOPUP, available_delta=D("5"),
                       held_delta=D("0"), available_after=D("6"), held_after=D("0"),
                       idempotency_key="sneaky"))  # fmt: skip
    with pytest.raises(MoneyAuthorityFrozen):
        db.flush()
    db.rollback()


def test_grant_entries_are_only_for_money_sync(db, monkeypatch):
    w = mk_world(db, "1", None)
    mode(monkeypatch, "central")
    with pytest.raises(MoneyAuthorityFrozen):  # pa authoritative=True
        wallets._post(db, w.id, EntryType.GRANT, D("5"), D("0"), "g", "grant", "g")
    db.rollback()
    mode(monkeypatch, "shadow")
    with pytest.raises(
        MoneyAuthorityFrozen
    ):  # grant me kredi vetëm nën central, edhe authoritative
        wallets._post(
            db, w.id, EntryType.GRANT, D("5"), D("0"), "g", "grant", "g", authoritative=True
        )
    db.rollback()


def test_online_payment_is_not_credited_locally_when_frozen(db, monkeypatch):
    from app.models.billing import Payment, PaymentPurpose, PaymentStatus
    from app.services import payments

    w = mk_world(db, "1", None)
    p = Payment(owner_ref="acme", purpose=PaymentPurpose.TOPUP, wallet_id=w.id, amount=D("5"),
                currency="EUR", provider="fake", external_id="x1", checkout_url="http://x")  # fmt: skip
    db.add(p)
    db.commit()
    mode(monkeypatch, "central")
    with pytest.raises(wallets.Conflict):
        payments.complete(db, "fake", "x1", "succeeded", "5", "EUR")
    assert p.status == PaymentStatus.FAILED and p.failure_reason == "money_authority_frozen"
    db.rollback()


def test_wallet_does_not_pay_invoices_under_shadow_or_central(db, monkeypatch):
    import inspect
    from types import SimpleNamespace

    from app.core.errors import Conflict
    from app.models.billing import InvoiceStatus
    from app.services import billing

    w = mk_world(db, "50", None)
    monkeypatch.setattr(billing, "_get_invoice", lambda *a, **k: SimpleNamespace(
        status=InvoiceStatus.OPEN, currency="EUR", total=D("5"), number="INV-1", id=1))  # fmt: skip
    for m in ("shadow", "central"):
        mode(monkeypatch, m)
        with pytest.raises(Conflict):
            billing.pay_from_wallet(db, "acme", 1)
    assert wallets.balances(db, w.id) == (D("50"), D("0"))
    assert 'settings.money_authority == "local"' in inspect.getsource(billing.generate_invoice)


def test_no_module_posts_to_the_ledger_except_wallet_and_money_sync():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    offenders = []
    for f in list((root / "app").rglob("*.py")) + list((root / "scripts").glob("*.py")):
        rel = f.relative_to(root).as_posix()
        if rel in ("app/services/wallet.py", "app/services/money_sync.py"):
            continue
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.Call):
                fn = n.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name in ("_post", "LedgerEntry"):
                    offenders.append((rel, name))
    assert offenders == [], offenders


# =============================================================================================================
# B. baseline
# =============================================================================================================


def test_baseline_requires_shadow_and_records_gross_with_active_holds(db, monkeypatch):
    w = mk_world(db)
    with pytest.raises(ma.BaselineError):
        ma.create_baseline(db, w.id, "op")  # authority=local
    mode(monkeypatch, "shadow")
    b = ma.create_baseline(db, w.id, "op")
    db.commit()
    assert (b.available_at_cutover, b.held_at_cutover, b.gross_at_cutover) == (
        D("10"),
        D("2"),
        D("12"),
    )
    assert (b.currency, b.product_id, b.enterprise_id, b.wallet_id) == ("EUR", SMS_P, EID, w.id)
    assert b.ledger_max_id == db.scalar(select(func.max(LedgerEntry.id)))
    assert ma.baseline_valid(b) and len(b.baseline_ref) == 64 and b.created_by == "op"
    assert ma.baseline_ref_of(b) == b.baseline_ref


def test_baseline_is_immutable(db, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    w, b = baseline_world(db, monkeypatch)
    for field, value in (("gross_at_cutover", D("99")), ("available_at_cutover", D("1")),
                         ("ledger_max_id", 1), ("currency", "USD"), ("baseline_ref", "0" * 64)):  # fmt: skip
        setattr(b, field, value)
        with pytest.raises(MoneyAuthorityImmutableError):
            db.flush()
        db.rollback()
        b = db.get(MoneyBaseline, b.id)
    db.delete(b)
    with pytest.raises(MoneyAuthorityImmutableError):
        db.flush()
    db.rollback()
    bad = MoneyBaseline(baseline_ref="a" * 64, wallet_id=w.id, enterprise_id=EID, currency="EUR",
                        product_id=SMS_P, available_at_cutover=D("1"), held_at_cutover=D("1"),
                        gross_at_cutover=D("5"), ledger_max_id=1, created_by="x", status="superseded",
                        superseded_at=NOW)  # fmt: skip
    db.add(bad)
    with pytest.raises(IntegrityError):  # gross = available + held (CHECK)
        db.flush()
    db.rollback()


def test_tampering_with_a_baseline_is_detected_by_the_hash(db, monkeypatch):
    w, b = baseline_world(db, monkeypatch)
    from sqlalchemy import text

    db.execute(
        text("UPDATE sms_money_baselines SET available_at_cutover = 9, gross_at_cutover = 11")
    )
    db.commit()
    db.expire_all()
    assert not ma.baseline_valid(db.get(MoneyBaseline, b.id))


def test_baseline_refusals(db, monkeypatch):
    # gross 0
    w = mk_world(db, None, None)
    mode(monkeypatch, "shadow")
    with pytest.raises(ma.BaselineError, match="gross balance is 0"):
        ma.create_baseline(db, w.id, "op")
    # held ≠ active holds (rresht HOLD pa Hold)
    wallets._post(
        db, w.id, EntryType.CAPTURE, D("0"), D("0"), "noop", "x", "x"
    )  # zero-delta: pa efekt
    mode(monkeypatch, "local")
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "3", TopupMethod.CASH).id)
    wallets._post(db, w.id, EntryType.HOLD, D("-1"), D("1"), "ghost", "hold", "ghost")
    db.commit()
    mode(monkeypatch, "shadow")
    with pytest.raises(ma.BaselineError, match="ACTIVE holds"):
        ma.create_baseline(db, w.id, "op")
    with pytest.raises(ma.BaselineError, match="created_by"):
        ma.create_baseline(db, w.id, "")


@pytest.mark.parametrize(
    "case", ["no_entitlement", "two_sms_products", "email_only", "withdrawn_only"]
)
def test_baseline_refuses_ambiguous_or_missing_product_mapping(db, monkeypatch, case):
    w = mk_world(db, "5", None, sms_entitlement=case == "two_sms_products")
    if case == "two_sms_products":
        db.add(Entitlement(enterprise_id=EID, assignment_id=uuid.uuid4(), product_id=uuid.uuid4(),
                           product_code="sms2", channel="sms", status="active", revision=1))  # fmt: skip
    if case == "email_only":
        db.add(Entitlement(enterprise_id=EID, assignment_id=uuid.uuid4(), product_id=EMAIL_P,
                           product_code="email", channel="email", status="active", revision=1))  # fmt: skip
    if case == "withdrawn_only":
        db.add(Entitlement(enterprise_id=EID, assignment_id=uuid.uuid4(), product_id=SMS_P,
                           product_code="sms", channel="sms", status="withdrawn", revision=1))  # fmt: skip
    db.commit()
    mode(monkeypatch, "shadow")
    with pytest.raises(ma.BaselineError, match="mapping"):
        ma.create_baseline(db, w.id, "op")


def test_new_baseline_supersedes_an_unmatched_one_but_not_a_matched_one(db, monkeypatch):
    w, b1 = baseline_world(db, monkeypatch)
    b2 = ma.create_baseline(db, w.id, "op-2", now=datetime(2030, 2, 1, tzinfo=UTC))
    db.commit()
    db.refresh(b1)
    assert (b1.status, b2.status) == ("superseded", "active") and b1.superseded_at is not None
    gid = uuid.uuid4()
    apply(db, [gev("issued", gid, "12", purpose="bootstrap", ref=b2.baseline_ref)])
    assert grant_row(db, gid).status == G_MATCHED
    with pytest.raises(ma.BaselineError, match="already has a matched"):
        ma.create_baseline(db, w.id, "op-3")


# =============================================================================================================
# C. bootstrap
# =============================================================================================================


def test_bootstrap_matches_without_any_balance_change_and_never_doubles(db, monkeypatch):
    w, b = baseline_world(db, monkeypatch)
    before, n = bal(db, w), ledger_count(db, w)
    gid = uuid.uuid4()
    e = gev("issued", gid, "12", purpose="bootstrap", ref=b.baseline_ref)
    r = apply(db, [e])
    g = grant_row(db, gid)
    assert (r.applied, g.status) == (1, G_MATCHED)
    assert bal(db, w) == before == (D("10"), D("2"))  # 12, jo 24
    assert sum(bal(db, w)) == D("12")
    assert ledger_count(db, w) == n + 1
    le = db.get(LedgerEntry, g.ledger_entry_id)
    assert (le.entry_type, le.available_delta, le.held_delta) == (EntryType.GRANT, D("0"), D("0"))
    assert (le.idempotency_key, le.ref_type, le.ref_id) == (f"grant:{gid}", "grant", str(gid))
    # replay: no-op
    r2 = apply(db, [e], next_seq=e.seq, init=False) if False else None
    cur = ms.get_cursor(db)
    cur.last_seq = 0
    db.commit()
    r2 = apply(db, [e])
    assert (r2.applied, r2.noop) == (0, 1) and ledger_count(db, w) == n + 1 and bal(db, w) == before


def test_bootstrap_stays_valid_after_normal_traffic_changes_current_gross(db, monkeypatch):
    w, b = baseline_world(db, monkeypatch)
    h = db.scalar(select(Hold))
    wallets.capture(db, h.id)  # trafik normal: gross 12 → 10
    wallets.adjustment(db, w.id, "-0.5", "n", "valid negative")
    db.commit()
    assert sum(bal(db, w)) == D("9.5")
    gid = uuid.uuid4()
    apply(db, [gev("issued", gid, "12", purpose="bootstrap", ref=b.baseline_ref)])
    assert grant_row(db, gid).status == G_MATCHED  # amount == baseline.gross_at_cutover
    assert sum(bal(db, w)) == D("9.5")


@pytest.mark.parametrize("case", ["amount", "currency", "product", "enterprise", "unknown_ref", "inactive",
                                  "mint_after", "hash"])  # fmt: skip
def test_bootstrap_mismatch_is_fail_closed_with_no_wallet_mutation(db, monkeypatch, case):
    w, b = baseline_world(db, monkeypatch)
    before, n = bal(db, w), ledger_count(db, w)
    kw = dict(purpose="bootstrap", ref=b.baseline_ref)
    amount = "12"
    if case == "amount":
        amount = "12.000001"
    elif case == "currency":
        kw["cur"] = "USD"
    elif case == "product":
        kw["prod"] = uuid.uuid4()
    elif case == "enterprise":
        kw["ent"] = uuid.uuid4()
    elif case == "unknown_ref":
        kw["ref"] = "9" * 64
    elif case == "inactive":
        b.status, b.superseded_at = "superseded", NOW
        db.commit()
    elif case == "mint_after":
        mode(monkeypatch, "local")
        wallets.confirm_topup(db, wallets.create_topup(db, w.id, "1", TopupMethod.CASH).id)
        db.commit()
        mode(monkeypatch, "shadow")
        before = bal(db, w)
        n = ledger_count(db, w)
    elif case == "hash":
        from sqlalchemy import text

        db.execute(
            text("UPDATE sms_money_baselines SET available_at_cutover = 9, gross_at_cutover = 11")
        )
        db.commit()
        db.expire_all()
    gid = uuid.uuid4()
    e = gev("issued", gid, amount, **kw)
    apply(db, [e])
    g = grant_row(db, gid)
    assert g.status == G_MISMATCH and g.ledger_entry_id is None, g.detail
    assert bal(db, w) == before and ledger_count(db, w) == n  # asnjë mutacion, asnjë rresht
    assert ms.get_cursor(db).last_seq == e.seq  # kursori përparon pas regjistrimit durabël
    assert ms.unresolved_count(db) == 1


def test_only_one_bootstrap_may_match_a_baseline_and_the_duplicate_is_a_mismatch(db, monkeypatch):
    w, b = baseline_world(db, monkeypatch)
    g1, g2 = uuid.uuid4(), uuid.uuid4()
    apply(db, [gev("issued", g1, "12", purpose="bootstrap", ref=b.baseline_ref),
               gev("issued", g2, "12", purpose="bootstrap", ref=b.baseline_ref)])  # fmt: skip
    assert grant_row(db, g1).status == G_MATCHED
    g = grant_row(db, g2)
    assert g.status == G_MISMATCH and "duplicate_bootstrap" in g.detail
    assert sum(bal(db, w)) == D("12")


# =============================================================================================================
# D. grant normal: shadow / central
# =============================================================================================================


def test_shadow_records_normal_grants_without_crediting_and_central_drains_them_in_order(
    db, monkeypatch
):
    w = mk_world(db)
    mode(monkeypatch, "shadow")
    a, b_ = uuid.uuid4(), uuid.uuid4()
    apply(db, [gev("issued", a, "5"), gev("issued", b_, "7")])
    assert [grant_row(db, x).status for x in (a, b_)] == [G_DEFERRED, G_DEFERRED]
    assert bal(db, w) == (
        D("10"),
        D("2"),
    )  # s'u shtua asgjë; fondet ekzistuese mbeten të shpenzueshme
    h = wallets.reserve(db, w.id, "1", "spend-in-shadow")
    wallets.capture(db, h.id)
    db.commit()
    mode(monkeypatch, "central")
    r = apply(db, [])  # faqe boshe: drain
    assert [grant_row(db, x).status for x in (a, b_)] == [G_APPLIED, G_APPLIED]
    assert bal(db, w) == (D("10") - D("1") + D("12"), D("2"))
    keys = list(db.scalars(select(LedgerEntry.idempotency_key).where(LedgerEntry.entry_type == EntryType.GRANT)
                           .order_by(LedgerEntry.id)))  # fmt: skip
    assert keys == [f"grant:{a}", f"grant:{b_}"]  # sipas issued_seq
    assert ms.drain_deferred(db) == 0  # idempotent
    assert wallets.verify_wallet(db, w.id) and r.applied == 0


def test_central_credits_immediately_and_replay_is_a_noop(db, monkeypatch):
    w = mk_world(db)
    mode(monkeypatch, "central")
    gid = uuid.uuid4()
    e = gev("issued", gid, "5.5")
    apply(db, [e])
    assert grant_row(db, gid).status == G_APPLIED and bal(db, w) == (D("15.5"), D("2"))
    ms.get_cursor(db).last_seq = 0
    db.commit()
    r = apply(db, [e])
    assert (r.applied, r.noop) == (0, 1) and bal(db, w) == (D("15.5"), D("2"))
    assert db.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.idempotency_key == f"grant:{gid}")) == 1  # fmt: skip


def test_first_central_grant_creates_the_empty_wallet(db, monkeypatch):
    mk_world(db, None, None)
    db.execute(Wallet.__table__.delete())
    db.commit()
    mode(monkeypatch, "central")
    gid = uuid.uuid4()
    apply(db, [gev("issued", gid, "3")])
    w = db.scalar(select(Wallet))
    assert (w.owner_ref, w.currency, w.enterprise_id) == ("acme", "EUR", EID)
    assert wallets.balances(db, w.id) == (D("3"), D("0"))


def test_money_amounts_keep_six_decimals_exactly(db, monkeypatch):
    w = mk_world(db, None, None)
    mode(monkeypatch, "central")
    apply(db, [gev("issued", uuid.uuid4(), "0.000001"), gev("issued", uuid.uuid4(), "0.000002")])
    assert wallets.balances(db, w.id)[0] == D("0.000003")


# =============================================================================================================
# E. mapimi
# =============================================================================================================


@pytest.mark.parametrize(
    "case", ["no_entitlement", "ambiguous", "email_product", "unknown_enterprise"]
)
def test_unmapped_grants_are_recorded_not_applied_and_block_readiness(db, monkeypatch, case):
    w = mk_world(db, "1", None, sms_entitlement=case != "no_entitlement")
    mode(monkeypatch, "central")
    kw = {}
    if case == "ambiguous":
        db.add(Entitlement(enterprise_id=EID, assignment_id=uuid.uuid4(), product_id=uuid.uuid4(),
                           product_code="sms2", channel="sms", status="active", revision=1))  # fmt: skip
        db.commit()
    if case == "email_product":
        db.add(Entitlement(enterprise_id=EID, assignment_id=uuid.uuid4(), product_id=EMAIL_P,
                           product_code="email", channel="email", status="active", revision=1))  # fmt: skip
        kw["prod"] = EMAIL_P
    if case == "unknown_enterprise":
        kw["ent"] = uuid.uuid4()
    gid = uuid.uuid4()
    apply(db, [gev("issued", gid, "9", **kw)])
    g = grant_row(db, gid)
    assert g.status == G_UNMAPPED and g.ledger_entry_id is None
    assert bal(db, w) == (D("1"), D("0")) and ms.unresolved_count(db) == 1


def test_unmapped_grant_is_promoted_when_the_mapping_becomes_valid(db, monkeypatch):
    w = mk_world(db, "1", None, sms_entitlement=False)
    mode(monkeypatch, "shadow")
    gid = uuid.uuid4()
    apply(db, [gev("issued", gid, "9")])
    assert grant_row(db, gid).status == G_UNMAPPED
    db.add(Entitlement(enterprise_id=EID, assignment_id=uuid.uuid4(), product_id=SMS_P,
                       product_code="sms", channel="sms", status="active", revision=1))  # fmt: skip
    db.commit()
    ms.drain_deferred(db)
    db.commit()
    assert grant_row(db, gid).status == G_DEFERRED and bal(db, w)[0] == D("1")
    mode(monkeypatch, "central")
    ms.drain_deferred(db)
    db.commit()
    assert grant_row(db, gid).status == G_APPLIED and bal(db, w)[0] == D("10")


# =============================================================================================================
# F. reversal
# =============================================================================================================


def test_reversal_before_credit_voids_the_deferred_grant(db, monkeypatch):
    w = mk_world(db)
    mode(monkeypatch, "shadow")
    gid = uuid.uuid4()
    apply(db, [gev("issued", gid, "5"), gev("reversed", gid, "5")])
    assert grant_row(db, gid).status == G_VOIDED
    mode(monkeypatch, "central")
    apply(db, [])
    assert (
        bal(db, w) == (D("10"), D("2")) and grant_row(db, gid).status == G_VOIDED
    )  # s'kreditohet kurrë


def test_reversal_debits_available_funds_once_and_replays_as_noop(db, monkeypatch):
    w = mk_world(db)
    mode(monkeypatch, "central")
    gid = uuid.uuid4()
    issued = gev("issued", gid, "5")
    rev = gev("reversed", gid, "5")
    apply(db, [issued, rev])
    g = grant_row(db, gid)
    assert g.status == G_REVERSED and bal(db, w) == (D("10"), D("2"))
    assert db.get(LedgerEntry, g.reversal_entry_id).entry_type == EntryType.GRANT_REVERSAL
    ms.get_cursor(db).last_seq = 0
    db.commit()
    r = apply(db, [issued, rev])
    assert r.applied == 0 and r.noop == 2 and bal(db, w) == (D("10"), D("2"))


def test_reversal_never_makes_the_wallet_negative_and_does_not_touch_active_holds(db, monkeypatch):
    w = mk_world(db, "12", "10")  # available 2, held 10
    mode(monkeypatch, "central")
    gid = uuid.uuid4()
    apply(db, [gev("issued", gid, "5")])
    assert bal(db, w) == (D("7"), D("10"))
    h = wallets.reserve(db, w.id, "6", "more")  # available 1, held 16
    db.commit()
    e = gev("reversed", gid, "5")
    r = apply(db, [e])
    g = grant_row(db, gid)
    assert (
        g.status == G_RECON and "insufficient available" in g.detail and g.reversal_entry_id is None
    )
    assert bal(db, w) == (D("1"), D("16"))  # holds aktive s'konsumohen
    assert (
        ms.get_cursor(db).last_seq == e.seq and r.error is None
    )  # kursori përparon pas regjistrimit
    assert ms.unresolved_count(db) == 1 and h.status.value == "active"


def test_matched_bootstrap_reversal_is_conservative_too(db, monkeypatch):
    w, b = baseline_world(db, monkeypatch)
    gid = uuid.uuid4()
    apply(db, [gev("issued", gid, "12", purpose="bootstrap", ref=b.baseline_ref)])
    apply(db, [gev("reversed", gid, "12", purpose="bootstrap", ref=b.baseline_ref)])
    g = grant_row(db, gid)
    assert g.status == G_RECON and bal(db, w) == (D("10"), D("2"))  # available 10 < 12: s'debiton


def test_reversal_of_unknown_grant_or_changed_identity_stops_the_cursor(db, monkeypatch):
    mk_world(db)
    mode(monkeypatch, "central")
    ok, bad_id = gev("issued", uuid.uuid4(), "1"), uuid.uuid4()
    r = apply(db, [ok, gev("reversed", bad_id, "1")])
    assert r.error and "unknown grant" in r.error
    cur = ms.get_cursor(db)
    assert (
        cur.last_seq == ok.seq and "unknown grant" in cur.last_error and cur.last_success_at is None
    )
    gid = uuid.UUID(ok.grant_id)
    r = apply(db, [gev("reversed", gid, "2")])  # shuma ndryshe nga issuance
    assert r.error and "identity differs" in r.error
    assert grant_row(db, gid).status == G_APPLIED


# =============================================================================================================
# G. idempotencë, konflikt, kursor, atomicitet
# =============================================================================================================


def test_same_grant_from_a_different_event_or_payload_is_a_conflict_that_stops_the_cursor(
    db, monkeypatch
):
    w = mk_world(db)
    mode(monkeypatch, "central")
    gid = uuid.uuid4()
    good = gev("issued", gid, "5")
    apply(db, [good])
    for evil in (gev("issued", gid, "6"), gev("issued", gid, "5")):  # shumë tjetër; event_id tjetër
        extra = gev("issued", uuid.uuid4(), "1")
        r = apply(db, [evil, extra])
        assert r.error and "different event" in r.error
        assert ms.get_cursor(db).last_seq == good.seq  # asnjë ngjarje pas konfliktit s'kapërcehet
    assert bal(db, w) == (D("15"), D("2"))


def test_malformed_event_parses_to_nothing_and_applies_nothing(db):
    d = gev("issued", uuid.uuid4(), "5").to_dict()
    d["data"]["amount"] = 5.0
    with pytest.raises(v1.ContractError):
        ms.parse_events([gev("issued", uuid.uuid4(), "1").to_dict(), d])


def test_cursor_epoch_generation_order_and_local_mode_checks(db, monkeypatch):
    mk_world(db)
    mode(monkeypatch, "central")
    with pytest.raises(ms.CursorMismatch) as e:
        ms.apply_batch(db, epoch=EPOCH, authorization_generation=1, events=[], next_seq=1)
    assert e.value.reason == "no_cursor"
    ms.init_cursor(db, EPOCH, 1)
    db.commit()
    with pytest.raises(ms.CursorMismatch) as e:
        ms.apply_batch(db, epoch=uuid.uuid4(), authorization_generation=1, events=[], next_seq=1)
    assert e.value.reason == "epoch_mismatch"
    with pytest.raises(ms.CursorMismatch) as e:
        ms.apply_batch(db, epoch=EPOCH, authorization_generation=2, events=[], next_seq=1)
    assert e.value.reason == "generation_mismatch"
    a, b = gev("issued", uuid.uuid4(), "1", seq=5), gev("issued", uuid.uuid4(), "1", seq=4)
    with pytest.raises(ms.MoneySyncError, match="out of order"):
        ms.apply_batch(db, epoch=EPOCH, authorization_generation=1, events=[a, b], next_seq=9)
    with pytest.raises(ms.MoneySyncError, match="out of order"):
        ms.apply_batch(db, epoch=EPOCH, authorization_generation=1, events=[a], next_seq=4)
    db.rollback()
    apply(db, [], next_seq=10)
    with pytest.raises(ms.MoneySyncError, match="behind"):
        ms.apply_batch(db, epoch=EPOCH, authorization_generation=1, events=[], next_seq=3)
    db.rollback()
    mode(monkeypatch, "local")
    with pytest.raises(ms.MoneySyncError, match="local"):
        ms.apply_batch(db, epoch=EPOCH, authorization_generation=1, events=[], next_seq=11)


def test_empty_page_advances_the_cursor_to_next_seq_gap_safely(db, monkeypatch):
    mk_world(db)
    mode(monkeypatch, "shadow")
    r = apply(db, [], next_seq=42)
    cur = ms.get_cursor(db)
    assert (cur.last_seq, r.last_seq, cur.last_error) == (
        42,
        42,
        None,
    ) and cur.last_success_at is not None


def test_a_failure_inside_the_batch_rolls_everything_back(db, monkeypatch):
    w = mk_world(db)
    mode(monkeypatch, "central")
    ms.init_cursor(db, EPOCH, 1)
    db.commit()
    real, calls = wallets._post, []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom")
        return real(*a, **k)

    monkeypatch.setattr(wallets, "_post", flaky)
    # pysqlite nuk fillon transaksion para DML: pa këtë, RELEASE i SAVEPOINT-it të parë bën commit (artefakt
    # vetëm i SQLite; PostgreSQL është korrekt). Një UPDATE i parë e bën transaksionin real.
    db.execute(ms.MoneyCursor.__table__.update().values(last_seq=0))
    with pytest.raises(RuntimeError):
        ms.apply_batch(db, epoch=EPOCH, authorization_generation=1, next_seq=2, now=NOW,
                       events=[gev("issued", uuid.uuid4(), "1", seq=1), gev("issued", uuid.uuid4(), "1", seq=2)])  # fmt: skip
    db.rollback()
    assert (
        bal(db, w) == (D("10"), D("2"))
        and db.scalar(select(func.count()).select_from(MoneyGrant)) == 0
    )
    assert ms.get_cursor(db).last_seq == 0


def test_rebase_generation_replays_from_zero_idempotently_and_epoch_reset_is_explicit(
    db, monkeypatch
):
    w = mk_world(db)
    mode(monkeypatch, "central")
    gid = uuid.uuid4()
    e = gev("issued", gid, "5", seq=3)
    apply(db, [e])
    ms.rebase_generation(db, EPOCH, 2)
    db.commit()
    cur = ms.get_cursor(db)
    assert (cur.authorization_generation, cur.last_seq) == (2, 0)
    r = ms.apply_batch(db, epoch=EPOCH, authorization_generation=2, events=[e], next_seq=3, now=NOW)
    db.commit()
    assert (r.applied, r.noop) == (0, 1) and bal(db, w) == (D("15"), D("2"))
    with pytest.raises(ms.CursorMismatch):
        ms.rebase_generation(db, uuid.uuid4(), 3)  # epokë tjetër ≠ rebase automatik
    db.rollback()
    new_epoch = uuid.uuid4()
    ms.reset_epoch(db, new_epoch, 1)
    db.commit()
    assert ms.get_cursor(db).epoch == new_epoch and ms.get_cursor(db).last_seq == 0
