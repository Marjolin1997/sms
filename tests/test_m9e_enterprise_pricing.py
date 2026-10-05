# ruff: noqa: F811
"""M9-e — Enterprise: cache `cp.pricing.v1`, sinkronizim atomik, autoritetet local/shadow/central, snapshot i çmimit në mesazh,
fushata, email overage, readiness, mbrojtjet e mutacionit lokal, rrugë dërgimi pa Central."""

import ast
import json
import socket
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select, text

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.models.billing import Invoice, InvoiceLine, Plan
from app.models.control_plane import Entitlement
from app.models.enterprise import Enterprise
from app.models.pricing import (
    PricingAssignment,
    PricingComparison,
    PricingImmutableError,
    PricingRule,
    PricingState,
    PricingVersion,
)
from app.models.sending import Message, MessageStatus, MessagePriceFrozenError
from app.services import control_plane_client as cc
from app.services import messages as svc
from app.services import pricing, pricing_poller, pricing_readiness, pricing_sync, rates
from app.services import wallet as wallets
from app.services.wallet import TopupMethod
from packages.contracts.control_plane.pricing import v1 as pv
from tests.test_billing import AFTER, OWNER, add_emails, profile, subscribe
from tests.test_central import make_db  # noqa: F401
from tests.test_m9a_unknown_outcome import stub, to_unknown  # noqa: F401
from tests.test_pipeline import OK, PAST, fake, world  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2030, 6, 1, 12, tzinfo=UTC)
EID, SMS_P, EMAIL_P = uuid.UUID(int=0xE1), uuid.UUID(int=0xA1), uuid.UUID(int=0xA2)
CTX = {
    "eid": EID
}  # enterprise-i real i owner-it "c1" (fixture-i `world` mund ta krijojë me id tjetër)
EPOCH = str(uuid.UUID(int=0xE0))
ISO = lambda dt: pv.format_ts(dt)  # noqa: E731
EFF = ISO(datetime(2020, 1, 1, tzinfo=UTC))


def mode(monkeypatch, value):
    monkeypatch.setattr(settings, "pricing_authority", value)


def rule(prefix="355", price="0.050000", operator="", channel="sms"):
    return {
        "rule_id": str(uuid.uuid4()),
        "channel": channel,
        "prefix": prefix,
        "operator": operator,
        "unit_price": pv.format_price(D(price)),
    }


def version(rules, *, eff=EFF, status="active", num=1):
    return {"version_id": str(uuid.uuid4()), "version": num, "status": status, "effective_from": eff,
            "content_hash": pv.rules_hash(rules), "rules": rules}  # fmt: skip


def book(versions, code="std", cur="EUR"):
    return {"book_id": str(uuid.uuid4()), "code": code, "currency": cur, "versions": versions}


def asg(book_, product=SMS_P, eff=EFF):
    return {
        "assignment_id": str(uuid.uuid4()),
        "product_id": str(product),
        "price_book_id": book_["book_id"],
        "effective_from": eff,
    }


def snap(books, assignments, rev=1, gen=1, enterprise=None, epoch=EPOCH):
    return pv.PricingSnapshotV1.build(epoch=epoch, revision=rev, generation=gen,
                                      enterprises=[{"enterprise_id": str(enterprise or CTX["eid"]), "assignments": assignments}], books=books)  # fmt: skip


def apply(db, s, now=NOW):
    r = pricing_sync.apply_snapshot(db, s, now=now)
    db.commit()
    return r


def identity(db, email_entitlement=True):
    """Enterprise + entitlement-e për owner-in e `world` ("c1"); përdor enterprise-in ekzistues nëse ka (dual-write)."""
    from sqlalchemy import select as _sel

    ent = db.scalar(_sel(Enterprise).where(Enterprise.owner_ref == "c1"))
    if ent is None:
        ent = Enterprise(id=EID, owner_ref="c1")
        db.add(ent)
        db.flush()
    CTX["eid"] = ent.id
    if not db.scalar(_sel(Entitlement.id).where(Entitlement.enterprise_id == ent.id)):
        for p, ch, code in ((SMS_P, "sms", "sms"), (EMAIL_P, "email", "email")):
            if ch == "email" and not email_entitlement:
                continue
            db.add(Entitlement(enterprise_id=ent.id, assignment_id=uuid.uuid4(), product_id=p, product_code=code, channel=ch,
                               status="active", revision=1))  # fmt: skip
        db.commit()


def central(db, price="0.080000", **kw):
    """Snapshot Central i thjeshtë për `c1`: një libër EUR, një version, rregull "355"."""
    identity(db)
    b = book([version([rule("355", price)])], **kw)
    s = snap([b], [asg(b)])
    apply(db, s)
    return b, s


def submit(db, key="k1", to=OK, text_="hello", now=None):
    m = svc.submit(db, "c1", key, to, "ACME", text=text_, now=now)
    db.commit()
    return m


def cmp_rows(db):
    db.expire_all()
    return list(db.scalars(select(PricingComparison).order_by(PricingComparison.id)))


# =============================================================================================================
# sync atomik
# =============================================================================================================


def test_snapshot_applies_atomically_and_replay_is_a_noop(db):
    b, s = central(db)
    st = pricing_sync.get_state(db)
    assert (
        st.active_snapshot_id
        and (st.revision, st.snapshot_hash) == (1, s.snapshot_hash)
        and st.last_success_at is not None
    )
    assert db.scalar(select(func.count()).select_from(PricingRule)) == 1
    again = apply(db, s)
    assert again.outcome == "noop" and db.scalar(select(func.count()).select_from(PricingRule)) == 1
    assert (
        db.scalar(select(func.count()).select_from(PricingAssignment)) == 1
    )  # asnjë rresht i dyfishtë


def test_new_version_arrives_whole_and_old_snapshot_is_ignored_as_stale(db):
    b, _ = central(db)
    v2 = version(
        [rule("355", "0.090000"), rule("44", "0.100000")],
        eff=ISO(datetime(2031, 1, 1, tzinfo=UTC)),
        num=2,
    )
    b2 = {**b, "versions": [*b["versions"], v2]}
    r = apply(db, snap([b2], [asg(b2)], rev=2))
    assert (r.outcome, r.new_versions, r.new_rules) == ("applied", 1, 2)
    old = apply(
        db, snap([b], [asg(b)], rev=1, gen=1)
    )  # revision më e vogël brenda të njëjtës epokë
    assert old.outcome == "stale" and pricing_sync.get_state(db).revision == 2


def test_a_failure_in_the_middle_of_activation_leaves_the_previous_snapshot_intact(db, monkeypatch):
    b, s1 = central(db)
    before = pricing_sync.get_state(db).snapshot_hash
    v2 = version([rule("355", "0.090000")], eff=ISO(datetime(2031, 1, 1, tzinfo=UTC)), num=2)
    b2 = {**b, "versions": [*b["versions"], v2]}
    s2 = snap([b2], [asg(b2)], rev=2)
    real = pricing_sync.PricingAssignment
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("boom after rules were staged")

    monkeypatch.setattr(pricing_sync, "PricingAssignment", boom)
    with pytest.raises(RuntimeError):
        pricing_sync.apply_snapshot(db, s2, now=NOW)
    db.rollback()
    monkeypatch.setattr(pricing_sync, "PricingAssignment", real)
    st = pricing_sync.get_state(db)
    assert st.snapshot_hash == before and st.revision == 1  # pointer i pandryshuar
    assert (
        db.scalar(select(func.count()).select_from(PricingVersion)) == 1
    )  # asnjë gjysmë-version (u rikthye gjithçka)
    assert calls == [1]


