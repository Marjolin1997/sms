# ruff: noqa: F811
"""M10-S2 — Central: outbox transaksional, feed `cp.sender.v1`, snapshot koherent, autorizim sipas enterprise-it, skop `sender:read`, epoka/generation/kursor."""

import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from apps.central.core import errors
from apps.central.main import create_app
from apps.central.models import CentralUser
from apps.central.models.sender import SenderImmutableError, SenderSyncOutbox
from apps.central.services import enterprises as ent
from apps.central.services import sender_sync, service_auth, users
from apps.central.services import senders as svc
from packages.contracts.control_plane.sender import v1
from tests.test_central import IS_PG, central_alembic, make_db  # noqa: F401
from tests.test_central_auth import PW, auth_secret  # noqa: F401
from tests.test_central_sync_api import assertion, auth, keypair


@pytest.fixture
def senv(make_db):
    url = make_db()
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    private, public = keypair()
    with Session(eng, expire_on_commit=False) as s:
        e1, e2, e3 = ent.create(s, "Acme"), ent.create(s, "Beta"), ent.create(s, "Gamma")
        admin = users.create_user(s, "a1@example.com", PW, "admin")
        service_auth.create_client(s, "snd", ["sender:read"], [e1.id, e2.id])
        service_auth.add_key(s, "snd", "k1", public)
        service_auth.create_client(s, "snd1", ["sender:read"], [e1.id])
        service_auth.add_key(s, "snd1", "k2", public)
        service_auth.create_client(s, "syn", ["sync:read"], [e1.id])
        service_auth.add_key(s, "syn", "k3", public)
        service_auth.create_client(s, "mon", ["money:read"], [e1.id])
        service_auth.add_key(s, "mon", "k4", public)
        s.commit()
        ids = dict(e1=e1.id, e2=e2.id, e3=e3.id, admin=admin.id)
    c = TestClient(create_app(eng))
    c.eng, c.private, c.ids = eng, private, ids
    yield c
    eng.dispose()


def tok(env, client="snd", kid="k1", scope="sender:read", **kw):
    return assertion(env.private, client=client, kid=kid, scope=scope, **kw)


def state(env):
    with Session(env.eng) as s:
        return sender_sync.read_state(s)


def gen_of(env, client="snd"):
    from apps.central.models import ServiceClient

    with Session(env.eng) as s:
        return int(
            s.scalar(select(ServiceClient.auth_generation).where(ServiceClient.client_id == client))
        )


def feed(env, after=0, limit=100, client="snd", kid="k1", epoch=None, generation=None, token=None):
    ep, _f, _l = state(env)
    q = f"after_seq={after}&limit={limit}&epoch={epoch or ep}&generation={generation if generation is not None else gen_of(env, client)}"
    return env.get("/internal/sender/changes?" + q, headers=auth(token or tok(env, client, kid)))


def A(s, env):
    return s.get(CentralUser, env.ids["admin"])


def mutate(env, fn):
    with Session(env.eng, expire_on_commit=False) as s:
        out = fn(s, A(s, env))
        s.commit()
        return out


def req(env, which="e1", value="Acme", ref=None, country="AL"):
    return mutate(
        env,
        lambda s, a: (
            svc.request_sender(
                s, a, env.ids[which], ref or f"r-{uuid.uuid4().hex[:8]}", country, value
            ).sender.id
        ),
    )


# =============================================================================================================
# sekuenca transaksionale dhe grupet
# =============================================================================================================


def test_events_are_written_in_the_same_transaction_with_consecutive_seq_and_one_group(senv):
    ids = [req(senv, "e1", f"Brand{i}") for i in range(3)]
    for i in ids:
        mutate(senv, lambda s, a, i=i: svc.approve(s, a, i))
    _e, _f, before = state(senv)
    ch = mutate(senv, lambda s, a: svc.set_policy(s, a, "AL", "alphanumeric", False, True, "ban"))
    assert ch.revoked == 3
    _e, _f, after = state(senv)
    assert after == before + 4  # politika + 3 revokime, seq i njëpasnjëshëm
    r = feed(senv, after=before).json()
    evs = r["events"]
    assert [e["seq"] for e in evs] == list(range(before + 1, after + 1))
    assert evs[0]["event_type"] == v1.EVENT_POLICY and {e["event_type"] for e in evs[1:]} == {
        v1.EVENT_REGISTRY
    }
    assert len({e["group"]["id"] for e in evs}) == 1 and {e["group"]["size"] for e in evs} == {4}
    assert all(
        e["data"]["status"] == "revoked"
        and e["data"]["policy_revision"] == 1
        and e["data"]["policy_source"] == "explicit"
        for e in evs[1:]
    )
    assert evs[0]["data"]["allowed"] is False and evs[0]["revision"] == 1


