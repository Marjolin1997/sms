# ruff: noqa: F811
"""M7-g — backfill i rate_limit_per_min nga AccountPlan legacy (Enterprise read-only, Central target)."""

import json

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from app.core.db import engine
from app.models.sending import AccountPlan
from apps.central.models import AuditLog, EnterpriseProduct, Product, SyncOutbox
from apps.central.services import enterprise_products as asg
from apps.central.tools import backfill_assignment_rate_limits as bf
from tests.test_central_bootstrap_products import (  # noqa: F401
    Seed,
    cen,
    central_setup,
    make_db,
    sdump,
    seed,
)


def src_url():
    return engine.url.render_as_string(hide_password=False)


def set_plan_limits(db, seed, key, sms=None, email=None):
    plan = db.scalar(select(AccountPlan).where(AccountPlan.owner_ref == seed.owner(key)))
    plan.rate_limit_per_min, plan.email_rate_limit_per_min = sms, email
    db.commit()


def assign_both(cen, seed, key, sms_limit=None, email_limit=None):
    eng = create_engine(cen)
    with Session(eng, expire_on_commit=False) as s:
        for code, lim in (("sms", sms_limit), ("email", email_limit)):
            p = s.scalar(select(Product).where(Product.code == code))
            ep, _ = asg.assign_product(s, seed.ids[key], p.id)
            if lim is not None:
                asg.set_rate_limit(s, seed.ids[key], ep.id, lim)
        s.commit()
    eng.dispose()


def central_state(cen):
    eng = create_engine(cen)
    with Session(eng) as s:
        out = {
            "limits": sorted(
                (str(ep.enterprise_id), p.code, ep.rate_limit_per_min, ep.revision)
                for ep, p in s.execute(
                    select(EnterpriseProduct, Product).join(
                        Product, Product.id == EnterpriseProduct.product_id
                    )
                )
            ),  # fmt: skip
            "outbox": s.scalar(select(func.count()).select_from(SyncOutbox)),
            "audit": s.scalar(select(func.count()).select_from(AuditLog)),
        }
    eng.dispose()
    return out


def run(cen, **kw):
    return bf.run(src_url(), cen, **kw)


def row(rep, seed, key, code):
    return next(
        r for r in rep.rows if r.enterprise_id == str(seed.ids[key]) and r.product_code == code
    )


@pytest.fixture
def world_(db, seed, cen):
    seed.enterprise("a")
    seed.plan("a")
    central_setup(cen, seed, ["a"])
    assign_both(cen, seed, "a")
    return seed


def test_sms_and_email_limits_are_backfilled_from_the_matching_legacy_field(db, world_, cen):
    set_plan_limits(db, world_, "a", sms=120, email=30)
    before = central_state(cen)
    rep = run(cen)  # dry-run: zero shkrime
    assert [row(rep, world_, "a", c).action for c in ("sms", "email")] == ["set", "set"]
    assert central_state(cen) == before and rep.written == 0
    rep = run(cen, apply=True)
    assert rep.ok and rep.written == 2
    st = central_state(cen)
    eid = str(world_.ids["a"])
    assert {(c, lim, rev) for e, c, lim, rev in st["limits"] if e == eid} == {
        ("sms", 120, 2),
        ("email", 30, 2),
    }
    assert (
        st["outbox"] == before["outbox"] + 2 and st["audit"] == before["audit"] + 2
    )  # revision/outbox/audit normal
    eng = create_engine(cen)
    with Session(eng) as s:
        a = list(s.scalars(select(AuditLog)))
        assert {x.actor_kind for x in a} == {"system"} and {x.actor_label for x in a} == {
            "system:rate_limit_backfill"
        }
        assert {x.action for x in a} == {"enterprise_product.rate_limit_backfill"}
        assert {tuple(sorted(x.detail["after"].items())) for x in a} == {
            (("rate_limit_per_min", 120),),
            (("rate_limit_per_min", 30),),
        }
        last = s.scalars(select(SyncOutbox).order_by(SyncOutbox.seq)).all()[-2:]
        assert sorted(x.payload["rate_limit_per_min"] for x in last) == [30, 120]  # rrjedh në feed
    eng.dispose()


def test_matching_is_noop_and_rerun_is_idempotent(db, world_, cen):
    set_plan_limits(db, world_, "a", sms=120, email=None)
    run(cen, apply=True)
    mid = central_state(cen)
    rep = run(cen, apply=True)
    assert rep.ok and rep.written == 0 and [r.action for r in rep.rows] == ["noop", "noop"]
    assert central_state(cen) == mid