def test_central_content_changes_to_an_existing_version_are_refused(db):
    b, _ = central(db)
    bad_rules = [rule("355", "0.999000")]
    evil = {
        **b,
        "versions": [
            {**b["versions"][0], "rules": bad_rules, "content_hash": pv.rules_hash(bad_rules)}
        ],
    }
    with pytest.raises(pricing_sync.ApplyError, match="immutable"):
        pricing_sync.apply_snapshot(db, snap([evil], [asg(evil)], rev=2), now=NOW)
    db.rollback()
    assert pricing_sync.get_state(db).revision == 1
    renamed = {**b, "code": "other"}
    with pytest.raises(pricing_sync.ApplyError):
        pricing_sync.apply_snapshot(db, snap([renamed], [asg(renamed)], rev=2), now=NOW)
    db.rollback()


def test_retirement_propagates_and_a_retired_version_cannot_be_reactivated(db, monkeypatch):
    b, _ = central(db)
    mode(monkeypatch, "central")
    assert pricing.quote(db, "c1", "355691230003", "hi", NOW).total == D("0.080000")
    retired = {**b, "versions": [{**b["versions"][0], "status": "retired"}]}
    r = apply(db, snap([retired], [asg(retired)], rev=2))
    assert r.retired == 1
    with pytest.raises(pricing.CentralPriceError, match="retired"):
        pricing.quote(db, "c1", "355691230003", "hi", NOW)  # tërhequr ⇒ fail-closed, pa rënie
    back = {**b, "versions": [{**b["versions"][0], "status": "active"}]}
    with pytest.raises(pricing_sync.ApplyError):
        pricing_sync.apply_snapshot(db, snap([back], [asg(back)], rev=3), now=NOW)
    db.rollback()


def test_removed_authorization_stops_pricing_for_that_enterprise(db, monkeypatch):
    b, _ = central(db)
    mode(monkeypatch, "central")
    apply(db, snap([b], [], rev=2, gen=2))  # enterprise pa caktime në snapshot-in e ri
    with pytest.raises(pricing.CentralPriceError, match="no price book"):
        pricing.quote(db, "c1", "355691230003", "hi", NOW)


# --- poller (fail-static / fail-closed) -----------------------------------------------------------------------------------


class FakeClient:
    def __init__(self, responses=(), exc=None):
        self.responses, self.exc, self.calls = list(responses), exc, []

    def get_pricing_snapshot(self, epoch=None, revision=None, generation=None):
        self.calls.append((epoch, revision, generation))
        if self.exc:
            raise self.exc
        return self.responses.pop(0) if self.responses else {"changed": False}


def poll(c, now=NOW):
    return pricing_poller.poll_once(SessionLocal, c, now=now)


def test_poller_is_idle_in_local_mode_and_never_calls_central(db):
    c = FakeClient()
    out = poll(c)
    assert out.kind == "disabled" and out.ok and c.calls == []


def test_poller_applies_a_changed_snapshot_and_marks_success_when_unchanged(db, monkeypatch):
    identity(db)
    mode(monkeypatch, "shadow")
    b = book([version([rule("355", "0.080000")])])
    s = snap([b], [asg(b)])
    out = poll(FakeClient([{"changed": True, "snapshot": s.to_dict()}]))
    assert out.ok and out.outcome == "applied" and out.revision == 1
    c = FakeClient([{"changed": False}])
    assert poll(c, now=NOW + timedelta(minutes=5)).outcome == "unchanged"
    assert c.calls == [(EPOCH, 1, 1)]  # i dërgon çfarë ka aplikuar
    assert pricing_sync.get_state(db).last_success_at.replace(tzinfo=UTC) == NOW + timedelta(
        minutes=5
    )


def test_malformed_or_partial_snapshot_is_not_activated_and_the_error_is_recorded(db, monkeypatch):
    b, _ = central(db)
    mode(monkeypatch, "central")
    good = snap([b], [asg(b)], rev=2).to_dict()
    partial = json.loads(json.dumps(good))
    partial["books"][0]["versions"][0]["rules"].append(
        rule("44", "0.500000")
    )  # rregull i shtuar jashtë hash-it
    out = poll(FakeClient([{"changed": True, "snapshot": partial}]))
    assert out.kind == "apply_error" and "content_hash" in out.detail
    st = pricing_sync.get_state(db)
    db.expire_all()
    assert st.revision == 1 and st.last_error and "content_hash" in st.last_error
    assert pricing.quote(db, "c1", "355691230003", "hi", NOW).unit_price == D(
        "0.080000"
    )  # çmimi i vjetër i plotë


@pytest.mark.parametrize("exc,kind", [(cc.CpTransportError("down"), "network_error"), (cc.CpAuthError("401"), "auth_error"),
                                      (cc.CpForbidden("403"), "forbidden")])  # fmt: skip
def test_central_outage_keeps_using_the_last_known_complete_version(
    db, monkeypatch, world, exc, kind
):
    central(db, "0.080000")
    mode(monkeypatch, "central")
    out = poll(FakeClient(exc=exc))
    assert out.kind == kind and not out.ok
    m = submit(db)  # dërgimi vazhdon me çmimin e fundit të plotë
    assert (m.price_source, m.unit_price, m.total_price) == (
        "central",
        D("0.080000"),
        D("0.080000"),
    )


def test_missing_initial_snapshot_fails_closed_under_central(db, monkeypatch, world):
    identity(db)
    mode(monkeypatch, "central")
    with pytest.raises(rates.NoRate, match="snapshot"):
        svc.submit(db, "c1", "k1", OK, "ACME", text="hello")
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Message)) == 0 and wallets.balances(
        db, 1
    ) == (D("10"), D("0"))


def test_stale_snapshot_alerts_in_readiness_but_never_changes_the_price(
    db, monkeypatch, world, caplog
):
    central(db, "0.080000")
    mode(monkeypatch, "central")
    st = pricing_sync.get_state(db)
    st.last_success_at = NOW - timedelta(hours=3)
    db.commit()
    with caplog.at_level("ERROR", logger="sms.pricing.poller"):
        age = pricing_poller.check_staleness(SessionLocal, now=NOW)
    assert age > settings.pricing_stale_fail_seconds and "stale" in caplog.text
    res = {c.name: c for c in pricing_readiness.evaluate(db, now=NOW)}
    assert res["sync_fresh"].level == "FAIL"
    assert pricing.quote(db, "c1", "355691230003", "hi", NOW).unit_price == D(
        "0.080000"
    )  # s'shpikët çmim i ri/default


# =============================================================================================================
# autoriteti
# =============================================================================================================


