"""M7-c — Central: auth shërbim-te-shërbim (Ed25519), feed i ndryshimeve, snapshot, epoka, generation."""

import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session

from apps.central.core import errors
from apps.central.core.db import Base
from apps.central.main import create_app
from apps.central.models import ServiceAssertionJti, ServiceClient, SyncOutbox
from apps.central.services import enterprise_products as asg
from apps.central.services import enterprises as ent
from apps.central.services import products as prod
from apps.central.services import service_auth, sync_contract, sync_feed
from packages.contracts.control_plane import v1
from tests.test_central import IS_PG, ROOT, central_alembic, make_db  # noqa: F401
from tests.test_central_auth import auth_secret  # noqa: F401  (fixture autouse)

AUD = "sms-central-sync"


def keypair():
    priv = Ed25519PrivateKey.generate()
    private_pem = priv.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public_pem = priv.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()  # fmt: skip
    return private_pem, public_pem


def assertion(private_pem, client="ent-main", kid="k1", *, scope="sync:read", aud=AUD, lifetime=120,
              iat=None, jti=None, sub=None, extra=None, alg="EdDSA", headers=None):  # fmt: skip
    now = int((iat or datetime.now(UTC)).timestamp())
    claims = {"iss": client, "sub": sub or client, "aud": aud, "iat": now, "exp": now + lifetime,
              "jti": jti or uuid.uuid4().hex, "scope": scope, **(extra or {})}  # fmt: skip
    return jwt.encode(claims, private_pem, algorithm=alg, headers={"kid": kid, **(headers or {})})


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def env(make_db):  # noqa: F811
    url = make_db()
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    private, public = keypair()
    with Session(eng, expire_on_commit=False) as s:
        e1, e2, e3 = ent.create(s, "Acme"), ent.create(s, "Beta"), ent.create(s, "Gamma")
        p_sms = prod.create(s, "sms", "SMS", "sms")
        p_email = prod.create(s, "email", "Email", "email")
        service_auth.create_client(s, "ent-main", ["sync:read"], [e1.id, e2.id])
        service_auth.add_key(s, "ent-main", "k1", public)
        s.commit()
    c = TestClient(create_app(eng))
    c.eng, c.private, c.public = eng, private, public
    c.ids = {"e1": e1.id, "e2": e2.id, "e3": e3.id, "sms": p_sms.id, "email": p_email.id}
    c.url = url
    yield c
    eng.dispose()


def tok(env, **kw):
    return assertion(env.private, **kw)


def state(env):
    with Session(env.eng) as s:
        epoch, floor, last = sync_feed.read_state(s)
        gen = s.scalar(select(ServiceClient.auth_generation))
    return str(epoch), floor, last, gen


def feed(env, after=0, limit=100, epoch=None, generation=None, token=None):
    ep, _f, _l, gen = state(env)
    q = f"after_seq={after}&limit={limit}&epoch={epoch or ep}&generation={generation or gen}"
    return env.get("/internal/sync/changes?" + q, headers=auth(token or tok(env)))


def mutate(env, fn):
    with Session(env.eng, expire_on_commit=False) as s:
        out = fn(s)
        s.commit()
        return out


# --- auth: pranim dhe refuzime ----------------------------------------------------------------------------


def test_valid_ed25519_assertion_is_accepted(env):
    r = env.get("/internal/sync/snapshot", headers=auth(tok(env)))
    assert r.status_code == 200 and r.json()["snapshot_seq"] >= 0


def test_header_and_claims_are_exactly_as_specified(env):
    t = tok(env)
    h = jwt.get_unverified_header(t)
    c = jwt.decode(t, options={"verify_signature": False})
    assert h["alg"] == "EdDSA" and h["kid"] == "k1"
    assert c["iss"] == c["sub"] == "ent-main" and c["aud"] == AUD and c["scope"] == "sync:read"
    assert c["exp"] - c["iat"] <= 300 and len(c["jti"]) >= 8


def _denied(r, status=401):
    assert r.status_code == status
    return r


def test_wrong_signature_unknown_kid_unknown_client(env):
    other_private, _ = keypair()
    _denied(
        env.get("/internal/sync/snapshot", headers=auth(assertion(other_private)))
    )  # çelës tjetër
    _denied(env.get("/internal/sync/snapshot", headers=auth(tok(env, kid="nope"))))
    _denied(env.get("/internal/sync/snapshot", headers=auth(tok(env, client="ghost"))))
    r = env.get("/internal/sync/snapshot", headers=auth(tok(env, kid="nope")))
    r2 = env.get("/internal/sync/snapshot", headers=auth(tok(env, client="ghost")))
    assert r.json() == r2.json()  # pa zbulim të regjistrit (e njëjta përgjigje gjenerike)
    assert (
        r.json()["detail"]["code"] == "unauthorized" and r.headers["www-authenticate"] == "Bearer"
    )


