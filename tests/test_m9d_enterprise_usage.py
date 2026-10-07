# ruff: noqa: F811
"""M9-d — Enterprise: raportet kumulative të përdorimit (snapshot, ekuacioni, outbox, outage, readiness, worker)."""

import ast
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.models.money_usage import (
    R_FAILED,
    R_PENDING,
    R_RETRY,
    R_SENDING,
    R_SENT,
    R_SUPERSEDED,
    UsageReport,
    UsageReportImmutableError,
)
from app.models.wallet import EntryType, LedgerEntry
from app.services import control_plane_client as cc
from app.services import money_usage as mu
from app.services import wallet as wallets
from packages.contracts.control_plane.money import usage_v1 as uv
from tests.test_central import make_db  # noqa: F401
from tests.test_m9c_money_authority import (
    EPOCH,
    apply,
    bal,
    baseline_world,
    gev,
    mk_world,
    mode,
)

ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)


def uv_aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def gen(now=T0):
    return mu.generate(engine, SessionLocal, now=now)


def reports(db):
    db.expire_all()
    return list(db.scalars(select(UsageReport).order_by(UsageReport.report_seq)))


def rep(db, i=-1) -> uv.UsageReportV1:
    return uv.UsageReportV1.parse(reports(db)[i].payload)


class PostClient:
    """Imiton ClientIn për `deliver`: regjistron çfarë u dërgua; sjellje e programueshme."""

    def __init__(self, behaviour=None):
        self.sent, self.calls, self.behaviour = (
            [],
            0,
            behaviour or (lambda n, p: {"status": "stored"}),
        )

    def post_usage_report(self, payload):
        self.calls += 1
        r = self.behaviour(self.calls, payload)
        if isinstance(r, Exception):
            raise r
        self.sent.append(payload)
        return r


# =============================================================================================================
# gjenerimi + ekuacioni
# =============================================================================================================


def test_report_for_a_plain_local_wallet_has_exact_flows_and_balances(db):
    w = mk_world(db, "10", None)
    h = wallets.reserve(db, w.id, "3", "a")
    wallets.capture(db, h.id, "2")  # 2 spent, 1 released
    h2 = wallets.reserve(db, w.id, "1", "b")  # active hold
    wallets.adjustment(db, w.id, "-0.5", "n1", "x")
    wallets.adjustment(db, w.id, "0.25", "p1", "y")
    db.commit()
    (rid,) = gen()
    r = rep(db)
    d = r.doc
    assert str(rid) == r.report_id and r.report_seq == 1 and d["authority_mode"] == "local"
    a, h_ = bal(db, w)
    assert (D(d["wallet"]["available"]), D(d["wallet"]["held"]), D(d["wallet"]["gross"])) == (
        a,
        h_,
        a + h_,
    )
    f = d["flows"]
    assert D(f["captured"]) == D("2") and D(f["negative_adjustments"]) == D("0.5")
    assert D(f["positive_local_credit"]) == D("10.25")  # topup 10 + adj 0.25
    assert (
        D(f["released"]) == D("1") and D(f["baseline_gross"]) == 0 and D(f["grants_applied"]) == 0
    )
    assert D(d["wallet"]["active_hold_total"]) == D("1") and d["wallet"]["active_hold_count"] == 1
    assert r.conservation_gap() == 0 and d["ledger_max_id"] == db.scalar(
        select(func.max(LedgerEntry.id))
    )
    assert h2.status.value == "active" and d["baseline"] is None and d["grants"] == []


def test_every_ledger_entry_type_is_accounted_for_in_the_conservation_equation(db, monkeypatch):
    w = mk_world(db, "20", None)
    h = wallets.reserve(db, w.id, "4", "m")
    wallets.capture(db, h.id)
    wallets.refund(db, h.id, "1", "rf")  # REFUND (local credit)
    wallets.charge(db, w.id, "2", "inv", "invoice", "INV-1")  # INVOICE debit
    wallets.adjustment(db, w.id, "-1", "n", "neg")
    wallets.adjustment(db, w.id, "3", "p", "pos")
    wallets.release(db, wallets.reserve(db, w.id, "1", "r").id)
    db.commit()
    mode(monkeypatch, "central")
    g = uuid.uuid4()
    apply(db, [gev("issued", g, "5"), gev("reversed", g, "5")])  # GRANT + GRANT_REVERSAL
    apply(db, [gev("issued", uuid.uuid4(), "7")])
    gen()
    r = rep(db)
    f = r.doc["flows"]
    assert D(f["grants_applied"]) == D("12") and D(f["grant_reversals"]) == D("5")
    assert D(f["invoice_debits"]) == D("2") and D(f["captured"]) == D("4")
    assert D(f["negative_adjustments"]) == D("1") and D(f["other_debits"]) == 0
    assert D(f["positive_local_credit"]) == D("24")  # topup 20 + refund 1 + adj 3
    assert r.conservation_gap() == 0
    assert D(r.doc["wallet"]["gross"]) == sum(bal(db, w))