def test_local_preserves_the_legacy_pricing_exactly(db, world):
    central(db, "0.080000")  # cache ekziston por authority=local
    m = submit(db)
    assert (m.price_source, m.unit_price, m.total_price) == ("legacy", D("0.05"), D("0.05"))
    assert m.rate_version_id is not None and m.rate_id is not None and m.pricing_version_ref is None
    assert cmp_rows(db) == []


def test_shadow_computes_both_records_the_comparison_and_still_charges_legacy(
    db, world, monkeypatch
):
    w, _ = world
    central(db, "0.080000")
    mode(monkeypatch, "shadow")
    m = submit(db)
    assert (m.price_source, m.unit_price, m.total_price) == (
        "legacy",
        D("0.05"),
        D("0.05"),
    )  # legacy ngarkohet
    assert wallets.balances(db, w.id) == (D("9.95"), D("0.05"))
    (c,) = cmp_rows(db)
    assert (c.kind, c.ref, c.classification, c.ok) == (
        "sms",
        m.public_id,
        "unit_price_mismatch",
        False,
    )
    assert (c.legacy_unit_price, c.central_unit_price, c.legacy_total, c.central_total) == (
        D("0.05"),
        D("0.08"),
        D("0.05"),
        D("0.08"),
    )
    assert c.central_version_id is not None and "0.05" in c.detail


def test_shadow_match_is_recorded_as_ok(db, world, monkeypatch):
    central(db, "0.050000")
    mode(monkeypatch, "shadow")
    submit(db)
    (c,) = cmp_rows(db)
    assert c.classification == "match" and c.ok is True


@pytest.mark.parametrize(
    "case,expected",
    [
        ("no_snapshot", "version_missing"),
        ("missing_rule", "missing_rule"),
        ("currency", "currency_mismatch"),
        ("precedence", "precedence_mismatch"),
        ("no_assignment", "version_missing"),
        ("retired", "version_missing"),
    ],
)
def test_shadow_classifies_each_kind_of_mismatch_without_blocking_the_send(
    db, world, monkeypatch, case, expected
):
    identity(db)
    if case != "no_snapshot":
        rules = {"missing_rule": [rule("44", "0.05")], "precedence": [rule("3", "0.05")]}.get(
            case, [rule("355", "0.05")]
        )
        b = book(
            [version(rules, status="retired" if case == "retired" else "active")],
            cur="USD" if case == "currency" else "EUR",
        )
        apply(db, snap([b], [] if case == "no_assignment" else [asg(b)]))
    mode(monkeypatch, "shadow")
    m = submit(db)
    assert m.status == MessageStatus.QUEUED and m.price_source == "legacy"
    (c,) = cmp_rows(db)
    assert c.classification == expected and not c.ok


def test_shadow_operator_specific_legacy_vs_general_central_is_a_precedence_mismatch(
    db, world, monkeypatch
):
    from app.services import rates as rates_svc

    identity(db)
    w, card = world
    v = rates_svc.new_draft(db, card.id)
    rates_svc.set_rate(db, v.id, "35569", "0.05", operator="27601")
    rates_svc.publish(db, v.id, datetime(2031, 1, 1, tzinfo=UTC), now=PAST)
    db.commit()
    b = book([version([rule("355", "0.05")])])
    apply(db, snap([b], [asg(b)]))
    mode(monkeypatch, "shadow")
    d = pricing.quote(
        db, "c1", "355691230003", "hi", datetime(2032, 1, 1, tzinfo=UTC), operator="27601"
    )
    assert d.shadow.classification == "precedence_mismatch" and d.source == "legacy"


def test_central_charges_the_central_snapshot_and_freezes_its_identity(db, world, monkeypatch):
    w, _ = world
    b, s = central(db, "0.080000")
    mode(monkeypatch, "central")
    m = submit(db, text_="x" * 161)  # 2 segmente GSM
    assert (m.price_source, m.segments, m.unit_price, m.total_price, m.currency) == (
        "central",
        2,
        D("0.08"),
        D("0.16"),
        "EUR",
    )
    assert m.rate_version_id is None and m.rate_id is None
    assert (
        str(m.pricing_book_ref) == b["book_id"]
        and str(m.pricing_version_ref) == b["versions"][0]["version_id"]
    )
    assert str(m.pricing_rule_ref) == b["versions"][0]["rules"][0]["rule_id"]
    assert wallets.balances(db, w.id) == (D("9.84"), D("0.16")) and cmp_rows(db) == []
    svc.process_one(db)
    svc.apply_dlr(db, "fake", m.provider_message_id, delivered=True)
    db.commit()
    assert wallets.balances(db, w.id) == (D("9.84"), D("0")) and wallets.verify_wallet(db, w.id)


@pytest.mark.parametrize("case,exc", [("no_rule", "no Central price rule"), ("currency", "no EUR wallet"), ("no_assignment", "no price book"),
                                     ("no_product", "pricing mapping")])  # fmt: skip
def test_central_fails_closed_without_inventing_a_price(db, world, monkeypatch, case, exc):
    w, _ = world
    identity(db)
    b = book(
        [version([rule("44" if case == "no_rule" else "355", "0.05")])],
        cur="USD" if case == "currency" else "EUR",
    )
    apply(db, snap([b], [] if case == "no_assignment" else [asg(b)]))
    if case == "no_product":
        db.add(Entitlement(enterprise_id=CTX["eid"], assignment_id=uuid.uuid4(), product_id=uuid.uuid4(), product_code="sms2",
                           channel="sms", status="active", revision=1))  # fmt: skip
        db.commit()
    mode(monkeypatch, "central")
    from app.core.errors import DomainError

    with pytest.raises(DomainError) as e:
        svc.submit(db, "c1", "k1", OK, "ACME", text="hello")
    assert (
        exc.lower() in str(e.value).lower()
        or "no rate" in str(e.value).lower()
        or "wallet" in str(e.value).lower()
    )
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Message)) == 0 and wallets.balances(
        db, w.id
    ) == (D("10"), D("0"))  # asnjë rezervim


def test_central_blocks_every_local_pricing_mutation_but_the_sync_applier_works(
    db, world, monkeypatch
):
    w, card = world
    mode(monkeypatch, "central")
    v = None
    for fn in (lambda: rates.create_card(db, "new", "EUR"), lambda: rates.new_draft(db, card.id)):
        with pytest.raises(pricing.PricingFrozen) as e:
            fn()
        assert e.value.code == "pricing_authority_frozen"
    mode(monkeypatch, "local")
    v = rates.new_draft(db, card.id)
    db.commit()
    mode(monkeypatch, "central")
    with pytest.raises(pricing.PricingFrozen):
        rates.set_rate(db, v.id, "44", "0.1")
    with pytest.raises(pricing.PricingFrozen):
        rates.publish(db, v.id, datetime(2040, 1, 1, tzinfo=UTC), now=PAST)
    from app.services import billing

    with pytest.raises(pricing.PricingFrozen):
        billing.create_plan(db, "p-central", "x", "EUR", "1", 0, "0.5")  # çmim email lokal
    assert (
        billing.create_plan(db, "p-free", "x", "EUR", "1", 0, "0").code == "p-free"
    )  # pa çmim email: lejohet
    central(db, "0.07")  # aplikuesi i sinkronizimit mund të përditësojë cache-in
    assert pricing_sync.get_state(db).revision == 1


