# ruff: noqa: F811
"""M9-e — Central: domain-i i çmimeve (libra, versione, rregulla, caktime), kërkimi, audit, immutability, feed `cp.pricing.v1`, import."""

import json
import threading
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from apps.central.core import errors
from apps.central.main import create_app
from apps.central.models import (
    AuditLog,
    CentralUser,
    PriceAssignment,
    PriceBook,
    PriceRule,
    PriceVersion,
)
from apps.central.models.pricing import PricingImmutableError
from apps.central.services import enterprises as ent
from apps.central.services import pricing, pricing_feed, pricing_import, service_auth, users
from apps.central.services import products as prod
from apps.central.tools import pricing_import as cli
from packages.contracts.control_plane.pricing import v1 as pv
from tests.test_central import IS_PG, central_alembic, make_db  # noqa: F401
from tests.test_central_auth import (
    PW,
    auth_secret,  # noqa: F401
)
from tests.test_central_sync_api import assertion, auth, keypair

T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)
DAY = timedelta(days=1)


@pytest.fixture
def env(make_db):
    url = make_db()
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    private, public = keypair()
    with Session(eng, expire_on_commit=False) as s:
        e1, e2, e3 = ent.create(s, "Acme"), ent.create(s, "Beta"), ent.create(s, "Gamma")
        sms = prod.create(s, "sms", "SMS", "sms")
        email = prod.create(s, "email", "Email", "email")
        admin = users.create_user(s, "a1@example.com", PW, "admin")
        op = users.create_user(s, "op@example.com", PW, "operator")
        for cid, scopes, ents in (("pr", ["pricing:read"], [e1.id, e2.id]), ("pr1", ["pricing:read"], [e1.id]),
                                  ("mon", ["money:read"], [e1.id]), ("syn", ["sync:read"], [e1.id])):  # fmt: skip
            service_auth.create_client(s, cid, scopes, ents)
            service_auth.add_key(s, cid, "k1", public)
        s.commit()
        ids = dict(
            e1=e1.id, e2=e2.id, e3=e3.id, sms=sms.id, email=email.id, admin=admin.id, op=op.id
        )
    c = TestClient(create_app(eng))
    c.eng, c.private, c.ids = eng, private, ids
    yield c
    eng.dispose()


def S(env):
    return Session(env.eng, expire_on_commit=False)


def adm(s, env):
    return s.get(CentralUser, env.ids["admin"])


def make_version(
    env,
    *,
    code="std",
    cur="EUR",
    rules=(("355", "", "0.050000"),),
    eff=T0 + DAY,
    now=T0,
    book=None,
    imported=False,
):
    """Libër (ose ekzistues) + draft + rregulla + aktivizim; → (book_id, version_id)."""
    with S(env) as s:
        a = adm(s, env)
        b = (
            pricing.get_book(s, book)
            if book
            else pricing.create_book(s, a, code, code.upper(), cur, now=now)
        )
        v = pricing.new_draft(s, a, b.id, now=now)
        for prefix, op, price in rules:
            pricing.set_rule(s, a, v.id, "sms", price, prefix=prefix, operator=op, now=now)
        pricing.activate(s, a, v.id, eff, now=now, imported=imported)
        s.commit()
        return b.id, v.id


def audit_actions(env, like="price_%"):
    with S(env) as s:
        return [
            r.action
            for r in s.scalars(
                select(AuditLog)
                .where(AuditLog.action.like(like))
                .order_by(AuditLog.created_at, AuditLog.id)
            )
        ]


def revision(env):
    with S(env) as s:
        return pricing.read_state(s)[1]


# =============================================================================================================
# domain
# =============================================================================================================


def test_create_book_validates_is_idempotent_and_audited_once(env):
    with S(env) as s:
        a = adm(s, env)
        b = pricing.create_book(s, a, "std", "Standard", "eur", now=T0)
        assert (b.code, b.currency) == ("std", "EUR")
        assert pricing.create_book(s, a, "std", "Standard", "EUR", now=T0).id == b.id  # no-op
        with pytest.raises(errors.Conflict):
            pricing.create_book(s, a, "std", "Other name", "EUR")
        with pytest.raises(errors.Conflict):
            pricing.create_book(s, a, "std", "Standard", "USD")  # asnjë FX/ndërrim monedhe
        for bad in ("A", "Bad Code", "-x", ""):
            with pytest.raises(errors.Invalid):
                pricing.create_book(s, a, bad, "x", "EUR")
        with pytest.raises(errors.Invalid):
            pricing.create_book(s, a, "ok1", "x", "EURO")
        s.commit()
    assert audit_actions(env) == ["price_book.create"]


def test_only_a_human_admin_can_mutate_pricing(env):
    with S(env) as s:
        op = s.get(CentralUser, env.ids["op"])
        with pytest.raises(errors.Forbidden):
            pricing.create_book(s, op, "std", "x", "EUR")