def test_null_and_zero_legacy_inherit_default_and_never_persist_600(db, world_, cen):
    set_plan_limits(db, world_, "a", sms=None, email=0)
    rep = run(cen, apply=True)
    assert rep.ok and rep.written == 0 and {r.action for r in rep.rows} == {"noop"}
    assert all(lim is None for _, _, lim, _ in central_state(cen)["limits"])  # NULL ruhet NULL


def test_conflict_is_reported_never_overwritten_and_blocks_all_writes(db, world_, cen):
    set_plan_limits(db, world_, "a", sms=200, email=40)
    eng = create_engine(cen)
    with Session(eng, expire_on_commit=False) as s:
        ep = s.scalar(select(EnterpriseProduct).join(Product, Product.id == EnterpriseProduct.product_id)
                      .where(Product.code == "sms"))  # fmt: skip
        asg.set_rate_limit(s, ep.enterprise_id, ep.id, 999)  # Central ≠ legacy
        s.commit()
    eng.dispose()
    before = central_state(cen)
    rep = run(cen, apply=True)
    assert not rep.ok and row(rep, world_, "a", "sms").action == "conflict"
    assert row(rep, world_, "a", "email").action == "set" and rep.written == 0
    assert central_state(cen) == before  # as email s'u shkrua; sms mbeti 999


def test_missing_assignment_missing_enterprise_and_invalid_legacy(db, seed, cen):
    for k in ("noasg", "ghost", "badlim"):
        seed.enterprise(k)
        seed.plan(k)
    central_setup(cen, seed, ["noasg", "badlim"])  # "ghost" s'ekziston në Central
    assign_both(cen, seed, "badlim")
    set_plan_limits(db, seed, "noasg", sms=10, email=10)
    set_plan_limits(db, seed, "ghost", sms=10, email=10)
    db.execute(
        text("update sms_account_plans set rate_limit_per_min = -5 where owner_ref = :o"),
        {"o": seed.owner("badlim")},
    )
    db.commit()
    rep = run(cen)
    assert {row(rep, seed, "noasg", c).action for c in ("sms", "email")} == {"no_assignment"}
    assert {row(rep, seed, "ghost", c).action for c in ("sms", "email")} == {"invalid"}
    assert row(rep, seed, "badlim", "sms").action == "invalid"
    db.execute(
        text("update sms_account_plans set rate_limit_per_min = 2000000 where owner_ref = :o"),
        {"o": seed.owner("badlim")},
    )
    db.commit()
    assert row(run(cen), seed, "badlim", "sms").action == "invalid"
    assert not run(cen, apply=True).ok and central_state(cen)["audit"] == 0


def test_source_is_read_only_and_accountplan_unchanged(db, world_, cen, monkeypatch):
    set_plan_limits(db, world_, "a", sms=120, email=30)
    plans = sdump(db)
    seen = []
    real = bf.create_engine

    def spy(url, *a, **k):
        from sqlalchemy import event

        eng = real(url, *a, **k)
        event.listen(eng, "before_cursor_execute", lambda c, cur, stmt, *r: seen.append(stmt))
        return eng

    monkeypatch.setattr(bf, "create_engine", spy)
    run(cen, apply=True)
    assert "READ ONLY" in seen[0].upper() or "QUERY_ONLY" in seen[0].upper()
    assert not [
        s
        for s in seen
        if s.strip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER"}
    ]
    assert sdump(db) == plans  # AccountPlan (si legacy) e paprekur


def test_cli_dry_run_default_json_and_exit_codes(db, world_, cen, monkeypatch, capsys):
    set_plan_limits(db, world_, "a", sms=77, email=None)
    monkeypatch.setenv("ENTERPRISE_DATABASE_URL", src_url())
    monkeypatch.setattr(bf.settings, "database_url", cen)
    before = central_state(cen)
    assert bf.main(["--format", "json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["mode"] == "dry-run" and doc["counts"]["set"] == 1
    assert central_state(cen) == before
    assert bf.main(["--apply", "--dry-run"]) == 2
    assert bf.main(["--apply"]) == 0 and bf.main(["--apply"]) == 0
    monkeypatch.delenv("ENTERPRISE_DATABASE_URL")
    assert bf.main([]) == 2