def test_rollback_leaves_no_seq_gap_and_no_outbox_row(senv):
    _e, _f, before = state(senv)
    with Session(senv.eng) as s:
        svc.request_sender(s, A(s, senv), senv.ids["e1"], "tmp-1", "AL", "Ghost")
        s.flush()
        assert sender_sync.read_state(s)[2] == before + 1  # brenda tx
        s.rollback()
    assert state(senv)[2] == before
    with Session(senv.eng) as s:
        assert s.scalar(select(func.count()).select_from(SenderSyncOutbox)) == 0
    sid = req(senv, "e1", "Real")
    assert state(senv)[2] == before + 1 and sid


def test_failed_mutations_publish_nothing(senv):
    a1 = req(senv, "e1", "Acme")
    a2 = req(senv, "e2", "ACME")
    mutate(senv, lambda s, a: svc.approve(s, a, a1))
    _e, _f, before = state(senv)
    with Session(senv.eng) as s, pytest.raises(errors.Conflict):
        svc.approve(s, A(s, senv), a2)  # çelës i zënë
    assert state(senv)[2] == before
    with Session(senv.eng) as s, pytest.raises(errors.Conflict):
        svc.revoke(s, A(s, senv), a2, "x")  # nga pending
    assert state(senv)[2] == before


def test_idempotent_operations_emit_no_duplicate_events(senv):
    req(senv, "e1", "Acme", ref="same")
    _e, _f, before = state(senv)
    assert req(senv, "e1", "Acme", ref="same")
    assert state(senv)[2] == before
    mutate(
        senv, lambda s, a: svc.set_policy(s, a, "AL", "alphanumeric", True, True, "default-like")
    )
    _e, _f, mid = state(senv)
    mutate(senv, lambda s, a: svc.set_policy(s, a, "AL", "alphanumeric", True, True, "again"))
    assert state(senv)[2] == mid  # përmbajtje identike ⇒ asnjë revizion, asnjë ngjarje


def test_auto_approval_emits_one_final_state_event_with_the_last_decision(senv):
    mutate(senv, lambda s, a: svc.set_policy(s, a, "AL", "alphanumeric", True, False, "open"))
    _e, _f, before = state(senv)
    sid = req(senv, "e1", "Auto")
    r = feed(senv, after=before).json()["events"]
    assert (
        len(r) == 1
        and r[0]["data"]["status"] == "approved"
        and r[0]["data"]["decision"] == "approved"
        and r[0]["revision"] == 2
    )
    assert r[0]["entity"]["id"] == str(sid)


def test_event_payload_is_frozen_at_the_time_of_the_event(senv):
    sid = req(senv, "e1", "Acme")
    mutate(senv, lambda s, a: svc.approve(s, a, sid))
    mutate(senv, lambda s, a: svc.revoke(s, a, sid, "abuse"))
    evs = feed(senv).json()["events"]
    assert [(e["revision"], e["data"]["status"]) for e in evs if e["entity"]["id"] == str(sid)] == [
        (1, "pending"),
        (2, "approved"),
        (3, "revoked"),
    ]


# =============================================================================================================
# feed: faqosje, next_seq, grupe, autorizim
# =============================================================================================================


def test_pagination_and_next_seq_semantics(senv):
    for i in range(5):
        req(senv, "e1", f"Item{i}")
    _e, _f, latest = state(senv)
    p1 = feed(senv, after=0, limit=2).json()
    assert (
        [e["seq"] for e in p1["events"]] == [1, 2]
        and p1["has_more"]
        and p1["next_seq"] == 2
        and p1["latest_seq"] == latest
    )
    p2 = feed(senv, after=p1["next_seq"], limit=2).json()
    assert [e["seq"] for e in p2["events"]] == [3, 4] and p2["has_more"]
    p3 = feed(senv, after=p2["next_seq"], limit=2).json()
    assert (
        [e["seq"] for e in p3["events"]] == [5] and not p3["has_more"] and p3["next_seq"] == latest
    )
    empty = feed(senv, after=latest).json()
    assert empty["events"] == [] and empty["next_seq"] == latest and not empty["has_more"]


def test_a_group_is_never_split_across_pages(senv):
    ids = [req(senv, "e1", f"Grp{i}") for i in range(4)]
    for i in ids:
        mutate(senv, lambda s, a, i=i: svc.approve(s, a, i))
    _e, _f, before = state(senv)
    mutate(
        senv, lambda s, a: svc.set_policy(s, a, "AL", "alphanumeric", False, True, "ban")
    )  # grup: 1 politikë + 4 revokime
    page = feed(senv, after=before, limit=2).json()
    assert (
        len(page["events"]) == 5
        and not page["has_more"]
        and {e["group"]["size"] for e in page["events"]} == {5}
    )
    # me limit që pret grupin në mes të faqes së parë të një lexuesi që fillon më herët
    page = feed(senv, after=before - 1, limit=2).json()
    groups = {}
    for e in page["events"]:
        groups.setdefault(e["group"]["id"], []).append(e)
    assert all(len(v) == v[0]["group"]["size"] for v in groups.values())