def test_disabled_client_and_disabled_key(env):
    mutate(env, lambda s: service_auth.disable_key(s, "ent-main", "k1"))
    _denied(env.get("/internal/sync/snapshot", headers=auth(tok(env))))
    mutate(env, lambda s: service_auth.add_key(s, "ent-main", "k2", env.public))
    env.k2 = True
    assert env.get("/internal/sync/snapshot", headers=auth(tok(env, kid="k2"))).status_code == 200
    mutate(env, lambda s: service_auth.disable_client(s, "ent-main"))
    _denied(env.get("/internal/sync/snapshot", headers=auth(tok(env, kid="k2"))))


def test_expired_too_long_and_future_assertions(env):
    past = datetime.now(UTC) - timedelta(hours=1)
    _denied(env.get("/internal/sync/snapshot", headers=auth(tok(env, iat=past))))  # skaduar
    _denied(env.get("/internal/sync/snapshot", headers=auth(tok(env, lifetime=301))))  # > 300 s
    _denied(env.get("/internal/sync/snapshot", headers=auth(tok(env, lifetime=3600))))
    assert (
        env.get("/internal/sync/snapshot", headers=auth(tok(env, lifetime=300))).status_code == 200
    )
    future = datetime.now(UTC) + timedelta(hours=1)
    _denied(env.get("/internal/sync/snapshot", headers=auth(tok(env, iat=future))))


def test_wrong_audience_sub_and_scope(env):
    _denied(env.get("/internal/sync/snapshot", headers=auth(tok(env, aud="other"))))
    _denied(env.get("/internal/sync/snapshot", headers=auth(tok(env, sub="someone-else"))))
    r = env.get(
        "/internal/sync/snapshot", headers=auth(tok(env, scope="admin:write"))
    )  # mungon sync:read
    assert r.status_code == 403 and r.json()["detail"]["code"] == "forbidden"
    mutate(env, lambda s: s.query(ServiceClient).update({"scopes": []}))  # klienti s'e ka scope-in
    assert env.get("/internal/sync/snapshot", headers=auth(tok(env))).status_code == 403
    mutate(env, lambda s: s.query(ServiceClient).update({"scopes": ["sync:read"]}))
    assert (
        env.get(
            "/internal/sync/snapshot", headers=auth(tok(env, scope="a sync:read b"))
        ).status_code
        == 200
    )


def test_replayed_jti_is_rejected_even_across_endpoints(env):
    t = tok(env, jti="fixed-jti-0001")
    assert env.get("/internal/sync/snapshot", headers=auth(t)).status_code == 200
    r = env.get("/internal/sync/snapshot", headers=auth(t))
    assert r.status_code == 401 and r.json()["detail"]["code"] == "unauthorized"
    ep, _f, _l, gen = state(env)
    assert (
        env.get(
            f"/internal/sync/changes?after_seq=0&epoch={ep}&generation={gen}", headers=auth(t)
        ).status_code
        == 401
    )
    assert (
        env.get("/internal/sync/snapshot", headers=auth(tok(env, jti="fixed-jti-0002"))).status_code
        == 200
    )


def test_jti_survives_a_failed_request_and_expired_rows_are_cleaned_lazily(env):
    t = tok(env, jti="persist-jti-01")
    bad = env.get(
        "/internal/sync/changes?after_seq=0&epoch=" + str(uuid.uuid4()) + "&generation=1",
        headers=auth(t),
    )
    assert bad.status_code == 409  # kërkesa dështoi...
    assert (
        env.get("/internal/sync/snapshot", headers=auth(t)).status_code == 401
    )  # ...jti ishte konsumuar
    with Session(env.eng) as s:
        client = s.scalar(select(ServiceClient))
        s.add(
            ServiceAssertionJti(
                client_pk=client.id,
                jti="old-expired-jti",
                expires_at=datetime(2000, 1, 1, tzinfo=UTC),
            )
        )
        s.commit()
    env.get("/internal/sync/snapshot", headers=auth(tok(env)))
    with Session(env.eng) as s:
        assert (
            s.scalar(
                select(func.count())
                .select_from(ServiceAssertionJti)
                .where(ServiceAssertionJti.jti == "old-expired-jti")
            )
            == 0
        )


@pytest.mark.parametrize("header", [{}, {"Authorization": ""}, {"Authorization": "Bearer"}, {"Authorization": "Bearer garbage"},
                                    {"Authorization": "Bearer a.b.c"}, {"Authorization": "Basic abc"}])  # fmt: skip
def test_no_token_and_malformed_tokens(env, header):
    for path in (
        "/internal/sync/snapshot",
        "/internal/sync/changes?after_seq=0&epoch=" + str(uuid.uuid4()) + "&generation=1",
    ):
        assert env.get(path, headers=header).status_code in (401, 422)
    assert env.get("/internal/sync/snapshot", headers=header).status_code == 401