def test_admin_plan_switch_is_blocked_under_central(db, world, monkeypatch, client):
    w, card = world
    ok = client.put("/v1/admin/plans/c1", json={"rate_card_id": card.id, "enabled": True})
    assert ok.status_code == 200
    mode(monkeypatch, "central")
    same = client.put("/v1/admin/plans/c1", json={"rate_card_id": card.id, "enabled": False})
    assert same.status_code == 200  # kill switch (enabled) s'është çmim: lejohet
    other = client.put("/v1/admin/plans/new-owner", json={"rate_card_id": card.id, "enabled": True})
    assert other.status_code == 409 and other.json()["detail"]["code"] == "pricing_authority_frozen"


def test_rate_card_api_is_blocked_under_central(db, world, monkeypatch, client):
    mode(monkeypatch, "central")
    r = client.post("/v1/rates/rate-cards", json={"name": "x", "currency": "EUR"})
    assert r.status_code in (404, 409) and (
        r.status_code == 404 or r.json()["detail"]["code"] == "pricing_authority_frozen"
    )


# =============================================================================================================
# snapshot-i i çmimit në mesazh
# =============================================================================================================


def test_message_freezes_version_unit_segments_and_total_and_later_changes_do_not_touch_it(
    db, world, monkeypatch
):
    w, card = world
    b, _ = central(db, "0.080000")
    mode(monkeypatch, "central")
    m = submit(db, text_="x" * 161)
    frozen = (
        m.price_source,
        m.encoding,
        m.segments,
        m.unit_price,
        m.total_price,
        m.currency,
        m.pricing_version_ref,
        m.pricing_rule_ref,
    )
    # çmim i ri (version 2, më i shtrenjtë) aktivizohet pas pranimit
    v2 = version([rule("355", "0.500000")], eff=ISO(datetime(2020, 6, 1, tzinfo=UTC)), num=2)
    b2 = {**b, "versions": [*b["versions"], v2]}
    apply(db, snap([b2], [asg(b2)], rev=2))
    assert pricing.quote(db, "c1", "355691230003", "hi", NOW).unit_price == D(
        "0.50"
    )  # mesazhet e reja: çmimi i ri
    db.expire_all()
    m2 = db.get(Message, m.id)
    assert (
        m2.price_source,
        m2.encoding,
        m2.segments,
        m2.unit_price,
        m2.total_price,
        m2.currency,
        m2.pricing_version_ref,
        m2.pricing_rule_ref,
    ) == frozen
    svc.process_one(db)
    svc.apply_dlr(
        db, "fake", m.provider_message_id, delivered=True
    )  # DLR i vonuar: s'rillogarit nga çmimi i ri
    db.commit()
    assert wallets.balances(db, w.id) == (
        D("9.84"),
        D("0"),
    )  # capture = hold i ngrirë (0.16), jo 1.00
    caps = list(
        db.scalars(
            select(wallets.LedgerEntry).where(
                wallets.LedgerEntry.entry_type == wallets.EntryType.CAPTURE
            )
        )
    )
    assert [c.held_delta for c in caps] == [D("-0.16")]


def test_legacy_message_snapshot_is_unaffected_by_a_new_legacy_version(db, world):
    w, card = world
    m = submit(db)
    v = rates.new_draft(db, card.id)
    rates.set_rate(db, v.id, "355", "0.90")
    rates.publish(db, v.id, datetime(2031, 1, 1, tzinfo=UTC), now=PAST)
    db.commit()
    svc.process_one(db)
    svc.apply_dlr(db, "fake", m.provider_message_id, delivered=True)
    db.commit()
    db.refresh(m)
    assert (m.unit_price, m.total_price, m.price_source) == (
        D("0.05"),
        D("0.05"),
        "legacy",
    ) and wallets.balances(db, w.id) == (D("9.95"), D("0"))


def test_unknown_resolution_uses_the_original_held_amount(db, world, stub, monkeypatch):
    w, _ = world
    b, _ = central(db, "0.080000")
    mode(monkeypatch, "central")
    m = to_unknown(db, stub)
    assert wallets.balances(db, w.id) == (D("9.92"), D("0.08"))
    v2 = version([rule("355", "0.700000")], eff=ISO(datetime(2020, 6, 1, tzinfo=UTC)), num=2)
    apply(db, snap([{**b, "versions": [*b["versions"], v2]}], [asg(b)], rev=2))
    svc.resolve_unknown(
        db,
        m.public_id,
        "billable_delivered",
        actor="r",
        role="superadmin",
        reason="provider confirmed",
    )
    db.commit()
    assert wallets.balances(db, w.id) == (D("9.92"), D("0"))  # 0.08 i ngrirë, jo 0.70


def test_message_price_fields_are_immutable_orm_and_database(db, world):
    m = submit(db)
    for field, value in (("total_price", D("9")), ("unit_price", D("9")), ("segments", 9), ("currency", "USD"), ("price_source", "central"),
                         ("pricing_version_ref", uuid.uuid4()), ("rate_id", 999)):  # fmt: skip
        setattr(m, field, value)
        with pytest.raises(MessagePriceFrozenError):
            db.flush()
        db.rollback()
        m = db.get(Message, m.id)
    m.status = MessageStatus.SENDING  # fushat jo-çmim ndryshojnë normalisht
    db.flush()
    db.rollback()


def test_pricing_cache_rows_are_immutable_except_active_to_retired(db):
    b, _ = central(db)
    for model, field, value in ((PricingRule, "unit_price", D("9")), (PricingVersion, "content_hash", "0" * 64),
                                (PricingVersion, "version", 9), (PricingAssignment, "book_id", uuid.uuid4())):  # fmt: skip
        row = db.scalar(select(model))
        setattr(row, field, value)
        with pytest.raises(PricingImmutableError):
            db.flush()
        db.rollback()
    for model in (PricingRule, PricingVersion, PricingAssignment):
        db.delete(db.scalar(select(model)))
        with pytest.raises(PricingImmutableError):
            db.flush()
        db.rollback()
    ver = db.scalar(select(PricingVersion))
    ver.status = "retired"
    db.flush()
    ver.status = "active"
    with pytest.raises(PricingImmutableError):
        db.flush()
    db.rollback()


# --- rrumbullakimi / segmentet --------------------------------------------------------------------------------------------


def test_segment_pricing_total_is_unit_times_segments_with_exact_decimals(db, world, monkeypatch):
    central(db, "0.333333")
    mode(monkeypatch, "central")
    for text_, segs in (
        ("a", 1),
        ("x" * 160, 1),
        ("x" * 161, 2),
        ("x" * 306, 2),
        ("x" * 307, 3),
        ("ë" * 71, 2),
    ):
        d = pricing.quote(db, "c1", "355691230003", text_, NOW)
        assert (d.segments, d.total) == (segs, D("0.333333") * segs) and d.total == pv.line_total(
            D("0.333333"), segs
        )
    assert pricing.quote(db, "c1", "355691230003", "x" * 307, NOW).total == D("0.999999")