def test_version_lifecycle_deterministic_lookup_and_activation_rules(env):
    b, v = make_version(
        env,
        rules=(("355", "", "0.050000"), ("35569", "", "0.040000"), ("35569", "27601", "0.035000")),
    )
    with S(env) as s:
        row = pricing.get_version(s, v)
        assert (row.status, row.version) == ("active", 1) and row.content_hash == pv.rules_hash(
            pricing.rules_of(s, v)
        )
        at = T0 + 2 * DAY
        q = pricing.lookup(s, b, "sms", "+355691234567", at)
        assert q.unit_price == D("0.040000") and q.currency == "EUR" and q.version_id == v
        assert pricing.lookup(s, b, "sms", "+355691234567", at, "27601").unit_price == D("0.035000")
        assert pricing.lookup(s, b, "sms", "+355441234567", at).unit_price == D("0.050000")
        assert pricing.lookup(s, b, "sms", "+355691234567", at) == pricing.lookup(
            s, b, "sms", "+355691234567", at
        )  # i përsëritshëm
        with pytest.raises(pricing.NoPrice):
            pricing.lookup(s, b, "sms", "+355691234567", T0)  # para effective_from
        with pytest.raises(pricing.NoPrice):
            pricing.lookup(
                s, b, "sms", "+441234567890", at
            )  # pa rregull ⇒ fail-closed, pa parazgjedhje
        with pytest.raises(pricing.NoPrice):
            pricing.lookup(s, b, "email", "", at)


def test_activation_constraints_and_idempotency(env):
    with S(env) as s:
        a = adm(s, env)
        b = pricing.create_book(s, a, "std", "Std", "EUR", now=T0)
        v = pricing.new_draft(s, a, b.id, now=T0)
        with pytest.raises(errors.Conflict):
            pricing.activate(s, a, v.id, T0 + DAY, now=T0)  # version bosh
        with pytest.raises(errors.Conflict):
            pricing.new_draft(s, a, b.id, now=T0)  # një draft per libër
        pricing.set_rule(s, a, v.id, "sms", "0.05", prefix="355", now=T0)
        with pytest.raises(errors.Conflict):
            pricing.activate(s, a, v.id, T0 - DAY, now=T0)  # në të kaluarën
        pricing.activate(s, a, v.id, T0 + DAY, now=T0)
        again = pricing.activate(s, a, v.id, T0 + DAY, now=T0)  # i njëjti ⇒ no-op
        assert again.id == v.id
        with pytest.raises(errors.Conflict):
            pricing.activate(s, a, v.id, T0 + 2 * DAY, now=T0)  # tashmë aktiv me tjetër data
        v2 = pricing.new_draft(s, a, b.id, now=T0)
        assert v2.version == 2
        with pytest.raises(errors.Conflict):
            pricing.activate(
                s, a, v2.id, T0 + DAY, now=T0
            )  # jo pas versionit paraprak (renditje e rreptë)
        pricing.activate(s, a, v2.id, T0 + 2 * DAY, now=T0)
        s.commit()
    assert audit_actions(env).count("price_version.activate") == 2


def test_active_versions_are_immutable_and_correction_is_a_new_version(env):
    b, v1_ = make_version(env)
    with S(env) as s:
        a = adm(s, env)
        with pytest.raises(errors.Conflict):
            pricing.set_rule(s, a, v1_, "sms", "0.99", prefix="355", now=T0)
        with pytest.raises(errors.Conflict):
            pricing.remove_rule(s, a, v1_, "sms", prefix="355")
        # mbrojtja ORM e shtresës së dytë (rrugë e drejtpërdrejtë)
        r = s.scalar(select(PriceRule).where(PriceRule.version_id == v1_))
        r.unit_price = D("0.99")
        with pytest.raises(PricingImmutableError):
            s.flush()
        s.rollback()
        s.add(
            PriceRule(version_id=v1_, channel="sms", prefix="44", operator="", unit_price=D("0.1"))
        )
        with pytest.raises(PricingImmutableError):
            s.flush()
        s.rollback()
        ver = pricing.get_version(s, v1_)
        for field, value in (
            ("content_hash", "0" * 64),
            ("effective_from", T0 + 9 * DAY),
            ("version", 7),
        ):
            setattr(ver, field, value)
            with pytest.raises(PricingImmutableError):
                s.flush()
            s.rollback()
            ver = pricing.get_version(s, v1_)
        ver.status = "draft"
        with pytest.raises(PricingImmutableError):
            s.flush()
        s.rollback()
        # korrigjimi = draft i ri që kopjon rregullat; versioni i vjetër mbetet i lexueshëm dhe i pandryshuar
        d = pricing.new_draft(s, a, b, now=T0)
        assert {(r["prefix"], r["unit_price"]) for r in pricing.rules_of(s, d.id)} == {
            ("355", "0.050000")
        }
        pricing.set_rule(s, a, d.id, "sms", "0.060000", prefix="355", now=T0)
        pricing.activate(s, a, d.id, T0 + 5 * DAY, now=T0)
        s.commit()
    with S(env) as s:
        assert pricing.lookup(s, b, "sms", "+355691234567", T0 + 2 * DAY).unit_price == D(
            "0.050000"
        )  # historia e vjetër
        assert pricing.lookup(s, b, "sms", "+355691234567", T0 + 6 * DAY).unit_price == D(
            "0.060000"
        )
        assert pricing.lookup(s, b, "sms", "+355691234567", T0 + 2 * DAY).version_id == v1_