def test_authorization_filters_registry_events_but_policies_are_global(senv):
    s1 = req(senv, "e1", "Alpha")
    s2 = req(senv, "e2", "Bravo")
    s3 = req(senv, "e3", "Charlie")
    mutate(senv, lambda s, a: svc.set_policy(s, a, "XK", "numeric", True, False, "open"))
    full = feed(senv).json()["events"]
    ids = {e["entity"]["id"] for e in full}
    assert str(s1) in ids and str(s2) in ids and str(s3) not in ids  # snd: e1,e2
    assert any(e["event_type"] == v1.EVENT_POLICY for e in full)
    one = feed(senv, client="snd1", kid="k2").json()["events"]
    ids1 = {e["entity"]["id"] for e in one}
    assert str(s1) in ids1 and str(s2) not in ids1 and str(s3) not in ids1
    assert any(e["event_type"] == v1.EVENT_POLICY for e in one)  # politika globale
    assert all(e["enterprise_id"] in (None, str(senv.ids["e1"])) for e in one)


def test_scope_isolation_wrong_missing_disabled_revoked_and_replayed_credentials(senv):
    for client, kid, scope in (("syn", "k3", "sync:read"), ("mon", "k4", "money:read")):
        r = senv.get("/internal/sender/state", headers=auth(tok(senv, client, kid, scope)))
        assert r.status_code == 403, client  # skop tjetër
    assert (
        senv.get(
            "/internal/sender/state", headers=auth(tok(senv, "snd", "k1", "sync:read"))
        ).status_code
        == 403
    )  # claim skop i gabuar
    assert senv.get("/internal/sender/state").status_code == 401
    assert senv.get("/internal/sender/state", headers=auth("garbage")).status_code == 401
    t = tok(senv)
    assert senv.get("/internal/sender/state", headers=auth(t)).status_code == 200
    assert senv.get("/internal/sender/state", headers=auth(t)).status_code == 401  # replay i jti
    # sender:read s'hap feed-in cp.v1/money
    t2 = tok(senv)
    assert (
        senv.get(
            "/internal/sync/changes?after_seq=0&epoch=" + str(uuid.uuid4()) + "&generation=1",
            headers=auth(t2),
        ).status_code
        == 403
    )
    with Session(senv.eng) as s:
        service_auth.disable_key(s, "snd1", "k2")
        s.commit()
    assert (
        senv.get("/internal/sender/state", headers=auth(tok(senv, "snd1", "k2"))).status_code == 401
    )
    with Session(senv.eng) as s:
        service_auth.disable_client(s, "snd")
        s.commit()
    assert senv.get("/internal/sender/state", headers=auth(tok(senv))).status_code == 401


def test_epoch_generation_and_cursor_conflicts_require_a_snapshot(senv):
    req(senv, "e1", "Acme")
    r = feed(senv, epoch=str(uuid.uuid4()))
    assert (
        r.status_code == 409
        and r.json()["detail"]["code"] == "sender_epoch_mismatch"
        and r.json()["detail"]["action"] == "snapshot"
    )
    r = feed(senv, generation=gen_of(senv) + 5)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "sender_authorization_changed"
    r = feed(senv, after=10_000)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "sender_cursor_ahead"
    with Session(senv.eng) as s:
        s.execute(text("UPDATE sender_sync_sequence SET floor_seq = last_seq"))
        s.commit()
    r = feed(senv, after=0)
    assert r.status_code == 410 and r.json()["detail"]["code"] == "sender_cursor_expired"
    # malformuar
    ep = str(state(senv)[0])
    for q in (
        f"after_seq=-1&epoch={ep}&generation=1",
        "after_seq=0&epoch=nope&generation=1",
        f"after_seq=0&epoch={ep}&generation=0",
        f"after_seq=0&epoch={ep}&generation=1&limit=0",
    ):
        assert (
            senv.get("/internal/sender/changes?" + q, headers=auth(tok(senv))).status_code == 422
        ), q


def test_authorization_change_bumps_generation_and_forces_snapshot(senv):
    g0 = gen_of(senv, "snd1")
    with Session(senv.eng) as s:
        service_auth.grant_enterprise(s, "snd1", senv.ids["e2"])
        s.commit()
    assert gen_of(senv, "snd1") == g0 + 1
    old = feed(senv, client="snd1", kid="k2", generation=g0)
    assert old.status_code == 409 and old.json()["detail"]["code"] == "sender_authorization_changed"


# =============================================================================================================
# snapshot
# =============================================================================================================