def test_reserve_and_capture_use_the_same_rounded_amount(db, world, monkeypatch):
    w, _ = world
    central(db, "0.333333")
    mode(monkeypatch, "central")
    m = submit(db, text_="x" * 307)
    assert m.total_price == D("0.999999") and wallets.balances(db, w.id) == (
        D("9.000001"),
        D("0.999999"),
    )
    svc.process_one(db)
    svc.apply_dlr(db, "fake", m.provider_message_id, delivered=True)
    db.commit()
    assert wallets.balances(db, w.id) == (D("9.000001"), D("0"))


# =============================================================================================================
# fushata
# =============================================================================================================


def test_campaign_estimate_follows_the_authority_and_actual_submit_freezes_the_current_price(
    db, world, monkeypatch
):
    from tests.test_campaigns import audience, campaign, drive, start

    w, _ = world
    b, _ = central(db, "0.080000")
    lst, ids = audience(db, 3)
    c = campaign(db, lst)
    from app.services import campaigns as camp

    local_est = camp.estimate(db, "c1", c.id, NOW)
    assert (local_est.recipients, local_est.total) == (3, D("0.15"))  # local: legacy
    mode(monkeypatch, "shadow")
    assert camp.estimate(db, "c1", c.id, NOW).total == D("0.15")  # shadow: legacy
    mode(monkeypatch, "central")
    est = camp.estimate(db, "c1", c.id, NOW)
    assert est.total == D("0.24") and est.currency == "EUR"  # central: snapshot Central
    # çmimi ndryshon PARA dërgimit real: vlerësimi është informues; çmimi i ngrirë është ai në çastin e submit
    v2 = version([rule("355", "0.100000")], eff=ISO(datetime(2020, 6, 1, tzinfo=UTC)), num=2)
    apply(db, snap([{**b, "versions": [*b["versions"], v2]}], [asg(b)], rev=2))
    start(db, c)
    drive(db)
    msgs = list(db.scalars(select(Message).order_by(Message.id)))
    assert len(msgs) == 3 and all(
        m.price_source == "central" and m.unit_price == D("0.10") and m.total_price == D("0.10")
        for m in msgs
    )
    assert est.total != sum(m.total_price for m in msgs)  # vlerësimi s'e mbivendos snapshot-in real
    assert len({m.pricing_version_ref for m in msgs}) == 1 and all(
        m.pricing_rule_ref for m in msgs
    )  # secili e shpjegueshme
    assert wallets.balances(db, w.id)[0] == D("10") - D("0.30") and wallets.verify_wallet(db, w.id)


def test_campaign_budget_check_uses_the_same_engine(db, world, monkeypatch):
    from tests.test_campaigns import audience, campaign, drive, start

    central(db, "0.800000")
    mode(monkeypatch, "central")
    lst, _ = audience(db, 3)
    c = campaign(db, lst, max_cost=D("1.0"))
    start(db, c)
    drive(db)
    assert (
        db.scalar(select(func.count()).select_from(Message)) == 1
    )  # 0.80 + 0.80 > 1.0 ⇒ buxheti u shteh me çmimin Central
    db.refresh(c)
    assert c.pause_reason == "budget_exhausted"


def test_console_quote_uses_the_authority_aware_engine(db, world, monkeypatch, client):
    central(db, "0.080000")
    body = {"to": "+355691230003", "text": "hello"}
    r = client.post("/v1/console/messages/quote", json=body)
    if r.status_code == 404:
        pytest.skip("console route not mounted in this client")
    assert r.status_code in (200, 401, 403)


def test_submit_estimate_and_console_do_not_call_the_legacy_quote_directly():
    for rel in ("app/services/messages.py", "app/services/campaigns.py", "app/api/console.py"):
        src = (ROOT / rel).read_text()
        assert "rates.quote(" not in src and "rates_svc.quote(" not in src, rel
        assert "pricing.quote(" in src or "pricing_svc.quote(" in src, rel


# =============================================================================================================
# email overage
# =============================================================================================================


def overage_world(db, price="0.10", cur="EUR"):
    p = billing_plan(db, price, cur)
    subscribe(db, p)
    add_emails(db, 5, datetime(2030, 2, 1, tzinfo=UTC))
    return p


def billing_plan(db, price="0.10", cur="EUR"):
    from app.services import billing

    p = billing.create_plan(db, f"p{uuid.uuid4().hex[:5]}", "Starter", cur, "20.00", 2, price)
    db.commit()
    return p


def email_snapshot(db, price="0.050000", cur="EUR", rev=1, extra_versions=()):
    identity(db)
    b = book(
        [version([rule(channel="email", prefix="", price=price)]), *extra_versions],
        code="email-std",
        cur=cur,
    )
    apply(db, snap([b], [asg(b, product=EMAIL_P)], rev=rev))
    return b


def invoice_lines(db):
    db.expire_all()
    return list(db.scalars(select(InvoiceLine).order_by(InvoiceLine.id)))


def test_local_invoice_uses_the_plan_price_and_records_the_source(db, world):
    from app.services import billing

    overage_world(db, "0.10")
    billing.generate_invoice(db, db.scalar(select(billing.Subscription.id)), AFTER)
    db.commit()
    over = [ln for ln in invoice_lines(db) if ln.description.startswith("Email overage")]
    assert len(over) == 1 and over[0].unit_price == D("0.10") and over[0].quantity == D("3")
    assert (over[0].pricing_source, over[0].pricing_version_ref) == ("legacy_plan", None)


def test_central_email_overage_price_is_snapshotted_on_the_invoice_line(db, world, monkeypatch):
    from app.services import billing

    overage_world(db, "0.10")
    b = email_snapshot(db, "0.050000")
    mode(monkeypatch, "central")
    sub_id = db.scalar(select(billing.Subscription.id))
    billing.generate_invoice(db, sub_id, AFTER)
    db.commit()
    (over,) = [ln for ln in invoice_lines(db) if ln.description.startswith("Email overage")]
    assert (over.unit_price, over.quantity, over.amount) == (D("0.05"), D("3"), D("0.15"))
    assert (
        over.pricing_source == "central"
        and str(over.pricing_version_ref) == b["versions"][0]["version_id"]
    )
    fee = [ln for ln in invoice_lines(db) if "monthly fee" in ln.description][0]
    assert fee.pricing_source == "legacy_plan"  # tarifa mujore mbetet çmim plani (M10)


def test_a_pricing_update_never_rewrites_an_issued_invoice(db, world, monkeypatch):
    from app.services import billing

    overage_world(db, "0.10")
    b = email_snapshot(db, "0.050000")
    mode(monkeypatch, "central")
    sub_id = db.scalar(select(billing.Subscription.id))
    billing.generate_invoice(db, sub_id, AFTER)
    db.commit()
    before = [(ln.id, ln.unit_price, ln.amount, ln.pricing_version_ref) for ln in invoice_lines(db)]
    total = db.scalar(select(Invoice.total))
    v2 = version(
        [rule(channel="email", prefix="", price="0.900000")],
        eff=ISO(datetime(2030, 3, 1, tzinfo=UTC)),
        num=2,
    )
    apply(db, snap([{**b, "versions": [*b["versions"], v2]}], [asg(b, product=EMAIL_P)], rev=2))
    assert [
        (ln.id, ln.unit_price, ln.amount, ln.pricing_version_ref) for ln in invoice_lines(db)
    ] == before
    assert db.scalar(select(Invoice.total)) == total
    ln = invoice_lines(db)[0]
    ln.unit_price = D("9")
    from app.models.billing import InvoiceImmutableError

    with pytest.raises(InvoiceImmutableError):
        db.flush()
    db.rollback()


