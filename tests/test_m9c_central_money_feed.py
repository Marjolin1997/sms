# ruff: noqa: F811
"""M9-c — Central: feed `cp.money.v1`, scope `money:read`, autorizim per enterprise, gap-safe, epoch/generation,
grant bootstrap i tipizuar (purpose/baseline_ref)."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from apps.central.core import errors
from apps.central.main import create_app
from apps.central.models import CentralUser, CreditGrant, MoneyEvent
from apps.central.models.money import MoneyImmutableError
from apps.central.services import credit_accounts as accts
from apps.central.services import enterprises as ent
from apps.central.services import grants, money_feed, payments, service_auth, users
from apps.central.services import products as prod
from packages.contracts.control_plane.money import v1
from tests.test_central import IS_PG, central_alembic, make_db  # noqa: F401
from tests.test_central_auth import (
    PW,
    auth_secret,  # noqa: F401
)
from tests.test_central_sync_api import assertion, auth, keypair

REF = "ef" * 32


@pytest.fixture
def env(make_db):
    url = make_db()
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    private, public = keypair()
    with Session(eng, expire_on_commit=False) as s:
        e1, e2, e3 = ent.create(s, "Acme"), ent.create(s, "Beta"), ent.create(s, "Gamma")
        sms = prod.create(s, "sms", "SMS", "sms")
        admin = users.create_user(s, "a1@example.com", PW, "admin")
        service_auth.create_client(s, "mon", ["money:read"], [e1.id, e2.id])
        service_auth.add_key(s, "mon", "k1", public)
        service_auth.create_client(s, "syn", ["sync:read"], [e1.id])
        service_auth.add_key(s, "syn", "k2", public)
        service_auth.create_client(s, "both", ["sync:read", "money:read"], [e1.id])
        service_auth.add_key(s, "both", "k3", public)
        s.commit()
        ids = dict(e1=e1.id, e2=e2.id, e3=e3.id, sms=sms.id, admin=admin.id)
    c = TestClient(create_app(eng))
    c.eng, c.private, c.ids = eng, private, ids
    yield c
    eng.dispose()


def acct_with_funds(env, which="e1", amount="500"):
    with Session(env.eng, expire_on_commit=False) as s:
        adm = s.get(CentralUser, env.ids["admin"])
        a = accts.create(s, env.ids[which], env.ids["sms"], "EUR", adm)
        p = payments.create(s, a.id, amount, system="system:payment_import")
        s2 = s
        users_other = users.create_user(s2, f"b-{uuid.uuid4().hex[:6]}@example.com", PW, "admin")
        payments.approve(s, p.id, users_other)
        s.commit()
        return a.id


def issue(env, account_id, amount, key=None, **kw):
    with Session(env.eng, expire_on_commit=False) as s:
        adm = s.get(CentralUser, env.ids["admin"])
        g = grants.issue(
            s, account_id, amount, idempotency_key=key or uuid.uuid4().hex, actor=adm, **kw
        )
        s.commit()
        return g.id


def reverse(env, gid):
    with Session(env.eng, expire_on_commit=False) as s:
        grants.reverse(s, gid, s.get(CentralUser, env.ids["admin"]), "customer refund")
        s.commit()


def state_of(env):
    with Session(env.eng) as s:
        epoch, last = money_feed.read_state(s)
    return str(epoch), last


def tok(env, client="mon", kid="k1", scope="money:read", **kw):
    return assertion(env.private, client=client, kid=kid, scope=scope, **kw)


def gen_of(env, client="mon"):
    from apps.central.models import ServiceClient

    with Session(env.eng) as s:
        return int(
            s.scalar(select(ServiceClient.auth_generation).where(ServiceClient.client_id == client))
        )


def feed(env, after=0, limit=100, epoch=None, generation=None, token=None, client="mon"):
    ep, _ = state_of(env)
    generation = generation if generation is not None else gen_of(env, client)
    q = f"after_seq={after}&limit={limit}&epoch={epoch or ep}&generation={generation}"
    return env.get("/internal/money/changes?" + q, headers=auth(token or tok(env)))


def test_feed_returns_exact_cp_money_v1_events_in_seq_order(env):
    a = acct_with_funds(env)
    g = issue(env, a, "100")
    reverse(env, g)
    r = feed(env)
    assert r.status_code == 200, r.text
    body = r.json()
    evs = body["events"]
    assert [e["event_type"] for e in evs] == ["credit_grant.issued", "credit_grant.reversed"]
    seqs = [
        e["seq"] for e in evs
    ]  # seq e feed-it ndahet me ledger-in tregtar: boshllëqet janë normale
    assert seqs == sorted(seqs) and len(set(seqs)) == 2
    assert body["next_seq"] == body["latest_seq"] >= seqs[-1]
    for (
        e
    ) in evs:  # çdo ngjarje parsohet nga kontrata (pa fusha të tjera) dhe riserializohet identike
        parsed = v1.MoneyEventV1.from_dict(e)
        assert json.loads(parsed.to_bytes()) == e
        assert e["grant_id"] == str(g) and e["data"]["amount"] == "100.000000"
        assert e["data"]["purpose"] == "standard" and e["data"]["baseline_ref"] is None
    assert body["has_more"] is False and body["epoch"] == state_of(env)[0]


def test_scope_money_read_is_separate_from_sync_read(env):
    acct_with_funds(env)
    # klient vetëm me sync:read → 403 te money; klient vetëm me money:read → 403 te sync
    r = feed(env, token=tok(env, client="syn", kid="k2", scope="sync:read"))
    assert r.status_code == 403
    r = feed(env, token=tok(env, client="syn", kid="k2", scope="money:read"))
    assert r.status_code == 403  # kërkon scope në assertion DHE në klient
    ep, _ = state_of(env)
    r = env.get(f"/internal/sync/changes?after_seq=0&epoch={ep}&generation={gen_of(env)}",
                headers=auth(tok(env)))  # fmt: skip
    assert r.status_code in (401, 403)
    ok = feed(env, token=tok(env, client="both", kid="k3"), client="both")
    assert ok.status_code == 200


def test_unauthenticated_and_garbage_tokens_are_401(env):
    ep, _ = state_of(env)
    url = f"/internal/money/changes?after_seq=0&epoch={ep}&generation={gen_of(env)}"
    assert env.get(url).status_code == 401
    assert env.get(url, headers=auth("garbage")).status_code == 401
    assert env.get("/internal/money/state").status_code == 401


def test_authorization_is_per_enterprise(env):
    a1, a2, a3 = (acct_with_funds(env, w) for w in ("e1", "e2", "e3"))
    for a in (a1, a2, a3):
        issue(env, a, "10")
    r = feed(env)
    ents = {e["enterprise_id"] for e in r.json()["events"]}
    assert ents == {str(env.ids["e1"]), str(env.ids["e2"])}  # e3 s'është e autorizuar
    assert str(env.ids["e3"]) not in r.text
    # klienti i vetëm-e1 sheh vetëm e1
    r = feed(env, token=tok(env, client="both", kid="k3"), client="both")
    assert {e["enterprise_id"] for e in r.json()["events"]} == {str(env.ids["e1"])}


def test_next_seq_is_gap_safe_over_other_tenants_and_paginates(env):
    a1, a3 = acct_with_funds(env, "e1"), acct_with_funds(env, "e3")
    issue(env, a1, "10")
    issue(env, a3, "10")  # enterprise e papautorizuar për klientin
    issue(env, a3, "10")
    both = dict(token=tok(env, client="both", kid="k3"), client="both")
    r = feed(env, **both).json()
    assert len(r["events"]) == 1 and r["next_seq"] == r["latest_seq"] > r["events"][0]["seq"]
    assert not r["has_more"]
    issue(env, a1, "10")
    first = feed(env, limit=1).json()
    assert first["has_more"] and first["next_seq"] == first["events"][0]["seq"]
    rest = feed(env, after=first["next_seq"], limit=10).json()
    assert (
        len(rest["events"]) == 1 and not rest["has_more"] and rest["next_seq"] == rest["latest_seq"]
    )
    assert rest["events"][0]["seq"] > first["next_seq"]
    quiet = feed(env, after=rest["next_seq"]).json()
    assert quiet["events"] == [] and quiet["next_seq"] == rest["next_seq"]


def test_epoch_generation_and_cursor_ahead_return_409_with_money_codes(env):
    acct_with_funds(env)
    bad_epoch = feed(env, epoch=str(uuid.uuid4()))
    assert (
        bad_epoch.status_code == 409
        and bad_epoch.json()["detail"]["code"] == "money_epoch_mismatch"
    )
    assert bad_epoch.json()["detail"]["action"] != "snapshot"
    bad_gen = feed(env, generation=7)
    assert (
        bad_gen.status_code == 409
        and bad_gen.json()["detail"]["code"] == "money_authorization_changed"
    )
    ahead = feed(env, after=10**6)
    assert ahead.status_code == 409 and ahead.json()["detail"]["code"] == "money_cursor_ahead"


def test_authorization_change_bumps_generation_and_state_endpoint_reports_it(env):
    a = acct_with_funds(env)
    issue(env, a, "10")
    st = env.get("/internal/money/state", headers=auth(tok(env))).json()
    assert set(st) == {"epoch", "authorization_generation", "latest_seq"}
    g0 = gen_of(env)
    assert st["authorization_generation"] == g0 and st["latest_seq"] >= 1
    with Session(env.eng) as s:
        service_auth.grant_enterprise(s, "mon", env.ids["e3"])
        s.commit()
    r = feed(env, generation=g0)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "money_authorization_changed"
    st2 = env.get("/internal/money/state", headers=auth(tok(env))).json()
    assert st2["authorization_generation"] > g0
    assert feed(env, generation=st2["authorization_generation"]).status_code == 200


def test_feed_is_read_only_and_exposes_no_ack_or_mutation_routes(env):
    paths = {p: sorted(v) for p, v in env.app.openapi()["paths"].items() if "/internal/money" in p}
    assert paths == {"/internal/money/state": ["get"], "/internal/money/changes": ["get"]}


def test_event_bytes_come_from_the_frozen_payload_not_from_current_grant_state(env):
    a = acct_with_funds(env)
    g = issue(env, a, "100")
    first = feed(env).json()["events"][0]
    reverse(env, g)
    again = feed(env).json()["events"]
    assert again[0] == first  # ngjarja e issuance s'ndryshon pas reversal-it
    assert (
        again[1]["event_type"] == "credit_grant.reversed"
        and again[1]["data"]["amount"] == "100.000000"
    )


def test_bootstrap_grant_is_typed_unique_per_baseline_and_flows_through_the_feed(env):
    a = acct_with_funds(env, amount="2000")
    g = issue(env, a, "1200", purpose="bootstrap", baseline_ref=REF)
    e = feed(env).json()["events"][0]
    assert e["data"]["purpose"] == "bootstrap" and e["data"]["baseline_ref"] == REF
    with Session(env.eng, expire_on_commit=False) as s:
        row = s.get(CreditGrant, g)
        assert (row.purpose, row.baseline_ref) == ("bootstrap", REF)
    # një baseline ka një autorizim bootstrap (edhe me çelës tjetër idempotence)
    with pytest.raises(errors.Conflict):
        issue(env, a, "1200", purpose="bootstrap", baseline_ref=REF)
    # idempotenca e të njëjtit çelës kthen të njëjtin grant
    k = "boot-key-0001"
    g2 = issue(env, a, "300", key=k, purpose="bootstrap", baseline_ref="aa" * 32)
    assert issue(env, a, "300", key=k, purpose="bootstrap", baseline_ref="aa" * 32) == g2
    with pytest.raises(errors.Conflict):
        issue(env, a, "300", key=k, purpose="bootstrap", baseline_ref="bb" * 32)


@pytest.mark.parametrize(
    "kw",
    [
        dict(purpose="bootstrap"),
        dict(purpose="bootstrap", baseline_ref="short"),
        dict(purpose="standard", baseline_ref=REF),
        dict(purpose="weird"),
    ],
)
def test_invalid_purpose_combinations_are_rejected(env, kw):
    a = acct_with_funds(env)
    with pytest.raises(errors.Invalid):
        issue(env, a, "10", **kw)


def test_purpose_and_baseline_ref_are_frozen(env):
    a = acct_with_funds(env)
    g = issue(env, a, "10")
    with Session(env.eng) as s:
        row = s.get(CreditGrant, g)
        row.purpose = "bootstrap"
        with pytest.raises(MoneyImmutableError):
            s.flush()
        s.rollback()
    if IS_PG:
        with Session(env.eng) as s, pytest.raises((DBAPIError, IntegrityError)):
            s.execute(text("UPDATE credit_grants SET purpose='bootstrap', baseline_ref=:r WHERE id=:i"),
                      {"r": REF, "i": g})  # fmt: skip
            s.commit()


def test_legacy_event_rows_without_purpose_are_served_as_standard(env):
    a = acct_with_funds(env)
    issue(env, a, "10")
    with Session(
        env.eng
    ) as s:  # simulon ngjarje të M9-b (payload pa purpose): ndërmjet tabelave të pandryshueshme
        ev = s.scalar(select(MoneyEvent))
        payload = {k: v for k, v in ev.payload.items() if k not in ("purpose", "baseline_ref")}
        from apps.central.services import money_contract

        ev.payload = payload
        assert money_contract.to_event(ev).data.purpose == "standard"
        s.rollback()


def test_services_never_commit_and_feed_has_no_unbounded_scan(env):
    import inspect as ins

    assert "commit(" not in ins.getsource(money_feed)
    assert money_feed.MAX_LIMIT == 500
    ep, _ = state_of(env)
    r = env.get(f"/internal/money/changes?after_seq=0&epoch={ep}&generation={gen_of(env)}&limit=501",
                headers=auth(tok(env)))  # fmt: skip
    assert r.status_code == 422