def test_version_switch_changes_only_new_lookups_atomically(env):
    b, v1_ = make_version(
        env, rules=(("355", "", "0.050000"), ("44", "", "0.070000")), eff=T0 + DAY
    )
    _, v2 = make_version(
        env, book=b, rules=(("355", "", "0.080000"), ("44", "", "0.090000")), eff=T0 + 5 * DAY
    )
    with S(env) as s:
        for number, old, new in (
            ("+355691234567", "0.050000", "0.080000"),
            ("+447911123456", "0.070000", "0.090000"),
        ):
            before = pricing.lookup(s, b, "sms", number, T0 + 4 * DAY)
            after = pricing.lookup(s, b, "sms", number, T0 + 5 * DAY)
            assert (str(before.unit_price), before.version_id) == (old, v1_) and (
                str(after.unit_price),
                after.version_id,
            ) == (new, v2)
        # asnjë përzierje: të dy kërkimet në të njëjtin çast marrin versionin e plotë të njëjtë
        a, b2 = (
            pricing.lookup(s, b, "sms", "+355691234567", T0 + 6 * DAY),
            pricing.lookup(s, b, "sms", "+447911123456", T0 + 6 * DAY),
        )
        assert a.version_id == b2.version_id == v2


def test_retired_versions_are_never_selected_and_there_is_no_silent_fallback(env):
    b, v1_ = make_version(env, eff=T0 + DAY, rules=(("355", "", "0.050000"),))
    _, v2 = make_version(env, book=b, eff=T0 + 3 * DAY, rules=(("355", "", "0.080000"),))
    with S(env) as s:
        a = adm(s, env)
        with pytest.raises(errors.Invalid):
            pricing.retire(s, a, v2, "")  # arsye e detyrueshme
        pricing.retire(s, a, v2, "wrong prices", now=T0 + 2 * DAY)
        assert pricing.retire(s, a, v2, "again", now=T0 + 2 * DAY).status == "retired"  # no-op
        s.commit()
    with S(env) as s:
        assert (
            pricing.lookup(s, b, "sms", "+355691234567", T0 + 2 * DAY).version_id == v1_
        )  # para v2: ende v1
        with pytest.raises(pricing.NoPrice, match="retired"):
            pricing.lookup(s, b, "sms", "+355691234567", T0 + 4 * DAY)  # v2 e tërhequr: s'bie te v1
        assert pricing.get_version(s, v2).status == "retired" and pricing.rules_of(
            s, v2
        )  # historia e lexueshme
        d = pricing.new_draft(s, adm(s, env), b, now=T0)
        with pytest.raises(errors.Conflict):
            pricing.retire(s, adm(s, env), d.id, "x")  # draft s'tërhiqet
    assert audit_actions(env).count("price_version.retire") == 1  # no-op ⇒ pa audit të dytë


def test_price_decimal_exactness_and_validation(env):
    big = (
        "9999999999999.999999" if is_pg(env) else "12345.678901"
    )  # NUMERIC i saktë vetëm në PG (SQLite = float)
    b, v = make_version(env, rules=(("355", "", "0.000001"), ("356", "", big)))
    with S(env) as s:
        assert pricing.lookup(s, b, "sms", "+355691234567", T0 + 2 * DAY).unit_price == D(
            "0.000001"
        )
        assert pricing.lookup(s, b, "sms", "+356691234567", T0 + 2 * DAY).unit_price == D(big)
        d = pricing.new_draft(s, adm(s, env), b, now=T0)
        for bad in (0.5, True, "0.0000001", "-1", "abc", D("NaN"), D("1E+20")):
            with pytest.raises(errors.Invalid):
                pricing.set_rule(s, adm(s, env), d.id, "sms", bad, prefix="357")
        for prefix in ("0355", "+355", "", "abc"):
            with pytest.raises(errors.Invalid):
                pricing.set_rule(s, adm(s, env), d.id, "sms", "1", prefix=prefix)
        with pytest.raises(errors.Invalid):
            pricing.set_rule(s, adm(s, env), d.id, "email", "1", prefix="355")  # email: prefix bosh
        with pytest.raises(errors.Invalid):
            pricing.set_rule(s, adm(s, env), d.id, "voice", "1")
        assert pricing.set_rule(s, adm(s, env), d.id, "email", "0.0015")[1] is True


def test_set_rule_noop_and_audit_semantics(env):
    with S(env) as s:
        a = adm(s, env)
        b = pricing.create_book(s, a, "std", "Std", "EUR", now=T0)
        v = pricing.new_draft(s, a, b.id, now=T0)
        _, changed = pricing.set_rule(s, a, v.id, "sms", "0.05", prefix="355", now=T0)
        _, same = pricing.set_rule(s, a, v.id, "sms", "0.050000", prefix="355", now=T0)
        _, upd = pricing.set_rule(s, a, v.id, "sms", "0.06", prefix="355", now=T0)
        assert (changed, same, upd) == (True, False, True)
        assert (
            pricing.remove_rule(s, a, v.id, "sms", prefix="355") is True
            and pricing.remove_rule(s, a, v.id, "sms", prefix="355") is False
        )
        s.commit()
    assert sorted(audit_actions(env)) == sorted(
        [
            "price_book.create",
            "price_version.create",
            "price_rule.set",
            "price_rule.set",
            "price_rule.remove",
        ]
    )