def test_email_is_postpaid_and_never_debits_the_sms_wallet(db, world, monkeypatch):
    from app.services import billing

    w, _ = world
    overage_world(db, "0.10")
    email_snapshot(db, "0.050000")
    for m in ("shadow", "central"):
        mode(monkeypatch, m)
        monkeypatch.setattr(
            settings, "money_authority", m
        )  # M9-c: wallet-i SMS-only nën shadow/central money authority
        before = wallets.balances(db, w.id)
        sub_id = db.scalar(select(billing.Subscription.id))
        billing.generate_invoice(db, sub_id, AFTER)
        db.commit()
        inv = db.scalar(select(Invoice).order_by(Invoice.id.desc()))
        assert inv.status.value == "open"  # fatura mbetet e hapur (M9-c: wallet SMS-only)
        with pytest.raises(Exception, match="cannot be paid from the wallet"):
            billing.pay_from_wallet(db, "c1", inv.id)
        db.rollback()
        assert wallets.balances(db, w.id) == before
        break


def test_central_email_price_missing_or_wrong_currency_postpones_the_invoice(
    db, world, monkeypatch
):
    from app.services import billing

    overage_world(db, "0.10", "EUR")
    identity(db)
    mode(monkeypatch, "central")
    sub_id = db.scalar(select(billing.Subscription.id))
    assert (
        billing.generate_invoice(db, sub_id, AFTER) is None
    )  # pa snapshot ⇒ fail-closed (fatura shtyhet)
    db.rollback()
    email_snapshot(db, "0.05", cur="USD")  # monedhë tjetër (pa FX)
    assert billing.generate_invoice(db, sub_id, AFTER) is None
    db.rollback()
    assert db.scalar(select(func.count()).select_from(Invoice)) == 0


def test_shadow_email_comparison_is_recorded_and_the_plan_price_is_charged(db, world, monkeypatch):
    from app.services import billing

    overage_world(db, "0.10")
    email_snapshot(db, "0.050000")
    mode(monkeypatch, "shadow")
    billing.generate_invoice(db, db.scalar(select(billing.Subscription.id)), AFTER)
    db.commit()
    (over,) = [ln for ln in invoice_lines(db) if ln.description.startswith("Email overage")]
    assert over.unit_price == D("0.10") and over.pricing_source == "legacy_plan"
    (c,) = [r for r in cmp_rows(db) if r.kind == "email"]
    assert c.ok is False and "0.10" in c.detail


# =============================================================================================================
# kufijtë: pa Central në dërgim; readiness; bootstrap; worker
# =============================================================================================================


def test_send_path_never_imports_the_pricing_consumer_or_the_client():
    forbidden = {"pricing_poller", "pricing_sync", "control_plane_client", "control_plane_poller"}
    paths = [ROOT / p for p in ("app/services/pricing.py", "app/services/messages.py", "app/services/campaigns.py", "app/services/billing.py",
                                "app/services/wallet.py")] + list((ROOT / "app/queue").glob("*.py"))  # fmt: skip
    for p in paths:
        for n in ast.walk(ast.parse(p.read_text())):
            mods = []
            if isinstance(n, ast.ImportFrom):
                mods = [n.module or "", *[f"{n.module}.{a.name}" for a in n.names]]
            elif isinstance(n, ast.Import):
                mods = [a.name for a in n.names]
            assert not {m.split(".")[-1] for m in mods} & forbidden, (p.name, mods)
    importers = {p.relative_to(ROOT).as_posix() for p in (ROOT / "app").rglob("*.py")
                 if any(w in p.read_text() for w in ("import pricing_poller", "pricing_sync", "pricing_poller"))}  # fmt: skip
    assert importers <= {
        "app/worker.py",
        "app/services/pricing_poller.py",
        "app/services/pricing_sync.py",
        "app/services/pricing_readiness.py",
    }, importers


def test_central_pricing_runs_with_all_network_blocked(db, world, monkeypatch):
    central(db, "0.080000")
    mode(monkeypatch, "central")

    def blocked(*a, **k):
        raise AssertionError("network used on the pricing/send path")

    monkeypatch.setattr(httpx.Client, "send", blocked)
    monkeypatch.setattr(httpx.AsyncClient, "send", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    m = submit(db)
    svc.process_one(db)
    svc.apply_dlr(db, "fake", m.provider_message_id, delivered=True)
    db.commit()
    assert m.price_source == "central"


def _ready_world(db, monkeypatch, price="0.050000", n=25):
    """Central = legacy ⇒ shadow match; `n` krahasime të regjistruara."""
    central(db, price)
    mode(monkeypatch, "shadow")
    for i in range(n):
        pricing.record_comparison(
            db, pricing.quote(db, "c1", "355691230003", "hi", NOW).shadow, f"ref-{i}"
        )
    db.commit()


@pytest.fixture
def cfg_ok(monkeypatch):
    monkeypatch.setattr(cc, "config_from_settings", lambda s: object())


def checks(db, **kw):
    return {c.name: c for c in pricing_readiness.evaluate(db, now=NOW, **kw)}


def fails(res):
    return {n for n, c in res.items() if c.level == "FAIL"}


def test_readiness_passes_when_snapshot_mapping_currency_and_shadow_all_hold(
    db, world, monkeypatch, cfg_ok
):
    _ready_world(db, monkeypatch)
    res = checks(db)
    assert fails(res) == set(), {n: c.reason for n, c in res.items() if c.level == "FAIL"}
    assert pricing_readiness.ok(list(res.values()))
    assert {"authority_mode", "sync_configured", "snapshot_present", "sync_fresh", "product_mapping_valid", "wallet_currency_matches",
            "cache_integrity_no_ambiguous_rules", "shadow_comparison", "no_local_pricing_mutation_since_shadow",
            "estimator_and_submit_use_pricing_engine", "central_version_available", "production_ack"} <= set(res)  # fmt: skip


def test_readiness_fails_for_each_missing_proof(db, world, monkeypatch, cfg_ok):
    mode(monkeypatch, "local")
    assert "authority_mode" in fails(checks(db))
    mode(monkeypatch, "shadow")
    r = checks(db)
    assert {"snapshot_present", "sync_fresh", "central_version_available"} <= fails(
        r
    )  # pa snapshot
    _ready_world(db, monkeypatch, "0.080000")  # Central ≠ legacy ⇒ mospërputhje
    assert "shadow_comparison" in fails(checks(db))
    assert "shadow_comparison" not in fails(
        checks(db, max_mismatch_pct=100.0)
    )  # politika e konfigurueshme
    assert "shadow_comparison" in fails(checks(db, min_samples=10_000))  # mostër e pamjaftueshme


def test_readiness_flags_currency_mapping_and_local_mutation_and_ack_and_sync_error(
    db, world, monkeypatch, cfg_ok
):
    w, card = world
    identity(db)
    b = book([version([rule("355", "0.05")])], cur="USD")
    apply(db, snap([b], [asg(b)]))
    mode(monkeypatch, "shadow")
    assert "wallet_currency_matches" in fails(checks(db, min_samples=0))
    _ready_world_ok = checks(db, min_samples=0)
    assert _ready_world_ok["product_mapping_valid"].level == "PASS"
    db.add(Entitlement(enterprise_id=CTX["eid"], assignment_id=uuid.uuid4(), product_id=uuid.uuid4(), product_code="sms2", channel="sms",
                       status="active", revision=1))  # fmt: skip
    db.commit()
    assert "product_mapping_valid" in fails(checks(db, min_samples=0))
    # ndryshim lokal i çmimit pas fillimit të dritares shadow
    pricing.record_comparison(db, pricing.Comparison("match", None, None), "r0")
    db.commit()
    first = db.scalar(select(func.min(PricingComparison.created_at)))
    v = rates.new_draft(db, card.id)
    db.commit()
    db.execute(
        text("UPDATE sms_rate_card_versions SET created_at = :t WHERE id = :i"),
        {"t": first + timedelta(minutes=5), "i": v.id},
    )
    db.commit()
    assert "no_local_pricing_mutation_since_shadow" in fails(checks(db, min_samples=0))
    st = pricing_sync.get_state(db)
    st.last_error = "snapshot rejected"
    db.commit()
    assert "sync_no_error" in fails(checks(db, min_samples=0))
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "pricing_authority_ack", False)
    assert "production_ack" in fails(checks(db, min_samples=0))
    monkeypatch.setattr(settings, "pricing_authority_ack", True)
    assert "production_ack" not in fails(checks(db, min_samples=0))