def test_algorithm_confusion_and_none_are_rejected(env):
    _denied(
        env.get(
            "/internal/sync/snapshot",
            headers=auth(
                jwt.encode({"iss": "ent-main"}, None, algorithm="none", headers={"kid": "k1"})
            ),
        )
    )
    hs = jwt.encode({"iss": "ent-main", "sub": "ent-main", "aud": AUD, "iat": int(time.time()), "exp": int(time.time()) + 60,
                     "jti": "hs256-token-01", "scope": "sync:read"}, "x" * 48, algorithm="HS256", headers={"kid": "k1"})  # fmt: skip
    _denied(env.get("/internal/sync/snapshot", headers=auth(hs)))


def test_enterprise_user_jwt_and_staff_tokens_are_not_service_tokens(env):
    from apps.central.core import tokens

    staff, _ = tokens.issue(uuid.uuid4())
    _denied(env.get("/internal/sync/snapshot", headers=auth(staff)))


def test_token_and_signature_are_never_logged(env, caplog):
    caplog.set_level(logging.DEBUG)
    good, other = tok(env), assertion(keypair()[0])
    env.get("/internal/sync/snapshot", headers=auth(good))
    env.get("/internal/sync/snapshot", headers=auth(other))
    env.get("/internal/sync/snapshot", headers=auth(good))  # replay
    env.get("/internal/sync/snapshot", headers=auth(tok(env, scope="x")))
    text_ = caplog.text
    assert (
        "reason=replayed" in text_
        and "reason=bad_signature" in text_
        and "reason=scope_denied" in text_
    )
    for t in (good, other):
        assert t not in text_ and t.split(".")[2] not in text_ and t.split(".")[1] not in text_
    assert "BEGIN" not in text_ and env.private not in text_


def test_key_rotation_two_active_kids_then_retire_the_old_one(env):
    new_private, new_public = keypair()
    mutate(env, lambda s: service_auth.add_key(s, "ent-main", "k2", new_public))
    assert env.get("/internal/sync/snapshot", headers=auth(tok(env))).status_code == 200  # k1 ende
    assert (
        env.get(
            "/internal/sync/snapshot", headers=auth(assertion(new_private, kid="k2"))
        ).status_code
        == 200
    )
    _denied(
        env.get("/internal/sync/snapshot", headers=auth(assertion(new_private, kid="k1")))
    )  # çelës i gabuar për kid
    mutate(env, lambda s: service_auth.disable_key(s, "ent-main", "k1"))
    _denied(env.get("/internal/sync/snapshot", headers=auth(tok(env))))
    assert (
        env.get(
            "/internal/sync/snapshot", headers=auth(assertion(new_private, kid="k2"))
        ).status_code
        == 200
    )


def test_only_public_keys_are_stored_and_only_ed25519_is_accepted(env):
    from cryptography.hazmat.primitives.asymmetric import rsa

    with env.eng.connect() as c:
        pem = c.execute(text("select public_key from service_keys")).scalar()
    assert "BEGIN PUBLIC KEY" in pem and "PRIVATE" not in pem
    rsa_pem = rsa.generate_private_key(65537, 2048).public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )  # fmt: skip
    with Session(env.eng) as s:
        for bad in (rsa_pem, b"junk", env.private.encode()):
            with pytest.raises(errors.Invalid):
                service_auth.add_key(s, "ent-main", "k9", bad)


# --- autorizimi: objektivat dhe generation -----------------------------------------------------------------------


def test_snapshot_contains_only_authorized_enterprises_and_filter_returns_403_for_others(env):
    ids = env.ids
    snap = env.get("/internal/sync/snapshot", headers=auth(tok(env))).json()
    assert {x["enterprise_id"] for x in snap["enterprises"]} == {str(ids["e1"]), str(ids["e2"])}
    one = env.get(
        f"/internal/sync/snapshot?enterprise_id={ids['e1']}", headers=auth(tok(env))
    ).json()
    assert [x["enterprise_id"] for x in one["enterprises"]] == [str(ids["e1"])]
    r = env.get(f"/internal/sync/snapshot?enterprise_id={ids['e3']}", headers=auth(tok(env)))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "forbidden"
    assert (
        env.get(
            "/internal/sync/snapshot?enterprise_id=not-a-uuid", headers=auth(tok(env))
        ).status_code
        == 422
    )