def test_revision_bumps_only_on_activation_retirement_and_assignment(env):
    assert revision(env) == 0
    b, v = make_version(env)  # activate ⇒ 1
    assert revision(env) == 1
    with S(env) as s:
        a = adm(s, env)
        d = pricing.new_draft(s, a, b, now=T0)
        pricing.set_rule(s, a, d.id, "sms", "0.07", prefix="355", now=T0)  # draft: pa bump
        s.commit()
    assert revision(env) == 1
    with S(env) as s:
        pricing.assign(s, adm(s, env), env.ids["e1"], env.ids["sms"], b, T0 + DAY, now=T0)
        s.commit()
    assert revision(env) == 2
    with S(env) as s:  # no-op ⇒ pa bump
        pricing.assign(s, adm(s, env), env.ids["e1"], env.ids["sms"], b, T0 + DAY, now=T0)
        s.commit()
    assert revision(env) == 2
    with S(env) as s:
        pricing.retire(s, adm(s, env), v, "obsolete", now=T0)
        s.commit()
    assert revision(env) == 3


def test_assignments_history_selection_and_constraints(env):
    b1, _ = make_version(env, code="std")
    b2, _ = make_version(env, code="gold", rules=(("355", "", "0.030000"),))
    e, p = env.ids["e1"], env.ids["sms"]
    with S(env) as s:
        a = adm(s, env)
        x = pricing.assign(s, a, e, p, b1, T0 + DAY, now=T0)
        assert pricing.assign(s, a, e, p, b1, T0 + DAY, now=T0).id == x.id  # idempotent
        with pytest.raises(errors.Conflict):
            pricing.assign(s, a, e, p, b2, T0 + DAY, now=T0)  # libër tjetër në të njëjtën kohë
        with pytest.raises(errors.Conflict):
            pricing.assign(s, a, e, p, b2, T0 - DAY, now=T0)  # e kaluar
        y = pricing.assign(s, a, e, p, b2, T0 + 5 * DAY, now=T0)
        with pytest.raises(errors.Conflict):
            pricing.assign(s, a, e, p, b1, T0 + 3 * DAY, now=T0)  # para të fundit ⇒ rend i rreptë
        with pytest.raises(errors.NotFound):
            pricing.assign(s, a, uuid.uuid4(), p, b1, T0 + 9 * DAY, now=T0)
        with pytest.raises(errors.NotFound):
            pricing.assign(s, a, e, uuid.uuid4(), b1, T0 + 9 * DAY, now=T0)
        s.commit()
        assert pricing.assignment_at(s, e, p, T0 + 2 * DAY).id == x.id
        assert pricing.assignment_at(s, e, p, T0 + 6 * DAY).id == y.id
        assert pricing.assignment_at(s, e, p, T0) is None
        # histori e pandryshueshme
        row = s.get(PriceAssignment, x.id)
        row.price_book_id = b2
        with pytest.raises(PricingImmutableError):
            s.flush()
        s.rollback()
    assert audit_actions(env).count("price_assignment.create") == 2


def test_price_book_identity_is_frozen_and_rows_are_never_deleted(env):
    b, v = make_version(env)
    with S(env) as s:
        book = s.get(PriceBook, b)
        book.currency = "USD"
        with pytest.raises(PricingImmutableError):
            s.flush()
        s.rollback()
        s.delete(s.get(PriceBook, b))
        with pytest.raises(PricingImmutableError):
            s.flush()
        s.rollback()
        s.delete(pricing.get_version(s, v))
        with pytest.raises(PricingImmutableError):
            s.flush()
        s.rollback()


def test_services_never_commit_and_audit_is_in_the_same_transaction(env):
    import inspect as ins

    for mod in (pricing, pricing_feed):
        assert "commit(" not in ins.getsource(mod)
    with S(env) as s:
        pricing.create_book(s, adm(s, env), "std", "Std", "EUR", now=T0)
        s.rollback()  # rollback heq edhe audit-in
    assert audit_actions(env) == []


# =============================================================================================================
# feed `cp.pricing.v1` + auth
# =============================================================================================================


def tok(env, client="pr", scope="pricing:read"):
    return assertion(env.private, client=client, kid="k1", scope=scope)


def snap(env, client="pr", scope="pricing:read", **q):
    return env.get("/internal/pricing/snapshot", params=q, headers=auth(tok(env, client, scope)))


def gen_of(env, client="pr"):
    from apps.central.models import ServiceClient

    with S(env) as s:
        return int(
            s.scalar(select(ServiceClient.auth_generation).where(ServiceClient.client_id == client))
        )


def test_scope_is_pricing_read_only_and_unauthenticated_is_401(env):
    assert snap(env).status_code == 200
    assert (
        snap(env, "mon", "money:read").status_code == 403
        and snap(env, "syn", "sync:read").status_code == 403
    )
    assert snap(env, "mon", "pricing:read").status_code == 403  # klienti s'e ka scope-in
    assert env.get("/internal/pricing/snapshot").status_code == 401
    assert env.get("/internal/pricing/snapshot", headers=auth("garbage")).status_code == 401
    assert env.get("/internal/pricing/state").status_code == 401
    ep = env.get("/internal/money/state", headers=auth(tok(env, "pr", "pricing:read")))
    assert ep.status_code == 403  # pricing:read s'jep qasje te money