def test_snapshot_is_coherent_authorized_and_carries_only_current_state(senv):
    s1, s2, s3 = req(senv, "e1", "Alpha"), req(senv, "e2", "Bravo"), req(senv, "e3", "Charlie")
    mutate(senv, lambda s, a: svc.approve(s, a, s1))
    mutate(senv, lambda s, a: svc.set_policy(s, a, "AL", "alphanumeric", True, True, "v1"))
    mutate(
        senv, lambda s, a: svc.set_policy(s, a, "AL", "alphanumeric", False, True, "v2")
    )  # revokon s1
    mutate(senv, lambda s, a: svc.set_policy(s, a, "XK", "numeric", True, False, "open"))
    r = senv.get("/internal/sender/snapshot", headers=auth(tok(senv)))
    assert r.status_code == 200
    snap = r.json()
    _e, _f, latest = state(senv)
    assert snap["snapshot_seq"] == latest and snap["authorization_generation"] == gen_of(senv)
    assert {
        (p["data"]["country"], p["data"]["sender_kind"], p["revision"]) for p in snap["policies"]
    } == {("AL", "alphanumeric", 2), ("XK", "numeric", 1)}  # vetëm revizioni aktual
    got = {i["entity"]["id"]: i for i in snap["senders"]}
    assert set(got) == {str(s1), str(s2)} and str(s3) not in got
    assert got[str(s1)]["data"]["status"] == "revoked" and got[str(s1)]["revision"] == 3
    # kontrata: i parsueshëm nga Enterprise
    from app.services import sender_sync as ent_sync

    parsed = ent_sync.parse_snapshot(snap)
    assert parsed.snapshot_seq == latest and len(parsed.policies) == 2 and len(parsed.senders) == 2
    one = senv.get("/internal/sender/snapshot", headers=auth(tok(senv, "snd1", "k2"))).json()
    assert {i["entity"]["id"] for i in one["senders"]} == {str(s1)}


@pytest.mark.skipif(not IS_PG, reason="REPEATABLE READ is PostgreSQL-only")
def test_snapshot_does_not_see_a_commit_that_lands_after_its_boundary(senv, monkeypatch):
    if senv.eng.dialect.name != "postgresql":
        pytest.skip("postgres parametrization only")
    req(senv, "e1", "Early")

    def late_commit():
        with Session(senv.eng) as s:
            svc.request_sender(s, A(s, senv), senv.ids["e1"], "late-ref", "AL", "Later")
            s.commit()

    monkeypatch.setattr(sender_sync, "_after_boundary_hook", late_commit)
    snap = senv.get("/internal/sender/snapshot", headers=auth(tok(senv))).json()
    monkeypatch.setattr(sender_sync, "_after_boundary_hook", None)
    names = {i["data"]["display_value"] for i in snap["senders"]}
    assert names == {"Early"} and snap["snapshot_seq"] == 1  # asnjë gjendje më e re se kufiri
    assert (
        feed(senv, after=snap["snapshot_seq"]).json()["events"][0]["data"]["display_value"]
        == "Later"
    )  # feed-i e jep pas kufirit


# =============================================================================================================
# PG: imutabilitet dhe konkurrencë
# =============================================================================================================


def test_outbox_is_append_only_in_orm_and_in_postgres(senv):
    req(senv, "e1", "Acme")
    with Session(senv.eng) as s:
        row = s.scalar(select(SenderSyncOutbox))
        row.revision = 99
        with pytest.raises(SenderImmutableError):
            s.flush()
        s.rollback()
    if senv.eng.dialect.name != "postgresql":
        return
    for sql in (
        "UPDATE sender_sync_outbox SET revision = 5",
        "DELETE FROM sender_sync_outbox",
        "TRUNCATE sender_sync_outbox",
        "DELETE FROM sender_sync_sequence",
        "TRUNCATE sender_sync_sequence",
    ):
        with senv.eng.connect() as c:
            with pytest.raises(DBAPIError):
                c.execute(text(sql))
            c.rollback()


@pytest.mark.skipif(not IS_PG, reason="postgres concurrency")
def test_concurrent_mutations_allocate_unique_consecutive_seqs(senv):
    if senv.eng.dialect.name != "postgresql":
        pytest.skip("postgres parametrization only")
    gate = threading.Barrier(8)
    errs = []

    def go(i):
        with Session(senv.eng) as s:
            try:
                gate.wait(timeout=20)
                svc.request_sender(
                    s, A(s, senv), senv.ids["e1" if i % 2 else "e2"], f"c-{i}", "AL", f"Race{i}"
                )
                s.commit()
            except Exception as e:  # noqa: BLE001
                s.rollback()
                errs.append(e)

    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, errs
    with Session(senv.eng) as s:
        seqs = sorted(s.scalars(select(SenderSyncOutbox.seq)))
    assert seqs == list(range(1, 9)) and state(senv)[2] == 8