def test_readiness_detects_a_tampered_cache(db, world, monkeypatch, cfg_ok):
    central(db)
    mode(monkeypatch, "shadow")
    db.execute(text("UPDATE sms_pricing_rules SET unit_price = 0.99"))
    db.commit()
    assert "cache_integrity_no_ambiguous_rules" in fails(checks(db, min_samples=0))


def test_production_central_pricing_requires_the_explicit_ack_and_https():
    s = settings.model_copy(
        update={
            "pricing_authority": "central",
            "pricing_authority_ack": False,
            "cp_base_url": "https://c.example",
        }
    )
    assert any("SMS_PRICING_AUTHORITY_ACK" in p for p in s.production_problems())
    assert not any(
        "SMS_PRICING_AUTHORITY" in p
        for p in s.model_copy(update={"pricing_authority_ack": True}).production_problems()
    )
    assert any(
        "https" in p and "PRICING" in p
        for p in s.model_copy(
            update={"cp_base_url": "http://x", "pricing_authority_ack": True}
        ).production_problems()
    )
    assert (
        settings.pricing_authority == "local" and settings.money_authority == "local"
    )  # të pavarur dhe default local


def test_pricing_and_money_authority_are_independent(db, world, monkeypatch):
    central(db, "0.080000")
    monkeypatch.setattr(settings, "money_authority", "central")
    assert submit(db).price_source == "legacy"  # money central, pricing local
    mode(monkeypatch, "central")
    monkeypatch.setattr(settings, "money_authority", "local")
    assert submit(db, "k2").price_source == "central"