def test_multi_enterprise_credential_and_authorization_generation(env):
    ids = env.ids
    ep, _f, _l, g1 = state(env)
    assert g1 == 3  # krijim (1) + 2 grant-e (rritje te çdo ndryshim real)
    mutate(
        env, lambda s: [service_auth.grant_enterprise(s, "ent-main", ids["e1"]) for _ in range(2)]
    )  # no-op
    assert state(env)[3] == g1
    assert feed(env, generation=g1).status_code == 200
    mutate(env, lambda s: service_auth.grant_enterprise(s, "ent-main", ids["e3"]))
    g2 = state(env)[3]
    assert g2 == g1 + 1
    old = feed(env, generation=g1)  # konsumatori me generation të vjetër
    assert old.status_code == 409 and old.json()["detail"] == {
        "code": "sync_authorization_changed", "message": "authorization changed; a full snapshot is required", "action": "snapshot"}  # fmt: skip
    snap = env.get("/internal/sync/snapshot", headers=auth(tok(env))).json()
    assert snap["authorization_generation"] == g2 and len(snap["enterprises"]) == 3
    assert (
        feed(
            env, after=snap["snapshot_seq"], generation=snap["authorization_generation"]
        ).status_code
        == 200
    )
    mutate(env, lambda s: service_auth.revoke_enterprise(s, "ent-main", ids["e2"]))
    assert state(env)[3] == g2 + 1 and feed(env, generation=g2).status_code == 409
    assert {
        x["enterprise_id"]
        for x in env.get("/internal/sync/snapshot", headers=auth(tok(env))).json()["enterprises"]
    } == {str(ids["e1"]), str(ids["e3"])}


def test_newly_granted_enterprise_history_is_not_lost_it_arrives_via_snapshot(env):
    ids = env.ids
    mutate(
        env, lambda s: ent.rename(s, ids["e3"], "Gamma 2")
    )  # ngjarje e enterprise-it ende të paautorizuar
    _ep, _f, last_before, g = state(env)
    assert [e["data"]["id"] for e in feed(env, generation=g).json()["events"]] != [str(ids["e3"])]
    mutate(env, lambda s: service_auth.grant_enterprise(s, "ent-main", ids["e3"]))
    snap = env.get("/internal/sync/snapshot", headers=auth(tok(env))).json()
    gamma = [x for x in snap["enterprises"] if x["enterprise_id"] == str(ids["e3"])][0]
    assert (
        gamma["data"]["name"] == "Gamma 2" and gamma["revision"] == 2
    )  # gjendja e tanishme, jo historia


# --- feed ------------------------------------------------------------------------------------------------------------


def seed_events(env):
    ids = env.ids

    def run(s):
        ent.rename(s, ids["e1"], "Acme 2")  # seq 1 (e1)
        ent.rename(s, ids["e3"], "Gamma 2")  # seq 2 (e3, i paautorizuar)
        ep, _ = asg.assign_product(s, ids["e1"], ids["sms"])  # seq 3
        asg.assign_product(s, ids["e3"], ids["sms"])  # seq 4 (e3)
        ent.suspend(s, ids["e2"])  # seq 5
        asg.suspend_assignment(s, ids["e1"], ep.id)  # seq 6
        return ep.id

    return mutate(env, run)


def test_feed_returns_ordered_events_exact_cp_v1_and_filters_other_tenants(env):
    mutate(env, lambda s: None)
    base = state(env)[2]
    seed_events(env)
    body = feed(env, after=base).json()
    seqs = [e["seq"] for e in body["events"]]
    assert seqs == [base + 1, base + 3, base + 5, base + 6] and seqs == sorted(
        seqs
    )  # boshllëqe nga e3
    assert all(e["schema"] == "cp.v1" for e in body["events"])
    assert {e["enterprise_id"] for e in body["events"]} == {str(env.ids["e1"]), str(env.ids["e2"])}
    assert str(env.ids["e3"]) not in str(body)  # asnjë rrjedhje e tenant-it tjetër
    with Session(env.eng) as s:
        rows = list(
            s.scalars(select(SyncOutbox).where(SyncOutbox.seq.in_(seqs)).order_by(SyncOutbox.seq))
        )
        assert [e for e in body["events"]] == [
            sync_contract.to_event(r).to_dict() for r in rows
        ]  # nga outbox + mapper
        assert all(v1.ControlPlaneEventV1.from_dict(e) for e in body["events"])
        for e, r in zip(body["events"], rows, strict=True):
            assert (e["event_id"], e["seq"], e["revision"]) == (
                str(r.event_id),
                r.seq,
                r.revision,
            )  # të paprekura


def test_feed_pagination_next_seq_and_cursor_semantics(env):
    base = state(env)[2]
    seed_events(env)
    ep, floor, latest, gen = state(env)
    p1 = feed(env, after=base, limit=2).json()
    assert [e["seq"] for e in p1["events"]] == [base + 1, base + 3] and p1["has_more"] is True
    assert p1["next_seq"] == base + 3  # faqe e plotë me më shumë: kursori = seq i fundit i kthyer
    p2 = feed(env, after=p1["next_seq"], limit=2).json()
    assert [e["seq"] for e in p2["events"]] == [base + 5, base + 6] and p2["has_more"] is False
    assert p2["next_seq"] == latest == p2["latest_seq"] and p2["oldest_available_seq"] == floor + 1
    assert p2["epoch"] == ep and p2["authorization_generation"] == gen
    p3 = feed(env, after=p2["next_seq"]).json()
    assert (
        p3["events"] == [] and p3["next_seq"] == latest and p3["has_more"] is False
    )  # s'lëviz mbrapa
    for bad in ("after_seq=-1", "limit=0", "limit=501"):
        r = env.get(
            f"/internal/sync/changes?after_seq=0&epoch={ep}&generation={gen}&{bad}".replace(
                "after_seq=0&epoch", "epoch"
            )
            if bad.startswith("after")
            else f"/internal/sync/changes?after_seq=0&epoch={ep}&generation={gen}&{bad}",
            headers=auth(tok(env)),
        )
        assert r.status_code == 422