def test_snapshot_is_complete_authorized_per_enterprise_and_verifiable(env):
    b1, v1_ = make_version(env, code="std")
    b2, _ = make_version(env, code="gold", rules=(("355", "", "0.030000"),))
    with S(env) as s:
        a = adm(s, env)
        pricing.assign(s, a, env.ids["e1"], env.ids["sms"], b1, T0 + DAY, now=T0)
        pricing.assign(s, a, env.ids["e2"], env.ids["sms"], b2, T0 + DAY, now=T0)
        pricing.assign(
            s, a, env.ids["e3"], env.ids["sms"], b2, T0 + DAY, now=T0
        )  # e3 s'është e autorizuar për "pr"
        s.commit()
    r = snap(env)
    assert r.status_code == 200 and r.json()["changed"] is True
    s_ = pv.PricingSnapshotV1.parse(r.json()["snapshot"])  # hash-et verifikohen
    assert {e["enterprise_id"] for e in s_.doc["enterprises"]} == {
        str(env.ids["e1"]),
        str(env.ids["e2"]),
    }
    assert str(env.ids["e3"]) not in r.text and {b["code"] for b in s_.doc["books"]} == {
        "std",
        "gold",
    }
    only1 = pv.PricingSnapshotV1.parse(snap(env, "pr1").json()["snapshot"])
    assert [b["code"] for b in only1.doc["books"]] == [
        "std"
    ]  # klienti i vetëm-e1 sheh vetëm librin e tij
    assert s_.revision == revision(env) and s_.doc["books"][0]["versions"][0]["rules"]


def test_drafts_are_never_distributed_and_retired_versions_are_marked(env):
    b, v = make_version(env)
    with S(env) as s:
        a = adm(s, env)
        pricing.assign(s, a, env.ids["e1"], env.ids["sms"], b, T0 + DAY, now=T0)
        d = pricing.new_draft(s, a, b, now=T0)
        pricing.set_rule(s, a, d.id, "sms", "0.99", prefix="355", now=T0)
        s.commit()
    versions = pv.PricingSnapshotV1.parse(snap(env).json()["snapshot"]).doc["books"][0]["versions"]
    assert [x["status"] for x in versions] == ["active"] and "0.990000" not in json.dumps(versions)
    with S(env) as s:
        pricing.retire(s, adm(s, env), v, "obsolete", now=T0)
        s.commit()
    versions = pv.PricingSnapshotV1.parse(snap(env).json()["snapshot"]).doc["books"][0]["versions"]
    assert [x["status"] for x in versions] == ["retired"]


def test_changed_false_when_nothing_moved_and_true_after_each_kind_of_change(env):
    b, v = make_version(env)
    with S(env) as s:
        pricing.assign(s, adm(s, env), env.ids["e1"], env.ids["sms"], b, T0 + DAY, now=T0)
        s.commit()
    first = snap(env).json()["snapshot"]
    known = dict(
        known_epoch=first["epoch"],
        known_revision=first["revision"],
        known_generation=first["authorization_generation"],
    )
    quiet = snap(env, **known).json()
    assert quiet == {"changed": False, "epoch": first["epoch"], "revision": first["revision"],
                     "authorization_generation": first["authorization_generation"]}  # fmt: skip
    with S(env) as s:  # draft i ri s'ndryshon asgjë
        pricing.new_draft(s, adm(s, env), b, now=T0)
        s.commit()
    assert snap(env, **known).json()["changed"] is False
    with S(env) as s:
        pricing.retire(s, adm(s, env), v, "x", now=T0)
        s.commit()
    assert snap(env, **known).json()["changed"] is True
    with S(env) as s:  # ndryshim autorizimi ⇒ generation tjetër ⇒ snapshot
        service_auth.grant_enterprise(s, "pr", env.ids["e3"])
        s.commit()
    assert (
        snap(
            env,
            known_epoch=first["epoch"],
            known_revision=revision(env),
            known_generation=first["authorization_generation"],
        ).json()["changed"]
        is True
    )


def test_state_endpoint_and_read_only_routes(env):
    st = env.get("/internal/pricing/state", headers=auth(tok(env))).json()
    assert set(st) == {"epoch", "revision", "authorization_generation"} and st["revision"] == 0
    paths = {
        p: sorted(v) for p, v in env.app.openapi()["paths"].items() if "/internal/pricing" in p
    }
    assert paths == {"/internal/pricing/state": ["get"], "/internal/pricing/snapshot": ["get"]}


def test_snapshot_does_not_change_state_and_serves_deterministic_bytes(env):
    b, _ = make_version(env)
    with S(env) as s:
        pricing.assign(s, adm(s, env), env.ids["e1"], env.ids["sms"], b, T0 + DAY, now=T0)
        s.commit()
    r1, r2 = snap(env).json()["snapshot"], snap(env).json()["snapshot"]
    assert r1 == r2 and revision(env) == 2


def test_central_pricing_migration_0020_up_down_up(make_db):  # noqa: F811
    url = make_db()
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    tables = {
        "price_books",
        "price_versions",
        "price_rules",
        "price_assignments",
        "pricing_sequence",
    }
    assert tables <= set(inspect(eng).get_table_names())
    with Session(eng) as s:
        assert pricing.read_state(s)[1] == 0
    central_alembic(url, "downgrade", "0019")
    assert not tables & set(inspect(eng).get_table_names())
    central_alembic(url, "upgrade", "head")
    eng.dispose()