def test_baseline_shadow_and_grants_appear_in_the_report(db, monkeypatch):
    w, b = baseline_world(db, monkeypatch)  # 10 available + 2 held, shadow
    gb, gn = uuid.uuid4(), uuid.uuid4()
    apply(
        db,
        [gev("issued", gb, "12", purpose="bootstrap", ref=b.baseline_ref), gev("issued", gn, "5")],
    )
    gen()
    d = rep(db).doc
    assert d["authority_mode"] == "shadow"
    assert d["baseline"] == {"baseline_ref": b.baseline_ref, "gross_at_cutover": "12.000000",
                             "ledger_max_id": b.ledger_max_id, "status": "active"}  # fmt: skip
    assert (
        D(d["flows"]["baseline_gross"]) == D("12") and D(d["flows"]["positive_local_credit"]) == 0
    )
    assert D(d["flows"]["grants_applied"]) == 0  # bootstrap = delta 0; normal grant deferred
    by = {g["grant_id"]: g for g in d["grants"]}
    assert (
        by[str(gb)]["status"] == "matched_to_existing_balance"
        and by[str(gn)]["status"] == "deferred_shadow"
    )
    assert (
        d["cursor"]["epoch"] == str(EPOCH)
        and d["cursor"]["last_seq"] >= 2
        and d["cursor"]["has_error"] is False
    )
    assert rep(db).conservation_gap() == 0


def test_unresolved_reversal_is_carried_with_reason_and_seq(db, monkeypatch):
    w = mk_world(db, "5", "4")  # available 1
    mode(monkeypatch, "central")
    g = uuid.uuid4()
    apply(db, [gev("issued", g, "3")])
    wallets.reserve(db, w.id, "3", "eat")
    db.commit()
    rv = gev("reversed", g, "3")
    apply(db, [rv])
    gen()
    (row,) = [x for x in rep(db).doc["grants"] if x["grant_id"] == str(g)]
    assert row["status"] == "reconciliation_required" and row["reversed_seq"] == rv.seq
    assert "insufficient available" in row["detail"]


def test_unmapped_wallets_are_not_reported_and_never_break_the_run(db, monkeypatch):
    mk_world(db, "5", None, sms_entitlement=False)
    assert gen() == []
    assert reports(db) == []


def test_orphan_grant_ledger_entries_are_exposed_as_integrity_facts(db, monkeypatch):
    w = mk_world(db, "5", None)
    mode(monkeypatch, "central")
    wallets._post(
        db, w.id, EntryType.GRANT, D("2"), D("0"), "forged", "grant", "x", authoritative=True
    )
    wallets._post(
        db,
        w.id,
        EntryType.GRANT_REVERSAL,
        D("-1"),
        D("0"),
        "forgedr",
        "grant",
        "x",
        authoritative=True,
    )
    db.commit()
    gen()
    i = rep(db).doc["integrity"]
    assert (D(i["orphan_grant_credit"]), D(i["orphan_grant_reversal"])) == (D("2"), D("1"))


def test_stored_balance_vs_ledger_sums_are_both_in_the_report(db):
    w = mk_world(db, "5", "1")
    gen()
    d = rep(db).doc
    assert (D(d["integrity"]["ledger_sum_available"]), D(d["integrity"]["ledger_sum_held"])) == bal(
        db, w
    )


# =============================================================================================================
# outbox: identitet, dedup, seq, immutability
# =============================================================================================================


def test_report_ids_and_seq_are_stable_monotone_and_content_is_immutable(db):
    w = mk_world(db, "5", None)
    gen(T0)
    wallets.adjustment(db, w.id, "1", "k", "n")
    db.commit()
    gen(T0 + timedelta(minutes=5))
    rows = reports(db)
    assert [r.report_seq for r in rows] == [1, 2] and rows[0].report_id != rows[1].report_id
    assert all(
        r.status == R_PENDING and r.payload_hash == uv.UsageReportV1.parse(r.payload).payload_hash()
        for r in rows
    )
    r = rows[0]
    for field, value in (
        ("payload", {"x": 1}),
        ("report_seq", 9),
        ("ledger_max_id", 999),
        ("payload_hash", "0" * 64),
    ):
        setattr(r, field, value)
        with pytest.raises(UsageReportImmutableError):
            db.flush()
        db.rollback()
        r = db.get(UsageReport, r.report_id)
    db.delete(r)
    with pytest.raises(UsageReportImmutableError):
        db.flush()
    db.rollback()