def test_cursor_advances_over_other_tenants_changes_to_the_boundary(env):
    base = state(env)[2]
    mutate(env, lambda s: ent.rename(s, env.ids["e3"], "Only other tenant"))
    body = feed(env, after=base).json()
    assert body["events"] == [] and body["latest_seq"] == base + 1 and body["next_seq"] == base + 1


def test_feed_requires_matching_epoch_and_a_valid_cursor(env):
    ep, floor, latest, gen = state(env)
    r = feed(env, epoch=str(uuid.uuid4()))
    assert (
        r.status_code == 409
        and r.json()["detail"]["code"] == "sync_epoch_mismatch"
        and r.json()["detail"]["action"] == "snapshot"
    )
    r = feed(env, after=latest + 5)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "sync_cursor_ahead"
    assert (
        env.get(
            f"/internal/sync/changes?after_seq=0&generation={gen}", headers=auth(tok(env))
        ).status_code
        == 422
    )  # epoch e detyrueshme
    assert (
        env.get(
            f"/internal/sync/changes?after_seq=0&epoch={ep}", headers=auth(tok(env))
        ).status_code
        == 422
    )  # generation e detyrueshme


def test_expired_cursor_returns_410_never_partial_history(env):
    base = state(env)[2]
    seed_events(env)
    _ep, _f, latest, gen = state(env)
    with (
        env.eng.begin() as c
    ):  # gjendje artificiale pas "pastrimit": historia e plotë vetëm nga seq 4
        c.execute(text("update sync_sequence set floor_seq = :f"), {"f": base + 3})
    r = feed(env, after=base)
    assert r.status_code == 410 and r.json()["detail"]["code"] == "sync_cursor_expired"
    assert r.json()["detail"]["action"] == "snapshot" and "events" not in r.json()
    ok = feed(env, after=base + 3).json()
    assert ok["oldest_available_seq"] == base + 4 and [e["seq"] for e in ok["events"]] == [
        base + 5,
        base + 6,
    ]
    assert feed(env, after=base + 2).status_code == 410


def test_epoch_is_persistent_and_changes_only_by_explicit_rotation(env):
    first = state(env)[0]
    assert state(env)[0] == first and uuid.UUID(first).version == 4
    env2 = TestClient(create_app(env.eng))  # "restart" i procesit
    assert env2.get("/internal/sync/snapshot", headers=auth(tok(env))).json()["epoch"] == first
    mutate(env, lambda s: ent.rename(s, env.ids["e1"], "X"))
    assert state(env)[0] == first  # mutacionet s'e ndryshojnë
    new = mutate(env, sync_feed.rotate_epoch)
    assert str(new) == state(env)[0] != first
    assert feed(env, epoch=first).status_code == 409


def test_feed_is_read_only_and_has_no_ack_or_mutation_routes(env):
    paths = env.app.openapi()["paths"]
    sync_paths = {p: set(m) for p, m in paths.items() if p.startswith("/internal")}
    assert sync_paths == {"/internal/sync/changes": {"get"}, "/internal/sync/snapshot": {"get"}}
    for method in ("post", "put", "patch", "delete"):
        assert getattr(env, method)(
            "/internal/sync/changes", headers=auth(tok(env))
        ).status_code in (404, 405)
    # asnjë gjendje konsumatori në Central
    assert "consumer" not in " ".join(inspect(env.eng).get_table_names())


# --- snapshot ---------------------------------------------------------------------------------------------------------


def test_snapshot_current_state_assignments_and_boundary(env):
    ids = env.ids
    ep_id = seed_events(env)
    mutate(env, lambda s: asg.assign_product(s, ids["e1"], ids["email"]))  # assignment email te e1
    snap = env.get("/internal/sync/snapshot", headers=auth(tok(env))).json()
    ep, _f, latest, gen = state(env)
    assert (
        snap["snapshot_seq"] == latest
        and snap["epoch"] == ep
        and snap["authorization_generation"] == gen
    )
    by_id = {x["enterprise_id"]: x for x in snap["enterprises"]}
    assert set(by_id) == {str(ids["e1"]), str(ids["e2"])}
    assert by_id[str(ids["e1"])]["data"] == {
        "id": str(ids["e1"]),
        "name": "Acme 2",
        "status": "active",
    }
    assert by_id[str(ids["e2"])]["data"]["status"] == "suspended"
    assert by_id[str(ids["e1"])]["revision"] == 2 and by_id[str(ids["e1"])]["entity"] == {
        "type": "enterprise",
        "id": str(ids["e1"]),
    }
    a = {x["entity"]["id"]: x for x in snap["assignments"]}
    assert len(a) == 2 and all(x["enterprise_id"] in by_id for x in a.values())  # e3 s'është këtu
    sms = a[str(ep_id)]
    assert sms["data"] == {"assignment_id": str(ep_id), "enterprise_id": str(ids["e1"]),
                           "product": {"id": str(ids["sms"]), "code": "sms", "channel": "sms"}, "status": "suspended", "rate_limit_per_min": None}  # fmt: skip
    assert sms["revision"] == 2
    email = [x for x in a.values() if x["data"]["product"]["code"] == "email"][0]
    assert email["data"]["product"]["channel"] == "email" and email["data"]["status"] == "active"
    assert set(snap) == {
        "epoch",
        "authorization_generation",
        "snapshot_seq",
        "enterprises",
        "assignments",
    }
    assert "products" not in snap and "catalog" not in str(snap)  # katalogu s'dërgohet
    for x in snap["enterprises"]:
        v1.EnterpriseStateV1.from_dict(x["data"])
    for x in snap["assignments"]:
        v1.EnterpriseProductStateV1.from_dict(x["data"])