# =============================================================================================================
# import (propozim → klasifikim → zbatim)
# =============================================================================================================


def proposal(env, **over):
    base = {
        "schema": "pricing-bootstrap.v1",
        "books": [
            {
                "code": "std",
                "name": "Std",
                "currency": "EUR",
                "versions": [
                    {
                        "version": 1,
                        "effective_from": "2029-01-01T00:00:00.000000+00:00",
                        "rules": [
                            {
                                "channel": "sms",
                                "prefix": "355",
                                "operator": "",
                                "unit_price": "0.050000",
                            }
                        ],
                    },
                    {
                        "version": 2,
                        "effective_from": "2029-06-01T00:00:00.000000+00:00",
                        "rules": [
                            {
                                "channel": "sms",
                                "prefix": "355",
                                "operator": "",
                                "unit_price": "0.055000",
                            },
                            {
                                "channel": "sms",
                                "prefix": "35569",
                                "operator": "27601",
                                "unit_price": "0.040000",
                            },
                        ],
                    },
                ],
            },
            {
                "code": "email-pro",
                "name": "Email pro",
                "currency": "EUR",
                "versions": [
                    {
                        "version": 1,
                        "effective_from": "2029-01-01T00:00:00.000000+00:00",
                        "rules": [
                            {
                                "channel": "email",
                                "prefix": "",
                                "operator": "",
                                "unit_price": "0.001500",
                            }
                        ],
                    }
                ],
            },
        ],
        "assignments": [
            {
                "owner_ref": "acme",
                "enterprise_id": str(env.ids["e1"]),
                "channel": "sms",
                "book_code": "std",
            },
            {
                "owner_ref": "acme",
                "enterprise_id": str(env.ids["e1"]),
                "channel": "email",
                "book_code": "email-pro",
            },
        ],
    }
    base.update(over)
    return base


def run_import(env, proposal_doc, **kw):
    with S(env) as s:
        rep = pricing_import.classify_and_apply(
            s, proposal_doc, actor=adm(s, env) if kw.get("apply") else None, now=T0, **kw
        )
        s.commit() if kw.get("apply") else s.rollback()
        return rep


def table_counts(env):
    with S(env) as s:
        return [
            s.scalar(select(func.count()).select_from(m))
            for m in (PriceBook, PriceVersion, PriceRule, PriceAssignment)
        ]


def test_dry_run_classifies_without_writing(env):
    rep = run_import(env, proposal(env))
    assert rep.counts() == {"exact": 4, "conflict": 0, "invalid": 0, "unmapped": 0}
    assert (
        table_counts(env) == [0, 0, 0, 0]
        and not any(i.applied for i in rep.items)
        and audit_actions(env) == []
    )
    assert rep.proposal_hash == pricing_import.proposal_hash(proposal(env))


def test_apply_imports_exact_items_with_history_and_is_idempotent(env):
    run_import(env, proposal(env), apply=True)
    assert table_counts(env) == [2, 3, 4, 2]
    with S(env) as s:
        b = s.scalar(select(PriceBook).where(PriceBook.code == "std"))
        vs = list(
            s.scalars(
                select(PriceVersion)
                .where(PriceVersion.price_book_id == b.id)
                .order_by(PriceVersion.version)
            )
        )
        assert [(v.version, v.status, v.imported) for v in vs] == [
            (1, "active", True),
            (2, "active", True),
        ]
        assert (
            pv.format_ts(vs[0].effective_from) == "2029-01-01T00:00:00.000000+00:00"
        )  # historia e ruajtur
        assert pricing.lookup(
            s, b.id, "sms", "+355691234567", datetime(2029, 3, 1, tzinfo=UTC)
        ).unit_price == D("0.050000")
        assert pricing.lookup(
            s, b.id, "sms", "+355691234567", datetime(2029, 7, 1, tzinfo=UTC), "27601"
        ).unit_price == D("0.040000")
        assert pricing.assignment_at(s, env.ids["e1"], env.ids["sms"], T0) is not None
        assert pricing.assignment_at(s, env.ids["e1"], env.ids["email"], T0) is not None
    again = run_import(env, proposal(env), apply=True)  # rerun: exact/no-op
    assert again.counts()["exact"] == 4 and table_counts(env) == [2, 3, 4, 2]
    assert "price_version.import" in audit_actions(env)


