from decimal import Decimal as D

import pytest
from sqlalchemy import text

from app.models.wallet import EntryType, HoldStatus, LedgerEntry, LedgerImmutableError, TopupMethod
from app.services import wallet as svc


def funded(db, amount="10"):
    w = svc.create_wallet(db, "acct-1", "eur")
    t = svc.create_topup(db, w.id, amount, TopupMethod.CASH)
    svc.confirm_topup(db, t.id)
    db.commit()
    return w


def test_topup_confirm_is_idempotent(db):
    w = funded(db, "10.5")
    t = svc.create_topup(db, w.id, "5", TopupMethod.ELECTRONIC, external_ref="pay-1")
    svc.confirm_topup(db, t.id)
    svc.confirm_topup(db, t.id)  # retry
    db.commit()
    assert svc.balances(db, w.id) == (D("15.5"), D("0"))
    assert svc.verify_wallet(db, w.id)


def test_external_ref_dedup(db):
    w = svc.create_wallet(db, "a", "EUR")
    a = svc.create_topup(db, w.id, "5", TopupMethod.ELECTRONIC, external_ref="x")
    assert svc.create_topup(db, w.id, "5", TopupMethod.ELECTRONIC, external_ref="x").id == a.id
    with pytest.raises(svc.Conflict):
        svc.create_topup(db, w.id, "6", TopupMethod.ELECTRONIC, external_ref="x")


def test_rejects_float_and_bad_amounts(db):
    w = svc.create_wallet(db, "a", "EUR")
    for bad in (0.1, "0", "-1", "1.0000001", "NaN", "Infinity"):
        with pytest.raises(svc.InvalidAmount):
            svc.create_topup(db, w.id, bad, TopupMethod.CASH)


def test_decimal_exactness(db):
    w = svc.create_wallet(db, "a", "EUR")
    for i in range(10):
        t = svc.create_topup(db, w.id, "0.1", TopupMethod.CASH, external_ref=f"r{i}")
        svc.confirm_topup(db, t.id)
    db.commit()
    assert svc.balances(db, w.id)[0] == D("1.000000")


def test_reserve_capture_partial_releases_rest(db):
    w = funded(db, "10")
    h = svc.reserve(db, w.id, "3", "msg-1")
    assert svc.balances(db, w.id) == (D("7"), D("3"))
    svc.capture(db, h.id, "2")
    svc.capture(db, h.id, "2")  # retry
    db.commit()
    assert h.status == HoldStatus.CAPTURED
    assert svc.balances(db, w.id) == (D("8"), D("0"))
    assert svc.verify_wallet(db, w.id)


def test_reserve_idempotent_and_insufficient(db):
    w = funded(db, "5")
    a = svc.reserve(db, w.id, "5", "m")
    assert svc.reserve(db, w.id, "5", "m").id == a.id
    assert svc.balances(db, w.id) == (D("0"), D("5"))
    with pytest.raises(svc.InsufficientFunds):
        svc.reserve(db, w.id, "0.000001", "m2")
    with pytest.raises(svc.Conflict):
        svc.reserve(db, w.id, "4", "m")


def test_release_and_capture_exclusive(db):
    w = funded(db, "5")
    h = svc.reserve(db, w.id, "2", "m")
    svc.release(db, h.id)
    svc.release(db, h.id)
    assert svc.balances(db, w.id) == (D("5"), D("0"))
    with pytest.raises(svc.Conflict):
        svc.capture(db, h.id)
    h2 = svc.reserve(db, w.id, "2", "m2")
    svc.capture(db, h2.id)
    with pytest.raises(svc.Conflict):
        svc.release(db, h2.id)
    with pytest.raises(svc.InvalidAmount):
        svc.capture(db, svc.reserve(db, w.id, "1", "m3").id, "2")


def test_refund_after_capture_idempotent(db):
    w = funded(db, "5")
    h = svc.reserve(db, w.id, "2", "m")
    svc.capture(db, h.id)
    svc.refund(db, h.id, "2", "dlr-failed-m")
    svc.refund(db, h.id, "2", "dlr-failed-m")
    db.commit()
    assert svc.balances(db, w.id) == (D("5"), D("0"))


def test_adjustment_cannot_go_negative(db):
    w = funded(db, "1")
    with pytest.raises(svc.InsufficientFunds):
        svc.adjustment(db, w.id, "-2", "k", "oops")


def test_ledger_is_append_only(db):
    w = funded(db, "1")
    e = db.query(LedgerEntry).first()
    assert e.entry_type == EntryType.TOPUP
    e.note = "tamper"
    with pytest.raises(LedgerImmutableError):
        db.flush()
    db.rollback()
    e = db.query(LedgerEntry).first()
    db.delete(e)
    with pytest.raises(LedgerImmutableError):
        db.flush()
    db.rollback()
    assert w.id
    assert db.execute(text("select count(*) from sms_ledger_entries")).scalar() == 1