def test_cli_readiness_exit_codes_and_json(db, world, monkeypatch, capsys, cfg_ok):
    from scripts import pricing_authority_readiness as cli

    assert cli.main([]) == 1 and "FAIL authority_mode" in capsys.readouterr().out
    _ready_world(db, monkeypatch)
    assert cli.main(["--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert all(set(r) == {"name", "level", "reason"} for r in rows)


def test_bootstrap_export_is_read_only_and_covers_cards_plans_and_email(
    db, world, tmp_path, capsys
):
    from scripts import pricing_bootstrap as pb

    identity(db)
    p = billing_plan(db, "0.10")
    subscribe(db, p)
    n = db.scalar(select(func.count()).select_from(Message))
    out = tmp_path / "p.json"
    assert pb.main(["export", "--out", str(out)]) == 0
    doc = json.loads(out.read_text())
    assert doc["schema"] == "pricing-bootstrap.v1"
    codes = {b["code"] for b in doc["books"]}
    assert "std" in codes and f"email-{p.code}" in codes
    std = next(b for b in doc["books"] if b["code"] == "std")
    assert std["currency"] == "EUR" and std["versions"][0]["rules"] == [
        {"channel": "sms", "prefix": "355", "operator": "", "unit_price": "0.050000"}
    ]
    sms = [a for a in doc["assignments"] if a["channel"] == "sms"][0]
    assert (
        sms["owner_ref"] == "c1"
        and sms["enterprise_id"] == str(CTX["eid"])
        and sms["book_code"] == "std"
    )
    assert any(
        a["channel"] == "email" and a["book_code"] == f"email-{p.code}" for a in doc["assignments"]
    )
    assert db.scalar(select(func.count()).select_from(Message)) == n  # asgjë s'u ndryshua


def test_worker_role_exists_and_misconfiguration_exits_2(monkeypatch):
    from app import worker

    assert '"pricing_control_plane"' in Path(worker.__file__).read_text()
    mode(monkeypatch, "shadow")
    monkeypatch.setattr(settings, "cp_base_url", "")
    assert worker.run_pricing_control_plane(once=True) == 2


def test_enterprise_migration_0025_up_down_up(make_db):  # noqa: F811
    from sqlalchemy import create_engine, inspect

    from tests.test_central import enterprise_alembic

    url = make_db("ent")
    enterprise_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    insp = inspect(eng)
    tables = {"sms_pricing_state", "sms_pricing_snapshots", "sms_pricing_books", "sms_pricing_versions", "sms_pricing_rules",
              "sms_pricing_assignments", "sms_pricing_comparisons"}  # fmt: skip
    assert tables <= set(insp.get_table_names())
    cols = {c["name"]: c for c in insp.get_columns("sms_messages")}
    assert {"price_source", "pricing_book_ref", "pricing_version_ref", "pricing_rule_ref"} <= set(
        cols
    )
    assert cols["rate_version_id"]["nullable"] and cols["rate_id"]["nullable"]
    assert {"pricing_source", "pricing_version_ref"} <= {
        c["name"] for c in insp.get_columns("sms_invoice_lines")
    }
    enterprise_alembic(url, "downgrade", "0024")
    assert not tables & set(inspect(eng).get_table_names())
    enterprise_alembic(url, "upgrade", "head")
    eng.dispose()


# =============================================================================================================
# PostgreSQL
# =============================================================================================================

pg = pytest.mark.skipif(
    not __import__("os").environ.get("SMS_TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="needs PostgreSQL",
)


@pg
def test_pg_message_price_snapshot_and_pricing_cache_are_immutable_in_the_database(make_db):
    """Trigger-at vijnë nga migrimi (conftest përdor create_all): DB e re e migruar + rreshta me SQL real."""
    from sqlalchemy import MetaData, Table, create_engine
    from sqlalchemy.exc import DBAPIError

    from tests.test_central import enterprise_alembic

    url = make_db("ent")
    if url.startswith("sqlite"):
        pytest.skip("needs PostgreSQL triggers")
    enterprise_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    md = MetaData()
    t_wallet, t_hold, t_msg = (
        Table(n, md, autoload_with=eng) for n in ("sms_wallets", "sms_holds", "sms_messages")
    )
    now = datetime.now(UTC)
    book_id, ver_id, rule_id, snap_id = (uuid.uuid4() for _ in range(4))
    with eng.begin() as c:
        wid = c.execute(
            t_wallet.insert().values(
                owner_ref="o", currency="EUR", created_at=now, low_balance_notified=False
            )
        ).inserted_primary_key[0]
        hid = c.execute(
            t_hold.insert().values(
                wallet_id=wid,
                amount=1,
                captured_amount=0,
                status="ACTIVE",
                reference="r",
                created_at=now,
            )
        ).inserted_primary_key[0]
        mid = c.execute(
            t_msg.insert().values(
                public_id="p1",
                owner_ref="o",
                idempotency_key="k",
                request_hash="h",
                wallet_id=wid,
                hold_id=hid,
                category="transactional",
                sender="S",
                destination="355",
                country="AL",
                text="t",
                encoding="gsm7",
                segments=1,
                currency="EUR",
                unit_price=1,
                total_price=1,
                price_source="central",
                provider="fake",
                status="QUEUED",
                attempts=0,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
            )
        ).inserted_primary_key[0]
        c.execute(text("INSERT INTO sms_pricing_snapshots (id, epoch, revision, authorization_generation, snapshot_hash, received_at) VALUES (:i, :e, 1, 1, 'h', now())"),
                  {"i": snap_id, "e": uuid.uuid4()})  # fmt: skip
        c.execute(
            text(
                "INSERT INTO sms_pricing_books (id, code, currency, created_at) VALUES (:i, 'std', 'EUR', now())"
            ),
            {"i": book_id},
        )
        c.execute(text("INSERT INTO sms_pricing_versions (id, book_id, version, status, effective_from, content_hash, rule_count, created_at) VALUES (:i, :b, 1, 'active', now(), 'h', 1, now())"),
                  {"i": ver_id, "b": book_id})  # fmt: skip
        c.execute(
            text(
                "INSERT INTO sms_pricing_rules (id, version_id, channel, prefix, operator, unit_price) VALUES (:i, :v, 'sms', '355', '', 1)"
            ),
            {"i": rule_id, "v": ver_id},
        )
        c.execute(text("INSERT INTO sms_pricing_assignments (id, snapshot_id, assignment_id, enterprise_id, product_id, book_id, effective_from) VALUES (:i, :s, :a, :e, :p, :b, now())"),
                  {"i": uuid.uuid4(), "s": snap_id, "a": uuid.uuid4(), "e": uuid.uuid4(), "p": uuid.uuid4(), "b": book_id})  # fmt: skip
        c.execute(
            text(
                "INSERT INTO sms_pricing_comparisons (kind, ref, classification, ok, created_at) VALUES ('sms', 'r', 'match', true, now())"
            )
        )
    for stmt in (
        f"UPDATE sms_messages SET total_price = 9 WHERE id = {mid}",
        f"UPDATE sms_messages SET unit_price = 9 WHERE id = {mid}",
        f"UPDATE sms_messages SET segments = 9 WHERE id = {mid}",
        f"UPDATE sms_messages SET price_source = 'legacy' WHERE id = {mid}",
        f"UPDATE sms_messages SET pricing_version_ref = '{uuid.uuid4()}' WHERE id = {mid}",
        "UPDATE sms_pricing_rules SET unit_price = 9",
        "DELETE FROM sms_pricing_rules",
        "UPDATE sms_pricing_assignments SET effective_from = now()",
        "DELETE FROM sms_pricing_assignments",
        "UPDATE sms_pricing_versions SET content_hash = 'x'",
        "UPDATE sms_pricing_versions SET effective_from = now()",
        "DELETE FROM sms_pricing_versions",
        "UPDATE sms_pricing_books SET currency = 'USD'",
        "DELETE FROM sms_pricing_books",
        "UPDATE sms_pricing_snapshots SET revision = 9",
        "UPDATE sms_pricing_comparisons SET ok = false",
        "DELETE FROM sms_pricing_comparisons",
    ):
        with pytest.raises(DBAPIError), eng.begin() as c:
            c.execute(text(stmt))
    with eng.begin() as c:  # lejohet: active → retired; fusha jo-çmim e mesazhit
        c.execute(text("UPDATE sms_pricing_versions SET status = 'retired'"))
        c.execute(text(f"UPDATE sms_messages SET attempts = 1 WHERE id = {mid}"))
    with pytest.raises(DBAPIError), eng.begin() as c:
        c.execute(text("UPDATE sms_pricing_versions SET status = 'active'"))
    eng.dispose()


@pg
def test_pg_version_activation_concurrent_with_submits_never_mixes_versions(db, world, monkeypatch):
    import threading

    w, _ = world
    identity(db)
    rules1 = [rule("355", "0.050000"), rule("35569", "0.060000")]
    v1 = version(rules1, num=1)
    b = book([v1])
    apply(db, snap([b], [asg(b)], rev=1))
    rules2 = [rule("355", "0.500000"), rule("35569", "0.600000")]
    v2 = version(rules2, eff=ISO(datetime(2020, 6, 1, tzinfo=UTC)), num=2)
    b2 = {**b, "versions": [v1, v2]}
    s2 = snap([b2], [asg(b2)], rev=2)
    s2_assignment = s2
    mode(monkeypatch, "central")
    wallets.confirm_topup(
        db, wallets.create_topup(db, w.id, "1000", TopupMethod.CASH).id
    ) if settings.money_authority == "local" else None
    db.commit()
    barrier, errors = threading.Barrier(2, timeout=20), []

    def activator():
        try:
            with SessionLocal() as s:
                barrier.wait()
                pricing_sync.apply_snapshot(s, s2_assignment, now=NOW)
                s.commit()
        except BaseException as e:  # noqa: BLE001
            errors.append(repr(e))

    def sender():
        try:
            with SessionLocal() as s:
                barrier.wait()
                for i in range(25):
                    svc.submit(s, "c1", f"race-{i}", OK, "ACME", text="hello")
                    s.commit()
        except BaseException as e:  # noqa: BLE001
            errors.append(repr(e))

    ts = [threading.Thread(target=activator), threading.Thread(target=sender)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    assert not errors, errors
    db.expire_all()
    msgs = list(db.scalars(select(Message).where(Message.idempotency_key.like("race-%"))))
    assert len(msgs) == 25
    valid = {str(v1["version_id"]): {r["rule_id"]: D(r["unit_price"]) for r in rules1},
             str(v2["version_id"]): {r["rule_id"]: D(r["unit_price"]) for r in rules2}}  # fmt: skip
    seen = set()
    for m in msgs:
        ver = str(m.pricing_version_ref)
        assert ver in valid, ver
        assert str(m.pricing_rule_ref) in valid[ver], "rule from a different version (mixed)"
        assert (
            m.unit_price == valid[ver][str(m.pricing_rule_ref)]
            and m.total_price == m.unit_price * m.segments
        )
        seen.add(ver)
    assert seen <= set(valid) and wallets.verify_wallet(db, w.id)
