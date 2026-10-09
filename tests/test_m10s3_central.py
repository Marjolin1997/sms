# ruff: noqa: F811
"""M10-S3 — kontrata `sender.request.v1`, endpoint-i Central `POST /internal/sender/requests` (skop `sender:report`), dedupe sipas `operation_id`, ridërgim vs riprovim, migrimi 0028."""

import json
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from apps.central.main import create_app
from apps.central.models import CentralUser
from apps.central.models.sender import (
    SenderDecision,
    SenderImmutableError,
    SenderRegistry,
    SenderRequestOperation,
    SenderSyncOutbox,
)
from apps.central.services import enterprises as ent
from apps.central.services import sender_requests, service_auth, users
from apps.central.services import senders as svc
from packages.contracts.control_plane.sender import request_v1 as rv
from tests.golden.control_plane_sender_request import regenerate as gen
from tests.test_central import IS_PG, central_alembic, make_db  # noqa: F401
from tests.test_central_auth import PW, auth_secret  # noqa: F401
from tests.test_central_sync_api import assertion, auth, keypair

GOLDEN = Path(__file__).parent / "golden" / "control_plane_sender_request"
U = lambda n: str(uuid.UUID(int=n))  # noqa: E731


@pytest.fixture
def renv(make_db):
    url = make_db()
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    private, public = keypair()
    with Session(eng, expire_on_commit=False) as s:
        e1, e2, e3 = ent.create(s, "Acme"), ent.create(s, "Beta"), ent.create(s, "Gamma")
        admin = users.create_user(s, "a1@example.com", PW, "admin")
        for name, scopes, ents, kid in (
            ("rep", ["sender:report"], [e1.id, e2.id], "k1"),
            ("rep1", ["sender:report"], [e1.id], "k2"),
            ("rdr", ["sender:read"], [e1.id, e2.id], "k3"),
            ("syn", ["sync:read"], [e1.id], "k4"),
            ("bil", ["billing:report"], [e1.id], "k5"),
        ):
            service_auth.create_client(s, name, scopes, ents)
            service_auth.add_key(s, name, kid, public)
        s.commit()
        ids = dict(e1=e1.id, e2=e2.id, e3=e3.id, admin=admin.id)
    c = TestClient(create_app(eng))
    c.eng, c.private, c.public, c.ids = eng, private, public, ids
    yield c
    eng.dispose()


def tok(env, client="rep", kid="k1", scope="sender:report", **kw):
    return assertion(env.private, client=client, kid=kid, scope=scope, **kw)


def body(env=None, **over):
    d = dict(
        operation_id=str(uuid.uuid4()), operation="request",
        enterprise_id=str(env.ids["e1"]) if env else U(1), external_ref="sms-sender-1",
        country="AL", sender_kind="alphanumeric", display_value="Acme", evidence_ref=None,
    )  # fmt: skip
    d.update(over)
    return rv.SenderRequestV1.build(**d).to_dict()


def post(env, payload, client="rep", kid="k1", token=None, raw=None):
    h = auth(token or tok(env, client, kid))
    if raw is not None:
        return env.post(
            "/internal/sender/requests",
            content=raw,
            headers={**h, "content-type": "application/json"},
        )
    return env.post("/internal/sender/requests", json=payload, headers=h)


def A(s, env):
    return s.get(CentralUser, env.ids["admin"])


def mutate(env, fn):
    with Session(env.eng, expire_on_commit=False) as s:
        out = fn(s, A(s, env))
        s.commit()
        return out


def counts(env):
    with Session(env.eng) as s:
        return (
            s.scalar(select(func.count()).select_from(SenderRegistry)),
            s.scalar(select(func.count()).select_from(SenderDecision)),
            s.scalar(select(func.count()).select_from(SenderRequestOperation)),
            s.scalar(select(func.count()).select_from(SenderSyncOutbox)),
        )