def test_classification_covers_conflict_invalid_and_unmapped(env):
    run_import(env, proposal(env), apply=True)
    p = proposal(env)
    p["books"][0]["versions"][1]["rules"][0]["unit_price"] = (
        "0.099000"  # ndryshon versionin ekzistues
    )
    p["books"].append(
        {
            "code": "Bad Code",
            "name": "x",
            "currency": "EUR",
            "versions": [
                {
                    "version": 1,
                    "effective_from": "2029-01-01T00:00:00.000000+00:00",
                    "rules": [
                        {"channel": "sms", "prefix": "355", "operator": "", "unit_price": "0.01"}
                    ],
                }
            ],
        }
    )
    p["books"].append(
        {
            "code": "badprice",
            "name": "x",
            "currency": "EUR",
            "versions": [
                {
                    "version": 1,
                    "effective_from": "2029-01-01T00:00:00.000000+00:00",
                    "rules": [
                        {
                            "channel": "sms",
                            "prefix": "355",
                            "operator": "",
                            "unit_price": "0.0000001",
                        }
                    ],
                }
            ],
        }
    )
    p["books"].append(
        {
            "code": "badprefix",
            "name": "x",
            "currency": "EUR",
            "versions": [
                {
                    "version": 1,
                    "effective_from": "2029-01-01T00:00:00.000000+00:00",
                    "rules": [
                        {"channel": "sms", "prefix": "0355", "operator": "", "unit_price": "0.01"}
                    ],
                }
            ],
        }
    )
    p["books"].append(
        {
            "code": "badcur",
            "name": "x",
            "currency": "eur",
            "versions": [
                {
                    "version": 1,
                    "effective_from": "2029-01-01T00:00:00.000000+00:00",
                    "rules": [
                        {"channel": "sms", "prefix": "355", "operator": "", "unit_price": "0.01"}
                    ],
                }
            ],
        }
    )
    p["assignments"] += [
        {"owner_ref": "ghost", "enterprise_id": None, "channel": "sms", "book_code": "std"},
        {
            "owner_ref": "x",
            "enterprise_id": str(uuid.uuid4()),
            "channel": "sms",
            "book_code": "std",
        },
        {
            "owner_ref": "y",
            "enterprise_id": str(env.ids["e2"]),
            "channel": "sms",
            "book_code": "Bad Code",
        },
    ]
    rep = run_import(env, p)
    by = {(i.kind, i.ref): i.classification for i in rep.items}
    assert (
        by[("book", "std")] == "conflict"
        and by[("book", "Bad Code")]
        == by[("book", "badprice")]
        == by[("book", "badprefix")]
        == by[("book", "badcur")]
        == "invalid"
    )
    assert (
        by[("assignment", "ghost->std")] == "unmapped"
        and by[("assignment", "y->Bad Code")] == "unmapped"
    )
    assert by[("assignment", "x->std")] == "unmapped"  # enterprise i panjohur
    c = rep.counts()
    assert c["conflict"] == 1 and c["invalid"] == 4 and c["unmapped"] >= 3


def test_apply_skips_everything_that_is_not_exact_and_changes_nothing_for_them(env):
    p = proposal(env)
    p["books"].append(
        {
            "code": "Bad Code",
            "name": "x",
            "currency": "EUR",
            "versions": [
                {
                    "version": 1,
                    "effective_from": "2029-01-01T00:00:00.000000+00:00",
                    "rules": [
                        {"channel": "sms", "prefix": "355", "operator": "", "unit_price": "0.01"}
                    ],
                }
            ],
        }
    )
    run_import(env, p, apply=True)
    with S(env) as s:
        assert (
            s.scalar(
                select(func.count()).select_from(PriceBook).where(PriceBook.code == "Bad Code")
            )
            == 0
        )
        assert s.scalar(select(func.count()).select_from(PriceBook)) == 2


def test_cli_dry_run_default_requires_hash_ack_and_admin_for_apply(env, tmp_path, capsys):
    f = tmp_path / "p.json"
    f.write_text(json.dumps(proposal(env)))
    assert cli.main(["--proposal", str(f)], engine=env.eng) == 0
    out = capsys.readouterr().out
    assert out.startswith("DRY-RUN") and table_counts(env) == [0, 0, 0, 0]
    h = pricing_import.proposal_hash(proposal(env))
    assert (
        cli.main(
            [
                "--proposal",
                str(f),
                "--apply",
                "--actor-email",
                "a1@example.com",
                "--ack-proposal-hash",
                "wrong",
            ],
            engine=env.eng,
        )
        == 1
    )
    assert (
        cli.main(
            [
                "--proposal",
                str(f),
                "--apply",
                "--actor-email",
                "nobody@example.com",
                "--ack-proposal-hash",
                h,
            ],
            engine=env.eng,
        )
        == 1
    )
    assert table_counts(env) == [0, 0, 0, 0]
    assert (
        cli.main(
            [
                "--proposal",
                str(f),
                "--apply",
                "--actor-email",
                "a1@example.com",
                "--ack-proposal-hash",
                h,
            ],
            engine=env.eng,
        )
        == 0
    )
    assert table_counts(env) == [2, 3, 4, 2] and "APPLIED" in capsys.readouterr().out
    assert cli.main(["--proposal", str(tmp_path / "missing.json")], engine=env.eng) == 2
    f.write_text(json.dumps({"schema": "other"}))
    assert cli.main(["--proposal", str(f)], engine=env.eng) == 2


# =============================================================================================================
# PostgreSQL
# =============================================================================================================

pg = pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")


def is_pg(env):
    return env.eng.dialect.name == "postgresql"


def race(n, fn):
    barrier = threading.Barrier(n, timeout=20)
    out, errs = [None] * n, []

    def run(i):
        try:
            out[i] = fn(i, barrier)
        except BaseException as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join(40) for t in ts]
    assert not any(t.is_alive() for t in ts)
    return out, errs