def test_snapshot_then_feed_loses_nothing_sequentially(env):
    ids = env.ids
    seed_events(env)
    snap = env.get("/internal/sync/snapshot", headers=auth(tok(env))).json()
    mutate(env, lambda s: ent.rename(s, ids["e1"], "After snapshot"))
    mutate(env, lambda s: ent.rename(s, ids["e3"], "Other tenant"))
    mutate(env, lambda s: ent.suspend(s, ids["e1"]))
    body = feed(
        env,
        after=snap["snapshot_seq"],
        generation=snap["authorization_generation"],
        epoch=snap["epoch"],
    ).json()
    assert [
        (e["data"]["name"] if e["type"] == "enterprise.upserted" else None, e["revision"])
        for e in body["events"]
    ] == [("After snapshot", 3), ("After snapshot", 4)]
    # zbatimi: gjendja finale = snapshot + feed me rregullin e revision
    state_by_id = {x["enterprise_id"]: x["revision"] for x in snap["enterprises"]}
    for e in body["events"]:
        assert e["revision"] > state_by_id[e["enterprise_id"]]
        state_by_id[e["enterprise_id"]] = e["revision"]


def test_snapshot_cli_modules_exist_and_no_enterprise_imports():
    import ast

    banned = {"app", "httpx", "requests", "celery", "redis", "asyncio"}
    for name in ("services/service_auth.py", "services/sync_feed.py", "api/internal_sync.py",
                 "tools/create_service_credential.py", "tools/service_credential_admin.py",
                 "models/service_auth.py"):  # fmt: skip
        for n in ast.walk(ast.parse((ROOT / "apps/central" / name).read_text())):
            mods = ([n.module] if isinstance(n, ast.ImportFrom) and n.module else
                    [a.name for a in n.names] if isinstance(n, ast.Import) else [])  # fmt: skip
            assert not [m for m in mods if m.split(".")[0] in banned], (name, mods)


# --- CLI -------------------------------------------------------------------------------------------------------------


def _cli(module, env_url, *args):
    e = {**os.environ, "CENTRAL_DATABASE_URL": env_url}
    return subprocess.run([sys.executable, "-m", f"apps.central.tools.{module}", *args], env=e,
                          cwd=ROOT, capture_output=True, text=True)  # fmt: skip


def test_create_service_credential_and_admin_cli(env, tmp_path):
    _, pub = keypair()
    f = tmp_path / "svc.pub.pem"
    f.write_text(pub)
    e1 = str(env.ids["e1"])
    r = _cli("create_service_credential", env.url, "--client-id", "other-svc", "--kid", "a1",
             "--public-key-file", str(f), "--enterprise", e1)  # fmt: skip
    assert r.returncode == 0 and "created client other-svc" in r.stdout
    again = _cli(
        "create_service_credential",
        env.url,
        "--client-id",
        "other-svc",
        "--kid",
        "a1",
        "--public-key-file",
        str(f),
    )
    assert again.returncode == 0 and "unchanged" in again.stdout
    _, pub2 = keypair()
    f2 = tmp_path / "other.pub.pem"
    f2.write_text(pub2)
    clash = _cli(
        "create_service_credential",
        env.url,
        "--client-id",
        "other-svc",
        "--kid",
        "a1",
        "--public-key-file",
        str(f2),
    )
    assert clash.returncode == 1 and "conflict" in clash.stderr
    rot = _cli(
        "create_service_credential",
        env.url,
        "--client-id",
        "other-svc",
        "--kid",
        "a2",
        "--public-key-file",
        str(f2),
    )
    assert rot.returncode == 0 and "added key a2" in rot.stdout
    blocked = _cli(
        "create_service_credential",
        env.url,
        "--client-id",
        "other-svc",
        "--kid",
        "a3",
        "--public-key-file",
        str(f2),
        "--enterprise",
        e1,
    )
    assert blocked.returncode == 2 and "service_credential_admin" in blocked.stderr
    priv = tmp_path / "priv.pem"
    priv.write_text(env.private)
    leak = _cli(
        "create_service_credential",
        env.url,
        "--client-id",
        "x-svc",
        "--kid",
        "k",
        "--public-key-file",
        str(priv),
    )
    assert (
        leak.returncode == 2
        and "PRIVATE" not in leak.stdout + leak.stderr
        and env.private not in leak.stderr
    )
    grant = _cli(
        "service_credential_admin",
        env.url,
        "grant",
        "--client-id",
        "other-svc",
        "--enterprise",
        str(env.ids["e2"]),
    )
    assert grant.returncode == 0 and "changed (auth_generation=3)" in grant.stdout
    assert (
        "no change"
        in _cli(
            "service_credential_admin",
            env.url,
            "grant",
            "--client-id",
            "other-svc",
            "--enterprise",
            str(env.ids["e2"]),
        ).stdout
    )
    assert (
        "changed"
        in _cli(
            "service_credential_admin",
            env.url,
            "disable-key",
            "--client-id",
            "other-svc",
            "--kid",
            "a1",
        ).stdout
    )
    assert (
        _cli(
            "service_credential_admin", env.url, "grant", "--client-id", "ghost", "--enterprise", e1
        ).returncode
        == 2
    )