def registry(env, ref="sms-sender-1"):
    with Session(env.eng) as s:
        return s.scalar(select(SenderRegistry).where(SenderRegistry.external_ref == ref))


# =============================================================================================================
# kontrata
# =============================================================================================================


@pytest.mark.parametrize(
    "case", json.loads((GOLDEN / "cases.json").read_text()), ids=lambda c: c["name"]
)
def test_golden_requests_round_trip_byte_for_byte(case):
    raw = (GOLDEN / f"{case['name']}.body").read_bytes()
    r = rv.SenderRequestV1.from_bytes(raw)
    assert r.to_bytes() == raw and r.request_hash() == case["hash"]


def test_golden_fixtures_are_reproduced_by_the_generator_without_writing():
    for name, r in gen.cases():
        assert (GOLDEN / f"{name}.body").read_bytes() == r.to_bytes()


def test_contract_is_strict_and_owner_ref_is_not_part_of_it():
    ok = body()
    assert rv.SenderRequestV1.parse(ok).operation == "request"
    bad = [
        {**ok, "owner_ref": "c1"},  # identiteti kanonik s'është owner_ref
        {**ok, "norm_value": "acme"},  # Central rinormalizon vetë
        {**ok, "schema": "sender.request.v2"},
        {k: v for k, v in ok.items() if k != "operation_id"},
        {**ok, "operation": "approve"},
        {**ok, "country": "al"},
        {**ok, "country": "ALB"},
        {**ok, "sender_kind": "shortcode"},
        {**ok, "operation_id": "not-a-uuid"},
        {**ok, "operation_id": str(uuid.uuid4()).upper()},
        {**ok, "enterprise_id": 5},
        {**ok, "external_ref": "has space"},
        {**ok, "external_ref": "x" * 65},
        {**ok, "display_value": "ab"},
        {**ok, "display_value": "x" * 17},
        {**ok, "evidence_ref": "e" * 129},
        {**ok, "evidence_ref": ""},
        {**ok, "evidence_ref": 5},
        [],
    ]
    for b in bad:
        with pytest.raises(rv.ContractError):
            rv.SenderRequestV1.parse(b)
    with pytest.raises(rv.UnsupportedSchemaError):
        rv.SenderRequestV1.parse({**ok, "schema": "x"})
    with pytest.raises(rv.ContractError):
        rv.SenderRequestV1.from_bytes(b"{not json")


def test_external_ref_is_a_pure_function_of_the_local_id():
    assert rv.external_ref_for(7) == "sms-sender-7" == rv.external_ref_for(7)
    for bad in (0, -1, True, "7", 1.5):
        with pytest.raises(rv.ContractError):
            rv.external_ref_for(bad)


def test_request_hash_covers_operation_identity_and_content():
    a = rv.SenderRequestV1.parse(body())
    d = a.to_dict()
    assert rv.SenderRequestV1.parse(d).request_hash() == a.request_hash()
    for k, v in (
        ("display_value", "Acmf"),
        ("operation", "resubmit"),
        ("evidence_ref", "x1"),
        ("operation_id", U(9)),
    ):
        assert rv.SenderRequestV1.parse({**d, k: v}).request_hash() != a.request_hash()