def test_pg_triggers_protect_active_versions_rules_assignments_and_books(env):
    if not is_pg(env):
        pytest.skip("needs PostgreSQL triggers")
    b, v = make_version(env)
    with S(env) as s:
        pricing.assign(s, adm(s, env), env.ids["e1"], env.ids["sms"], b, T0 + DAY, now=T0)
        s.commit()
    for stmt in (
        f"UPDATE price_rules SET unit_price = 9 WHERE version_id = '{v}'",
        f"DELETE FROM price_rules WHERE version_id = '{v}'",
        f"INSERT INTO price_rules (id, version_id, channel, prefix, operator, unit_price) VALUES ('{uuid.uuid4()}', '{v}', 'sms', '44', '', 1)",
        f"UPDATE price_versions SET content_hash = '{'0' * 64}' WHERE id = '{v}'",
        f"UPDATE price_versions SET effective_from = now() WHERE id = '{v}'",
        f"UPDATE price_versions SET status = 'draft', effective_from = NULL, content_hash = NULL, activated_at = NULL WHERE id = '{v}'",
        f"DELETE FROM price_versions WHERE id = '{v}'",
        "UPDATE price_assignments SET effective_from = now()",
        "DELETE FROM price_assignments",
        "UPDATE price_books SET currency = 'USD'",
        "DELETE FROM price_books",
    ):
        with pytest.raises(DBAPIError), env.eng.begin() as c:
            c.execute(text(stmt))
    with env.eng.begin() as c:  # active → retired lejohet; retired është final
        c.execute(
            text(
                f"UPDATE price_versions SET status='retired', retired_at=now(), retire_reason='x' WHERE id = '{v}'"
            )
        )
    with pytest.raises(DBAPIError), env.eng.begin() as c:
        c.execute(
            text(
                f"UPDATE price_versions SET status='active', retired_at=NULL, retire_reason=NULL WHERE id = '{v}'"
            )
        )


def test_pg_two_concurrent_activations_of_the_same_draft_yield_one_active_version(env):
    if not is_pg(env):
        pytest.skip("needs PostgreSQL")
    with S(env) as s:
        a = adm(s, env)
        b = pricing.create_book(s, a, "std", "Std", "EUR", now=T0)
        d = pricing.new_draft(s, a, b.id, now=T0)
        pricing.set_rule(s, a, d.id, "sms", "0.05", prefix="355", now=T0)
        s.commit()
        bid, did = b.id, d.id

    def go(i, bar):
        with S(env) as s:
            bar.wait()
            try:
                pricing.activate(
                    s, adm(s, env), did, T0 + DAY * (1 + i), now=T0
                )  # data të ndryshme!
                s.commit()
                return "ok"
            except errors.Conflict:
                s.rollback()
                return "conflict"

    out, errs = race(2, go)
    assert not errs and sorted(out) == ["conflict", "ok"]
    with S(env) as s:
        actives = list(
            s.scalars(
                select(PriceVersion).where(
                    PriceVersion.price_book_id == bid, PriceVersion.status == "active"
                )
            )
        )
        assert len(actives) == 1 and pricing.read_state(s)[1] == 1  # një aktivizim ⇒ një bump


def test_pg_rule_edit_racing_activation_never_mutates_an_active_version(env):
    if not is_pg(env):
        pytest.skip("needs PostgreSQL")
    with S(env) as s:
        a = adm(s, env)
        b = pricing.create_book(s, a, "std", "Std", "EUR", now=T0)
        d = pricing.new_draft(s, a, b.id, now=T0)
        pricing.set_rule(s, a, d.id, "sms", "0.05", prefix="355", now=T0)
        s.commit()
        did = d.id

    def go(i, bar):
        with S(env) as s:
            bar.wait()
            try:
                if i == 0:
                    pricing.activate(s, adm(s, env), did, T0 + DAY, now=T0)
                else:
                    pricing.set_rule(s, adm(s, env), did, "sms", "0.99", prefix="355", now=T0)
                s.commit()
                return "ok"
            except (errors.Conflict, PricingImmutableError, DBAPIError):
                s.rollback()
                return "rejected"

    out, errs = race(2, go)
    assert not errs
    with S(env) as s:
        v = pricing.get_version(s, did)
        rules = pricing.rules_of(s, did)
        assert v.status == "active" and v.content_hash == pv.rules_hash(
            rules
        )  # hash-i ≡ rregullat finale (asnjë gjysmë-ndryshim)


def test_pg_concurrent_snapshot_is_consistent_with_its_revision_while_pricing_is_activated(env):
    if not is_pg(env):
        pytest.skip("needs PostgreSQL")
    b, v1_ = make_version(env, eff=T0 + DAY)
    with S(env) as s:
        pricing.assign(s, adm(s, env), env.ids["e1"], env.ids["sms"], b, T0 + DAY, now=T0)
        s.commit()
    seen = []

    def go(i, bar):
        bar.wait()
        if i == 0:
            make_version(env, book=b, rules=(("355", "", "0.090000"),), eff=T0 + 5 * DAY)
            return None
        for _ in range(15):
            doc = snap(env).json()["snapshot"]
            seen.append((doc["revision"], len(doc["books"][0]["versions"])))
        return None

    _, errs = race(2, go)
    assert not errs
    # revision 2 ⇒ vetëm v1; revision 3 ⇒ v1+v2 të plota: kurrë revision i ri me përmbajtje të vjetër
    assert all(vers == (1 if rev <= 2 else 2) for rev, vers in seen), seen