# --- migrim / readiness / izolim ------------------------------------------------------------------------------------


def test_migrations_0008_to_0010_up_down_up_and_readiness(make_db):  # noqa: F811
    url = make_db()
    eng = create_engine(url)
    c = TestClient(create_app(eng))
    central_alembic(url, "upgrade", "0007")
    r = c.get("/readyz")
    assert r.status_code == 503 and "not at the expected version" in r.json()["reason"]
    central_alembic(url, "upgrade", "head")
    assert c.get("/readyz").json() == {"status": "ready"}
    tables = set(inspect(eng).get_table_names())
    assert {
        "service_clients",
        "service_keys",
        "service_client_enterprises",
        "service_assertion_jti",
        "registration_requests",
        "registration_products",
    } <= tables
    with eng.connect() as conn:
        row = conn.execute(text("select id, last_seq, floor_seq, epoch from sync_sequence")).one()
    assert (row[0], row[1], row[2]) == (1, 0, 0) and row[
        3
    ] is not None  # epoka gjenerohet në migrim
    central_alembic(url, "downgrade", "0007")
    assert not {"service_clients", "service_assertion_jti"} & set(inspect(eng).get_table_names())
    assert "epoch" not in {col["name"] for col in inspect(eng).get_columns("sync_sequence")}
    assert c.get("/readyz").status_code == 503
    central_alembic(url, "upgrade", "head")
    assert c.get("/readyz").status_code == 200


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_metadata_matches_schema_and_tables_stay_in_central_db(make_db):  # noqa: F811
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    url = make_db()
    if not url.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    central_alembic(url, "upgrade", "head")
    with create_engine(url).connect() as conn:
        ctx = MigrationContext.configure(
            conn, opts={"compare_type": True, "version_table": "central_alembic_version"}
        )
        assert compare_metadata(ctx, Base.metadata) == []


def test_metadata_isolation():
    import app.models  # noqa: F401
    from app.core.db import Base as EnterpriseBase

    new = {"service_clients", "service_keys", "service_client_enterprises", "service_assertion_jti"}
    assert new <= set(Base.metadata.tables) and not new & set(EnterpriseBase.metadata.tables)