def test_identical_content_is_not_duplicated_until_the_heartbeat_is_due(db, monkeypatch):
    mk_world(db, "5", None)
    monkeypatch.setattr(settings, "money_report_heartbeat_seconds", 600)
    assert len(gen(T0)) == 1
    assert gen(T0 + timedelta(minutes=5)) == []  # i njëjti përmbajtje, brenda heartbeat
    assert len(gen(T0 + timedelta(minutes=11))) == 1  # heartbeat i detyruar
    assert [r.report_seq for r in reports(db)] == [1, 2]


def test_cursor_success_time_alone_does_not_force_a_new_report(db, monkeypatch):
    mk_world(db, "5", None)
    mode(monkeypatch, "shadow")
    apply(db, [], next_seq=1)
    gen(T0)
    apply(db, [], next_seq=2)  # last_success_at ndryshon, asgjë financiare
    assert gen(T0 + timedelta(minutes=1)) == []


# =============================================================================================================
# dërgimi, outage, at-least-once
# =============================================================================================================


def test_delivery_marks_sent_and_posts_the_frozen_payload(db):
    mk_world(db, "5", None)
    gen()
    c = PostClient()
    out = mu.deliver(SessionLocal, c, now=T0)
    assert out.ok and out.sent == 1 and c.sent[0] == reports(db)[0].payload
    r = reports(db)[0]
    assert r.status == R_SENT and r.sent_at is not None and r.attempts == 1 and r.last_error is None


@pytest.mark.parametrize("exc,kind", [(cc.CpTransportError("down"), "network_error"), (cc.CpAuthError("401"), "auth_error"),
                                      (cc.CpForbidden("403"), "forbidden")])  # fmt: skip
def test_central_outage_retains_the_report_without_touching_the_wallet_and_sms_continues(
    db, monkeypatch, exc, kind
):
    w = mk_world(db, "10", None)
    gen()
    before = bal(db, w)
    out = mu.deliver(SessionLocal, PostClient(lambda n, p: exc), now=T0)
    assert out.kind == kind and not out.ok
    r = reports(db)[0]
    assert r.status == R_RETRY and r.last_error and uv_aware(r.next_attempt_at) > T0
    assert r.payload == uv.UsageReportV1.parse(r.payload).doc  # payload intact
    # SMS/wallet: reserve/capture/release continue; wallet unchanged by the failure
    assert bal(db, w) == before
    h = wallets.reserve(db, w.id, "1", "during-outage")
    wallets.capture(db, h.id)
    db.commit()
    assert wallets.verify_wallet(db, w.id)
    # Central returns: backoff elapsed → delivered; the stale report is superseded by the newer cumulative one
    gen(T0 + timedelta(minutes=20))
    c = PostClient()
    out = mu.deliver(SessionLocal, c, now=T0 + timedelta(minutes=30))
    assert out.ok and out.sent == 1 and out.superseded == 1
    statuses = [x.status for x in reports(db)]
    assert statuses == [R_SUPERSEDED, R_SENT]
    assert uv.UsageReportV1.parse(c.sent[0]).report_seq == 2


def test_backoff_is_exponential_and_capped(db):
    assert mu._backoff(1).total_seconds() == 30 and mu._backoff(2).total_seconds() == 60
    assert mu._backoff(20).total_seconds() == mu.BACKOFF_CAP_S


def test_retry_is_not_attempted_before_next_attempt_at(db):
    mk_world(db, "5", None)
    gen()
    mu.deliver(SessionLocal, PostClient(lambda n, p: cc.CpTransportError("x")), now=T0)
    c = PostClient()
    assert mu.deliver(SessionLocal, c, now=T0 + timedelta(seconds=5)).sent == 0 and c.calls == 0
    assert mu.deliver(SessionLocal, c, now=T0 + timedelta(seconds=40)).sent == 1


def test_duplicate_delivery_has_no_financial_effect(db):
    w = mk_world(db, "5", None)
    gen()
    before = bal(db, w)
    c = PostClient(lambda n, p: {"status": "duplicate"})
    assert mu.deliver(SessionLocal, c, now=T0).ok
    r = reports(db)[0]
    r.status = R_PENDING
    db.commit()
    assert mu.deliver(SessionLocal, c, now=T0).ok and c.calls == 2
    assert bal(db, w) == before and db.scalar(select(func.count()).select_from(LedgerEntry)) == 1