def test_contract_package_is_a_stdlib_leaf():
    import ast

    tree = ast.parse((Path(rv.__file__)).read_text())
    mods = {
        n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    }
    mods |= {
        a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert mods <= {"hashlib", "json", "re", "uuid", "dataclasses", "typing"}


# =============================================================================================================
# endpoint-i: pranimi, dedupe, ridërgim vs riprovim
# =============================================================================================================


def test_new_request_is_accepted_creates_registry_decision_and_operation_and_a_sync_event(renv):
    b = body(renv)
    r = post(renv, b)
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["status"] == "accepted" and j["operation_id"] == b["operation_id"]
    assert (j["outcome"], j["auto"], j["current_status"]) == (
        "created",
        "not_applicable",
        "pending",
    )
    row = registry(renv)
    assert (
        str(row.id) == j["registry_ref"]
        and row.source == "enterprise"
        and row.external_ref == "sms-sender-1"
    )
    assert (row.country, row.sender_kind, row.display_value, row.norm_value) == (
        "AL",
        "alphanumeric",
        "Acme",
        "acme",
    )
    assert j["decision_ref"] == str(row.current_decision_id)
    regs, decs, ops, evs = counts(renv)
    assert (regs, decs, ops) == (1, 1, 1) and evs >= 1
    with Session(renv.eng) as s:
        d = s.scalar(select(SenderDecision))
        assert (d.decision, d.actor_label, d.source) == (
            "requested",
            "system:enterprise-request",
            "enterprise",
        )


def test_duplicate_delivery_is_idempotent_and_produces_no_new_effect(renv):
    b = body(renv)
    first = post(renv, b)
    snap = counts(renv)
    again = post(renv, b)
    assert again.status_code == 200 and again.json()["status"] == "duplicate"
    for k in ("operation_id", "outcome", "registry_ref", "current_status", "decision_ref"):
        assert again.json()[k] == first.json()[k]
    assert counts(renv) == snap  # zero efekt: as vendim, as ngjarje cp.sender.v1


def test_same_operation_with_changed_payload_is_a_conflict(renv):
    b = body(renv)
    assert post(renv, b).status_code == 201
    for over in (
        {"display_value": "Other"},
        {"country": "XK"},
        {"evidence_ref": "t1"},
        {"operation": "resubmit"},
    ):
        r = post(renv, {**b, **over})
        assert r.status_code == 409, over
    assert post(renv, {**b, "enterprise_id": str(renv.ids["e2"])}).status_code == 409


def test_same_external_ref_with_different_identity_is_a_conflict_and_nothing_is_created(renv):
    assert post(renv, body(renv)).status_code == 201
    snap = counts(renv)
    r = post(renv, body(renv, display_value="Different"))  # operation_id i ri, identitet tjetër
    assert r.status_code == 409
    assert counts(renv) == snap
    clash = post(
        renv, body(renv, external_ref="sms-sender-2")
    )  # i njëjti sender nën external_ref tjetër
    assert clash.status_code == 409


def test_a_new_operation_for_the_same_identity_is_recorded_as_existing_without_a_new_decision(renv):
    assert post(renv, body(renv)).status_code == 201
    regs, decs, ops, _ = counts(renv)
    r = post(renv, body(renv))  # operation_id i ri, e njëjta kërkesë (p.sh. outbox i rindërtuar)
    assert r.status_code == 201 and r.json()["outcome"] == "existing"
    assert counts(renv)[:3] == (regs, decs, ops + 1)


def test_resubmit_after_rejection_is_a_new_transition_and_its_retry_is_not(renv):
    assert post(renv, body(renv)).status_code == 201
    rid = registry(renv).id
    mutate(renv, lambda s, a: svc.reject(s, a, rid, "no proof"))
    rb = body(renv, operation="resubmit")
    r = post(renv, rb)
    assert (
        r.status_code == 201
        and r.json()["outcome"] == "resubmitted"
        and r.json()["current_status"] == "pending"
    )
    snap = counts(renv)
    retry = post(renv, rb)  # riprovim transporti i të njëjtit ridërgim
    assert (
        retry.status_code == 200 and retry.json()["status"] == "duplicate" and counts(renv) == snap
    )
    mutate(renv, lambda s, a: svc.reject(s, a, rid, "still no"))
    again = post(
        renv, body(renv, operation="resubmit")
    )  # ridërgim i ri i përdoruesit ⇒ tranzicion i ri
    assert again.status_code == 201 and again.json()["outcome"] == "resubmitted"
    with Session(renv.eng) as s:
        kinds = [d.decision for d in s.scalars(select(SenderDecision).order_by(SenderDecision.seq))]
    assert kinds == ["requested", "rejected", "resubmitted", "rejected", "resubmitted"]


def test_resubmit_on_a_sender_that_is_not_rejected_or_revoked_is_an_accepted_noop(renv):
    assert post(renv, body(renv)).status_code == 201
    rid = registry(renv).id
    snap = counts(renv)
    p = post(renv, body(renv, operation="resubmit"))
    assert p.status_code == 201 and p.json()["outcome"] == "noop_pending"
    assert counts(renv)[:2] == snap[:2] and counts(renv)[3] == snap[3]  # pa vendim, pa ngjarje
    mutate(renv, lambda s, a: svc.approve(s, a, rid))
    ap = post(renv, body(renv, operation="resubmit"))
    assert (
        ap.status_code == 201
        and ap.json()["outcome"] == "noop_approved"
        and ap.json()["current_status"] == "approved"
    )


def test_resubmit_of_an_unknown_sender_or_changed_identity_is_a_conflict(renv):
    r = post(renv, body(renv, operation="resubmit"))
    assert r.status_code == 409 and counts(renv)[:3] == (0, 0, 0)
    assert post(renv, body(renv)).status_code == 201
    rid = registry(renv).id
    mutate(renv, lambda s, a: svc.reject(s, a, rid, "x"))
    bad = post(renv, body(renv, operation="resubmit", display_value="Changed"))
    assert bad.status_code == 409


def test_request_after_resubmit_ordering_is_enforced_by_the_registry(renv):
    # ridërgimi para kërkesës fillestare (renditje e prishur) nuk krijon asgjë
    assert post(renv, body(renv, operation="resubmit")).status_code == 409
    assert post(renv, body(renv)).status_code == 201
    assert post(renv, body(renv, operation="resubmit")).json()["outcome"] == "noop_pending"


def test_policy_auto_approval_and_denial_are_business_results_reported_as_accepted(renv):
    mutate(renv, lambda s, a: svc.set_policy(s, a, "XK", "numeric", True, False, "open"))
    ok = post(
        renv,
        body(
            renv,
            external_ref="sms-sender-3",
            country="XK",
            sender_kind="numeric",
            display_value="383441234567",
        ),
    )
    assert ok.status_code == 201
    assert (ok.json()["auto"], ok.json()["current_status"]) == ("approved", "approved")
    mutate(renv, lambda s, a: svc.set_policy(s, a, "RU", "alphanumeric", False, True, "ban"))
    no = post(renv, body(renv, external_ref="sms-sender-4", country="RU", display_value="Blocked"))
    assert no.status_code == 201  # transporti ka sukses; rezultati i biznesit është refuzim
    assert (no.json()["auto"], no.json()["current_status"]) == ("denied", "rejected")


def test_kind_parity_is_verified_against_central_normalization(renv):
    r = post(renv, body(renv, sender_kind="numeric", display_value="Acme"))
    assert r.status_code == 422
    r = post(renv, body(renv, sender_kind="alphanumeric", display_value="355691234567"))
    assert r.status_code == 422 and counts(renv)[:3] == (0, 0, 0)


# =============================================================================================================
# siguria: skop, enterprise, kredenciale, trup
# =============================================================================================================


def test_unauthorized_enterprise_is_rejected_even_with_a_valid_signature_and_scope(renv):
    r = post(renv, body(renv, enterprise_id=str(renv.ids["e3"])))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "enterprise_not_authorized"
    r1 = post(
        renv, body(renv, enterprise_id=str(renv.ids["e2"])), "rep1", "k2"
    )  # klient tjetër: vetëm e1
    assert r1.status_code == 403 and counts(renv)[:3] == (0, 0, 0)


def test_scopes_are_least_privilege_in_both_directions(renv):
    b = body(renv)
    for client, kid, scope in (
        ("rdr", "k3", "sender:read"),
        ("syn", "k4", "sync:read"),
        ("bil", "k5", "billing:report"),
    ):
        assert post(renv, b, token=tok(renv, client, kid, scope)).status_code == 403, client
    assert (
        post(renv, b, token=tok(renv, "rep", "k1", "sender:read")).status_code == 403
    )  # claim i gabuar
    assert counts(renv)[:3] == (0, 0, 0)
    # sender:report s'lexon feed-in
    for path in ("/internal/sender/state", "/internal/sender/snapshot"):
        assert renv.get(path, headers=auth(tok(renv))).status_code == 403
    assert (
        renv.get(
            "/internal/sender/state", headers=auth(tok(renv, "rdr", "k3", "sender:read"))
        ).status_code
        == 200
    )
    assert renv.get("/internal/sync/state", headers=auth(tok(renv))).status_code in (403, 404)


def test_missing_garbage_disabled_revoked_and_replayed_credentials(renv):
    b = body(renv)
    assert renv.post("/internal/sender/requests", json=b).status_code == 401
    assert post(renv, b, token="garbage").status_code == 401
    t = tok(renv)
    assert post(renv, body(renv), token=t).status_code == 201
    assert (
        post(
            renv, body(renv, external_ref="sms-sender-9", display_value="Other1"), token=t
        ).status_code
        == 401
    )  # jti i ripërdorur
    tampered = tok(renv)[:-4] + ("AAAA" if not tok(renv).endswith("AAAA") else "BBBB")
    assert post(renv, body(renv), token=tampered).status_code == 401
    with Session(renv.eng) as s:
        service_auth.disable_key(s, "rep1", "k2")
        s.commit()
    assert post(renv, body(renv), "rep1", "k2").status_code == 401
    with Session(renv.eng) as s:
        service_auth.disable_client(s, "rep")
        s.commit()
    assert post(renv, body(renv, external_ref="sms-sender-9")).status_code == 401
    assert counts(renv)[2] == 1


def test_strict_payload_and_oversized_fields_are_rejected_without_effect(renv):
    base = body(renv)
    for bad in (
        {**base, "owner_ref": "c1"},
        {**base, "extra": 1},
        {k: v for k, v in base.items() if k != "country"},
        {**base, "evidence_ref": "e" * 129},
        {**base, "display_value": "x" * 40},
        {**base, "operation_id": "nope"},
        {**base, "schema": "cp.sender.v1"},
    ):
        assert post(renv, bad).status_code == 422, bad
    big = json.dumps({**base, "evidence_ref": "e" * 4000}).encode()
    assert post(renv, None, raw=big).status_code == 413
    assert (
        renv.post(
            "/internal/sender/requests",
            content=b"[]",
            headers={**auth(tok(renv)), "content-type": "application/json"},
        ).status_code
        == 422
    )
    assert counts(renv)[:3] == (0, 0, 0)


def test_transport_never_calls_admin_review_and_registry_source_is_enterprise(renv):
    r = renv.post("/internal/sender/requests/approve", json={}, headers=auth(tok(renv)))
    assert r.status_code in (404, 405)
    assert post(renv, body(renv)).status_code == 201
    assert registry(renv).current_status == "pending"  # nuk ka miratim nga transporti


# =============================================================================================================
# konkurrencë (PostgreSQL)
# =============================================================================================================


def _race(renv, payloads):
    outs, errs = [], []
    gate = threading.Barrier(len(payloads))

    def run(p):
        try:
            with Session(renv.eng, expire_on_commit=False) as s:
                gate.wait(timeout=20)
                res = sender_requests.process(s, sender_requests.parse(p))
                s.commit()
                outs.append((res.op.outcome, res.duplicate))
        except Exception as e:  # noqa: BLE001
            errs.append(type(e).__name__)

    ts = [threading.Thread(target=run, args=(p,)) for p in payloads]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    return outs, errs


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_same_operation_delivered_twice_concurrently_has_exactly_one_effect(renv):
    b = body(renv)
    outs, errs = _race(renv, [b, b, b])
    assert not errs and sorted(d for _o, d in outs) == [False, True, True]
    assert counts(renv)[:3] == (1, 1, 1)


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_two_distinct_resubmits_race_to_one_transition(renv):
    assert post(renv, body(renv)).status_code == 201
    rid = registry(renv).id
    mutate(renv, lambda s, a: svc.reject(s, a, rid, "x"))
    outs, errs = _race(renv, [body(renv, operation="resubmit"), body(renv, operation="resubmit")])
    assert not errs and sorted(o for o, _d in outs) == ["noop_pending", "resubmitted"]
    with Session(renv.eng) as s:
        assert [d.decision for d in s.scalars(select(SenderDecision).order_by(SenderDecision.seq))] == [
            "requested", "rejected", "resubmitted",
        ]  # fmt: skip


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_initial_request_and_resubmit_arriving_together_never_create_two_registries(renv):
    outs, errs = _race(renv, [body(renv), body(renv, operation="resubmit")])
    with Session(renv.eng) as s:
        assert s.scalar(select(func.count()).select_from(SenderRegistry)) == 1
    assert len(outs) + len(errs) == 2 and set(errs) <= {"Conflict"}


# =============================================================================================================
# migrimi 0028
# =============================================================================================================


def _drift(ctx):
    from alembic.autogenerate import compare_metadata

    import apps.central.models  # noqa: F401
    from apps.central.core.db import Base

    return [
        d for d in compare_metadata(ctx, Base.metadata) if "central_alembic_version" not in repr(d)
    ]


def test_central_0028_is_additive_reversible_and_matches_metadata(make_db):
    from alembic.migration import MigrationContext

    url = make_db()
    central_alembic(url, "upgrade", "0027")
    eng = create_engine(url)
    before = {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    }
    assert "sender_request_operations" not in before
    central_alembic(url, "upgrade", "0028")
    assert "sender_request_operations" in inspect(eng).get_table_names()
    for t, cols in before.items():
        if t == "central_alembic_version":
            continue
        assert {c["name"] for c in inspect(eng).get_columns(t)} == cols
    with eng.connect() as c:
        ctx = MigrationContext.configure(c, opts={"compare_type": True})
        assert _drift(ctx) == []
    central_alembic(url, "downgrade", "0027")
    assert "sender_request_operations" not in inspect(eng).get_table_names()
    central_alembic(url, "upgrade", "head")
    with eng.connect() as c:
        ctx = MigrationContext.configure(c, opts={"compare_type": True})
        assert _drift(ctx) == []
    eng.dispose()


def test_operations_are_append_only_in_the_orm(renv):
    assert post(renv, body(renv)).status_code == 201
    with Session(renv.eng) as s:
        o = s.scalar(select(SenderRequestOperation))
        o.outcome = "existing"
        with pytest.raises(SenderImmutableError):
            s.flush()
        s.rollback()
        s.delete(s.scalar(select(SenderRequestOperation)))
        with pytest.raises(SenderImmutableError):
            s.flush()


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_triggers_make_operations_append_only(renv):
    assert post(renv, body(renv)).status_code == 201
    for stmt in (
        "UPDATE sender_request_operations SET outcome = 'existing'",
        "DELETE FROM sender_request_operations",
        "TRUNCATE sender_request_operations",
    ):
        with renv.eng.connect() as c:
            with pytest.raises(DBAPIError):
                c.execute(text(stmt))
                c.commit()
    assert counts(renv)[2] == 1


def test_central_service_does_not_import_enterprise_code():
    import ast

    src = Path(sender_requests.__file__).read_text()
    tree = ast.parse(src)
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m == "app" or m.startswith("app.") for m in mods) and "SMS_" not in src