# --- PostgreSQL: konsistenca e snapshot-it dhe handoff-i ----------------------------------------------------------


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_snapshot_is_consistent_with_its_boundary_while_a_change_commits_midway(env):
    """Ndryshim që commit-ohet pas leximit të kufirit por para leximit të gjendjes: s'duhet të dalë
    gjysmë-konsistent. REPEATABLE READ: as kufiri as gjendja nuk e shohin; feed pas kufirit e sjell."""
    if not env.url.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    ids = env.ids
    boundary_read, writer_done = threading.Event(), threading.Event()

    def hook():
        boundary_read.set()
        assert writer_done.wait(20)

    def writer():
        assert boundary_read.wait(20)
        with Session(env.eng, expire_on_commit=False) as s:
            ent.rename(s, ids["e1"], "Committed midway")
            s.commit()
        writer_done.set()

    sync_feed._after_boundary_hook = hook
    t = threading.Thread(target=writer)
    t.start()
    try:
        snap = env.get("/internal/sync/snapshot", headers=auth(tok(env))).json()
    finally:
        sync_feed._after_boundary_hook = None
        t.join(20)
    assert writer_done.is_set()
    e1 = [x for x in snap["enterprises"] if x["enterprise_id"] == str(ids["e1"])][0]
    assert (
        e1["data"]["name"] == "Acme" and e1["revision"] == 1
    )  # gjendja përputhet me kufirin (pa ndryshimin)
    after = feed(
        env,
        after=snap["snapshot_seq"],
        generation=snap["authorization_generation"],
        epoch=snap["epoch"],
    ).json()
    assert [(e["data"]["name"], e["revision"]) for e in after["events"]] == [
        ("Committed midway", 2)
    ]  # as dhe jo asnjëri


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_negative_control_read_committed_would_be_inconsistent(env):
    """Provon që testi është i ndjeshëm: pa REPEATABLE READ gjendja do përmbante ndryshimin ndërsa kufiri jo."""
    if not env.url.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    ids = env.ids
    boundary_read, writer_done = threading.Event(), threading.Event()

    def hook():
        boundary_read.set()
        assert writer_done.wait(20)

    def writer():
        assert boundary_read.wait(20)
        with Session(env.eng, expire_on_commit=False) as s:
            ent.rename(s, ids["e1"], "Committed midway")
            s.commit()
        writer_done.set()

    base = state(env)[2]
    sync_feed._after_boundary_hook = hook
    t = threading.Thread(target=writer)
    t.start()
    try:
        with Session(env.eng) as s:  # READ COMMITTED (parazgjedhja)
            client_pk = s.scalar(select(ServiceClient.id))
            snap = sync_feed.snapshot(s, client_pk)
    finally:
        sync_feed._after_boundary_hook = None
        t.join(20)
    e1 = [x for x in snap["enterprises"] if x["enterprise_id"] == str(ids["e1"])][0]
    assert e1["revision"] == 2  # gjendja e re, por snapshot_seq e vjetër → gjysmë-konsistent
    assert snap["snapshot_seq"] == base


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_snapshot_does_not_see_an_uncommitted_change_and_feed_delivers_it_after_commit(env):
    """Tx me seq të alokuar por pa commit gjatë snapshot-it: snapshot_seq e përjashton; pas commit-it hyn në feed."""
    if not env.url.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    ids = env.ids
    base = state(env)[2]
    allocated, release = threading.Event(), threading.Event()

    def pending_writer():
        with Session(env.eng, expire_on_commit=False) as s:
            ent.rename(s, ids["e1"], "Pending")  # alokon seq 1, pa commit
            allocated.set()
            assert release.wait(20)
            s.commit()

    t = threading.Thread(target=pending_writer)
    t.start()
    assert allocated.wait(20)
    snap = env.get("/internal/sync/snapshot", headers=auth(tok(env))).json()
    assert snap["snapshot_seq"] == base
    assert [
        x["data"]["name"] for x in snap["enterprises"] if x["enterprise_id"] == str(ids["e1"])
    ] == ["Acme"]
    release.set()
    t.join(20)
    after = feed(
        env, after=base, generation=snap["authorization_generation"], epoch=snap["epoch"]
    ).json()
    assert [(e["seq"], e["data"]["name"]) for e in after["events"]] == [(base + 1, "Pending")]


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_concurrent_writers_during_snapshot_handoff_lose_nothing(env):
    """Shumë shkrues paralelë + snapshot + feed: çdo ndryshim përfundon ose në snapshot (seq <= snapshot_seq)
    ose në feed (seq > snapshot_seq), kurrë asnjërës; revision-et rriten pa kundërthënie."""
    if not env.url.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    ids = env.ids
    stop = threading.Event()
    done = []

    def writer(n):
        i = 0
        while not stop.is_set() and i < 12:
            with Session(env.eng, expire_on_commit=False) as s:
                ent.rename(s, ids["e1"] if n % 2 == 0 else ids["e2"], f"w{n}-{i}")
                s.commit()
            done.append(1)
            i += 1

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    [t.start() for t in threads]
    time.sleep(0.2)
    snap = env.get("/internal/sync/snapshot", headers=auth(tok(env))).json()
    [t.join(60) for t in threads]
    after = feed(
        env,
        after=snap["snapshot_seq"],
        generation=snap["authorization_generation"],
        epoch=snap["epoch"],
        limit=500,
    ).json()
    revisions = {x["enterprise_id"]: x["revision"] for x in snap["enterprises"]}
    names = {x["enterprise_id"]: x["data"]["name"] for x in snap["enterprises"]}
    seqs = [e["seq"] for e in after["events"]]
    assert (
        seqs == sorted(seqs)
        and all(s > snap["snapshot_seq"] for s in seqs)
        and not after["has_more"]
    )
    for e in after["events"]:  # zbatim sipas revision: çdo hap +1 (pa boshllëk, pa kundërthënie)
        assert e["revision"] == revisions[e["enterprise_id"]] + 1
        revisions[e["enterprise_id"]] = e["revision"]
        names[e["enterprise_id"]] = e["data"]["name"]
    with Session(env.eng) as s:
        final = {str(e.id): (e.revision, e.name) for e in s.query(ent.Enterprise)}
    for eid in (str(ids["e1"]), str(ids["e2"])):
        assert (revisions[eid], names[eid]) == final[eid]  # snapshot + feed = gjendja finale
    assert len(done) == 48