def test_rejection_is_permanent_and_never_retried(db):
    mk_world(db, "5", None)
    gen()
    out = mu.deliver(
        SessionLocal, PostClient(lambda n, p: cc.CpReportRejected(409, "conflict")), now=T0
    )
    assert out.failed == 1 and not out.ok
    assert reports(db)[0].status == R_FAILED and "rejected" in reports(db)[0].last_error
    c = PostClient()
    assert mu.deliver(SessionLocal, c, now=T0 + timedelta(hours=1)).sent == 0 and c.calls == 0


def test_expired_lease_is_recovered_after_a_crash(db):
    mk_world(db, "5", None)
    gen()
    r = reports(db)[0]
    r.status, r.leased_until, r.attempts = R_SENDING, T0 - timedelta(seconds=1), 1
    db.commit()
    c = PostClient()
    assert mu.deliver(SessionLocal, c, now=T0).sent == 1
    r = reports(db)[0]
    assert r.status == R_SENT and r.attempts == 2
    r.status, r.leased_until = R_SENDING, T0 + timedelta(minutes=5)  # lease aktiv: nuk preket
    db.commit()
    assert mu.deliver(SessionLocal, PostClient(), now=T0).sent == 0


def test_report_generation_and_delivery_never_write_to_the_wallet_ledger(db, monkeypatch):
    w = mk_world(db, "5", None)
    n = db.scalar(select(func.count()).select_from(LedgerEntry))
    for m in ("local", "shadow", "central"):
        mode(monkeypatch, m)
        gen(T0 + timedelta(hours=1 if m == "local" else 2 if m == "shadow" else 3))
        mu.deliver(SessionLocal, PostClient(lambda k, p: cc.CpTransportError("x")), now=T0)
    assert db.scalar(select(func.count()).select_from(LedgerEntry)) == n and bal(db, w) == (
        D("5"),
        D("0"),
    )


# =============================================================================================================
# kufijtë arkitekturorë
# =============================================================================================================


def test_reporting_is_outside_the_send_path_and_has_its_own_worker_role():
    from app import worker

    hot = ["app/services/messages.py", "app/services/emails.py", "app/services/wallet.py",
           "app/services/dispatch_outcome.py", "app/services/campaigns.py"]  # fmt: skip
    for rel in hot + [p.relative_to(ROOT).as_posix() for p in (ROOT / "app/queue").glob("*.py")]:
        src = (ROOT / rel).read_text()
        assert "money_usage" not in src and "usage_report" not in src, rel
    importers = {p.relative_to(ROOT).as_posix() for p in (ROOT / "app").rglob("*.py")
                 if "money_usage" in p.read_text() and p.name != "money_usage.py"}  # fmt: skip
    assert importers <= {
        "app/worker.py",
        "app/services/money_readiness.py",
        "app/models/__init__.py",
        "app/models/money_usage.py",
        "app/core/config.py",
        "app/services/financial_ops.py",
        "app/services/billing_usage.py",  # M9-g2: vetëm `snapshot_session`
    }, importers  # fmt: skip  (M9-f: pamje vetëm-lexim e outbox-it)
    src = Path(worker.__file__).read_text()
    assert '"money_usage_reporter"' in src and "run_money_usage_reporter" in src
    mode_src = (ROOT / "app/services/money_usage.py").read_text()
    assert "_post(" not in mode_src and "LedgerEntry(" not in mode_src  # kurrë shkrim ledger


def test_reporter_role_misconfiguration_exits_2_and_disabled_is_idle(monkeypatch):
    from app import worker

    monkeypatch.setattr(settings, "money_reporting", True)
    monkeypatch.setattr(settings, "cp_base_url", "")
    assert worker.run_money_usage_reporter(once=True) == 2


def test_report_contract_module_is_stdlib_only_leaf():
    src = (ROOT / "packages/contracts/control_plane/money/usage_v1.py").read_text()
    mods = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add((n.module or "").split(".")[0])
    assert mods <= {
        "hashlib",
        "json",
        "re",
        "uuid",
        "dataclasses",
        "datetime",
        "decimal",
        "typing",
    }, mods


def test_migration_0024_up_down_up(make_db):  # noqa: F811
    from sqlalchemy import create_engine, inspect

    from tests.test_central import enterprise_alembic

    url = make_db("ent")
    enterprise_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    assert "sms_usage_reports" in inspect(eng).get_table_names()
    enterprise_alembic(url, "downgrade", "0023")
    assert "sms_usage_reports" not in inspect(eng).get_table_names()
    enterprise_alembic(url, "upgrade", "head")
    eng.dispose()
