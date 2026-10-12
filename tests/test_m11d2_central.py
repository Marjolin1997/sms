# ruff: noqa: F811
"""M11-D2 — Central: kontrata `sender.ops.v1`, ingest `POST /internal/sender/operations` (skop `sender:report`), idempotencë/rend, ruajtja e gjendjes së fundit, migrimi 0030
dhe bashkimi i provës Enterprise në `GET /admin/operations/sender` (freski, unknown vs zero, mospërputhje, alarme, privatësi)."""

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.orm import Session

from apps.central.core.config import settings as central_settings
from apps.central.models import CentralUser
from apps.central.models.sender import (
    SenderBootstrapRun,
    SenderImmutableError,
    SenderOpsSnapshot,
    SenderRegistry,
    SenderRequestOperation,
)
from apps.central.services import sender_operations as opsvc
from apps.central.services import sender_ops_evidence as ev
from apps.central.services import service_auth
from packages.contracts.control_plane.sender import ops_v1 as v1
from tests.test_central import IS_PG, central_alembic, make_db  # noqa: F401
from tests.test_central_auth import PW, auth_secret, bearer, mk, token_for  # noqa: F401
from tests.test_central_sync_api import auth
from tests.test_m10s3_central import renv, tok  # noqa: F401

OPS = "/internal/sender/operations"
ADMIN_URL = "/admin/operations/sender"
NOW = datetime.now(UTC).replace(microsecond=0)


def ts(d=NOW):
    return v1.format_ts(d)


# --- ndërtues dokumentesh ------------------------------------------------------------------------------------------------


def alert_items(**levels):
    names = ev.ALERT_CHECKS
    return [{"name": n, "level": levels.get(n, "ok"), "value": 0, "threshold": 1} for n in names]


def data(**over):
    d = {
        "sync": dict(
            enabled=True,
            initialized=True,
            cursor_seq=40,
            latest_central_seq=40,
            lag=0,
            last_success_age_seconds=12,
            snapshot_age_seconds=300,
            failure_count=0,
            gap_recoveries=0,
            drift_repairs=0,
            policy_count=2,
            authorization_count=5,
            withdrawn_count=0,
            projection_stale=False,
            last_error_age_seconds=None,
        ),
        "requests": dict(
            enabled=True,
            pending=0,
            retry=0,
            sending=0,
            failed=0,
            sent=7,
            stuck_sending=0,
            oldest_open_age_seconds=None,
            last_sent_age_seconds=20,
            attempts_max=1,
            failed_by_category={},
        ),
        "shadow": dict(
            mode="shadow",
            window_hours=None,
            min_samples=100,
            comparisons_total=250,
            match_total=250,
            match_rate=1.0,
            critical_total=0,
            local_allow_central_deny=0,
            local_deny_central_allow=0,
            central_missing=0,
            policy_mismatch=0,
            identity_mismatch=0,
            projection_stale=0,
            by_category={"match_allowed": 250},
        ),
        "bootstrap": {
            "state": dict(
                completed=True,
                version=1,
                completed_at=ts(),
                tenant_count=2,
                sender_count=4,
                unresolved_count=0,
                report_hash="abcdef012345",
            ),
            "issues": {"open": 0, "resolved": 0, "open_by_category": {}},
        },
        "cutover": dict(
            authority_mode="shadow",
            dispatch_recheck=True,
            ack_configured=False,
            ack_valid=None,
            evidence_count=1,
            latest_evidence=dict(
                kind="pre_cutover",
                hash="0123456789ab",
                readiness_status="PASS",
                created_at=ts(),
                authority_version=1,
            ),
        ),
        "alerts": {"items": alert_items(), "evaluated_age_seconds": 3},
    }
    for k, v in over.items():
        d[k] = (
            v
            if (v is None or not isinstance(d[k], dict) or "items" in d[k] or "state" in d[k])
            else {**d[k], **v}
        )
    return d


def doc(generated=NOW, **over):
    return {
        "schema": v1.SCHEMA,
        "snapshot_id": str(uuid.uuid4()),
        "scope": "deployment",
        "generated_at": ts(generated),
        "source_revision": "8c820ff",
        "data": data(**over),
    }


def parse(d):
    return v1.SenderOpsV1.parse(d)


# =============================================================================================================
# kontrata
# =============================================================================================================


def test_valid_snapshot_roundtrips_with_a_stable_hash():
    d = doc()
    s = parse(d)
    assert (
        s.to_dict() == d
        and v1.SenderOpsV1.from_bytes(s.to_bytes()).content_hash() == s.content_hash()
    )
    assert parse({**d, "snapshot_id": str(uuid.uuid4())}).content_hash() != s.content_hash()
    assert s.generated_at == NOW and s.source_revision == "8c820ff"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: {**d, "extra": 1},
        lambda d: {k: v for k, v in d.items() if k != "scope"},
        lambda d: {**d, "schema": "sender.ops.v2"},
        lambda d: {**d, "scope": "tenant"},
        lambda d: {**d, "snapshot_id": "NOT-A-UUID"},
        lambda d: {**d, "snapshot_id": str(uuid.uuid4()).upper()},
        lambda d: {**d, "generated_at": "2031-01-01 00:00:00"},
        lambda d: {**d, "generated_at": "2031-13-01T00:00:00Z"},
        lambda d: {**d, "source_revision": "has space"},
        lambda d: {**d, "source_revision": "x" * 65},
        lambda d: {**d, "data": {**d["data"], "extra": None}},
        lambda d: {**d, "data": {k: v for k, v in d["data"].items() if k != "alerts"}},
        lambda d: {**d, "data": "nope"},
    ],
)
def test_envelope_is_strict(mutate):
    with pytest.raises(v1.ContractError):
        parse(mutate(doc()))


def test_unsupported_schema_is_a_distinct_error():
    with pytest.raises(v1.UnsupportedSchemaError):
        parse({**doc(), "schema": "sender.ops.v9"})


def bad(section, key, value):
    d = doc()
    d["data"][section] = {**d["data"][section], key: value}
    return d


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("sync", "lag", -1),
        ("sync", "lag", True),
        ("sync", "lag", 1.5),
        ("sync", "lag", 2**60),
        ("sync", "enabled", 1),
        ("sync", "extra", 1),
        ("requests", "failed", "3"),
        ("requests", "failed_by_category", {"Bad Key": 1}),
        ("requests", "failed_by_category", {"+355691234567": 1}),
        ("requests", "failed_by_category", {f"c{i}": 1 for i in range(30)}),
        ("requests", "failed_by_category", {"ok": -1}),
        ("shadow", "mode", "enterprise"),
        ("shadow", "match_rate", float("nan")),
        ("shadow", "match_rate", float("inf")),
        ("shadow", "match_rate", "1"),
        ("cutover", "authority_mode", "both"),
        ("cutover", "ack_valid", "yes"),
    ],
)
def test_section_fields_are_typed_and_bounded(section, key, value):
    with pytest.raises(v1.ContractError):
        parse(bad(section, key, value))


def test_free_text_cannot_smuggle_values_in():
    for path in (("alerts", "name", "+355691234567"), ("bootstrap", "report_hash", "ACME-brand-x")):
        d = doc()
        if path[0] == "alerts":
            d["data"]["alerts"]["items"][0]["name"] = path[2]
        else:
            d["data"]["bootstrap"]["state"]["report_hash"] = path[2]
        with pytest.raises(v1.ContractError):
            parse(d)
    d = doc()
    d["data"]["alerts"]["items"] = alert_items() * 4  # > MAX_ALERTS
    with pytest.raises(v1.ContractError):
        parse(d)


def test_null_section_and_zero_are_different_documents():
    zero = parse(doc(requests=dict(failed=0))).to_dict()
    unavailable = parse(doc(requests=None)).to_dict()
    assert zero["data"]["requests"]["failed"] == 0 and unavailable["data"]["requests"] is None
    assert (
        parse(
            doc(sync=None, shadow=None, bootstrap=None, cutover=None, alerts=None, requests=None)
        ).data["sync"]
        is None
    )


# =============================================================================================================
# ingest
# =============================================================================================================


def put(env, d, client="rep", kid="k1", token=None, raw=None):
    h = auth(token or tok(env, client, kid))
    if raw is not None:
        return env.post(OPS, content=raw, headers={**h, "content-type": "application/json"})
    return env.post(OPS, json=d, headers=h)


def stored(env):
    with Session(env.eng) as s:
        return list(s.scalars(select(SenderOpsSnapshot)))


def add_client(env, name, scopes, enterprises, kid="kx"):
    with Session(env.eng, expire_on_commit=False) as s:
        service_auth.create_client(s, name, scopes, enterprises)
        service_auth.add_key(s, name, kid, env.public)
        s.commit()


def test_authenticated_report_is_accepted_and_stored_as_latest_state(renv):
    d = doc()
    r = put(renv, d)
    assert r.status_code == 201 and r.json() == {
        "status": "accepted",
        "snapshot_id": d["snapshot_id"],
    }
    (row,) = stored(renv)
    assert (
        str(row.snapshot_id) == d["snapshot_id"]
        and row.schema_version == "sender.ops.v1"
        and row.content_hash == parse(d).content_hash()
    )
    assert row.payload == d and row.source_revision == "8c820ff" and row.received_at is not None


def test_authentication_and_scope_matrix(renv):
    assert renv.post(OPS, json=doc()).status_code == 401
    assert put(renv, None, token="garbage").status_code == 401
    assert (
        put(
            renv, doc(), client="rdr", kid="k3", token=tok(renv, "rdr", "k3", scope="sender:read")
        ).status_code
        == 403
    )  # skop i gabuar (lexim)
    assert (
        put(
            renv, doc(), client="syn", kid="k4", token=tok(renv, "syn", "k4", scope="sync:read")
        ).status_code
        == 403
    )
    assert (
        put(
            renv,
            doc(),
            client="bil",
            kid="k5",
            token=tok(renv, "bil", "k5", scope="billing:report"),
        ).status_code
        == 403
    )
    assert (
        put(renv, doc(), token=tok(renv, "rep", "k1", scope="sender:read")).status_code == 403
    )  # kërkon skop që klienti s'e ka të firmosur
    assert stored(renv) == []


def test_disabled_client_revoked_key_and_replayed_jti_are_rejected(renv):
    t = tok(renv)
    assert put(renv, doc(), token=t).status_code == 201
    assert (
        put(renv, doc(NOW + timedelta(seconds=5)), token=t).status_code == 401
    )  # jti i ripërdorur
    with Session(renv.eng) as s:
        service_auth.disable_key(s, "rep1", "k2")
        s.commit()
    assert put(renv, doc(), "rep1", "k2").status_code == 401
    with Session(renv.eng) as s:
        service_auth.disable_client(s, "rep")
        s.commit()
    assert put(renv, doc(NOW + timedelta(seconds=9))).status_code == 401


def test_client_without_authorized_enterprises_is_denied(renv):
    add_client(renv, "lonely", ["sender:report"], [])
    r = put(renv, doc(), "lonely", "kx", token=tok(renv, "lonely", "kx"))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "enterprise_not_authorized"
    assert stored(renv) == []


def test_identity_comes_from_the_authenticated_client_never_from_the_body(renv):
    d = doc()
    assert (
        put(renv, {**d, "client_pk": str(uuid.uuid4())}).status_code == 422
    )  # trup me identitet të injektuar: i refuzuar
    assert put(renv, {**d, "enterprise_id": str(renv.ids["e3"])}).status_code == 422
    assert put(renv, d).status_code == 201
    add_client(renv, "other", ["sender:report"], [renv.ids["e3"]])
    assert (
        put(
            renv, doc(NOW + timedelta(seconds=2)), "other", "kx", token=tok(renv, "other", "kx")
        ).status_code
        == 201
    )
    rows = stored(renv)
    assert len(rows) == 2 and len({r.client_pk for r in rows}) == 2  # një rresht për klient


def test_same_snapshot_same_content_is_idempotent(renv):
    d = doc()
    assert put(renv, d).status_code == 201
    (before,) = stored(renv)
    r = put(renv, d)
    assert r.status_code == 200 and r.json()["status"] == "duplicate"
    (after,) = stored(renv)
    assert after.received_at == before.received_at and after.content_hash == before.content_hash


def test_same_snapshot_id_with_different_content_is_a_conflict_and_changes_nothing(renv):
    d = doc()
    assert put(renv, d).status_code == 201
    other = {**d, "data": {**d["data"], "requests": {**d["data"]["requests"], "failed": 9}}}
    r = put(renv, other)
    assert r.status_code == 409
    (row,) = stored(renv)
    assert row.payload["data"]["requests"]["failed"] == 0


def test_older_snapshot_never_replaces_a_newer_one_and_newer_does(renv):
    new = doc(NOW)
    assert put(renv, new).status_code == 201
    old = doc(NOW - timedelta(minutes=5), requests=dict(failed=5))
    r = put(renv, old)
    assert r.status_code == 200 and r.json()["status"] == "ignored_stale"
    same_time = put(renv, doc(NOW))
    assert same_time.json()["status"] == "ignored_stale"
    assert str(stored(renv)[0].snapshot_id) == new["snapshot_id"]
    newest = doc(NOW + timedelta(seconds=30), requests=dict(failed=2))
    assert put(renv, newest).status_code == 201
    (row,) = stored(renv)
    assert (
        str(row.snapshot_id) == newest["snapshot_id"]
        and row.payload["data"]["requests"]["failed"] == 2
    )


def test_body_size_limit_and_malformed_bodies(renv):
    assert put(renv, None, raw=b"x" * (v1.MAX_BODY_BYTES + 1)).status_code == 413
    assert put(renv, None, raw=b"{not json").status_code == 422
    assert put(renv, {**doc(), "schema": "sender.ops.v9"}).status_code == 422
    assert put(renv, {**doc(), "extra": 1}).status_code == 422
    assert put(renv, bad("sync", "lag", -5)).status_code == 422
    assert stored(renv) == []


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_concurrent_first_reports_for_the_same_client_leave_one_consistent_row(renv):
    docs = [doc(NOW + timedelta(seconds=i)) for i in range(4)]
    codes = []

    def go(d):
        codes.append(put(renv, d, token=tok(renv)).status_code)

    ts_ = [threading.Thread(target=go, args=(d,)) for d in docs]
    [t.start() for t in ts_]
    [t.join() for t in ts_]
    assert all(c in (200, 201) for c in codes), codes
    (row,) = stored(renv)
    assert str(row.snapshot_id) == docs[-1]["snapshot_id"] or str(row.snapshot_id) in {
        d["snapshot_id"] for d in docs
    }


def test_client_pk_is_immutable_in_the_orm(renv):
    assert put(renv, doc()).status_code == 201
    with Session(renv.eng) as s:
        row = s.scalar(select(SenderOpsSnapshot))
        row.client_pk = uuid.uuid4()
        with pytest.raises(SenderImmutableError):
            s.flush()


# =============================================================================================================
# migrimi 0030
# =============================================================================================================


def test_central_0030_is_additive_reversible_and_matches_metadata(make_db):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    import apps.central.models  # noqa: F401
    from apps.central.core.db import Base

    def drift(eng):
        with eng.connect() as c:
            ctx = MigrationContext.configure(c, opts={"compare_type": True})
            return [
                d
                for d in compare_metadata(ctx, Base.metadata)
                if "central_alembic_version" not in repr(d)
            ]

    url = make_db()
    central_alembic(url, "upgrade", "0029")
    eng = create_engine(url)
    before = {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    }
    assert "sender_ops_snapshots" not in before
    central_alembic(url, "upgrade", "0030")
    assert "sender_ops_snapshots" in inspect(eng).get_table_names()
    for t, cols in before.items():
        if t != "central_alembic_version":
            assert {c["name"] for c in inspect(eng).get_columns(t)} == cols
    assert {c["name"] for c in inspect(eng).get_columns("sender_ops_snapshots")} == {
        "client_pk",
        "snapshot_id",
        "schema_version",
        "generated_at",
        "received_at",
        "source_revision",
        "content_hash",
        "payload",
    }
    assert drift(eng) == []
    central_alembic(url, "downgrade", "0029")
    assert "sender_ops_snapshots" not in inspect(eng).get_table_names()
    central_alembic(url, "upgrade", "head")
    assert drift(eng) == []
    if eng.dialect.name == "postgresql":
        with eng.connect() as c:
            assert (
                c.execute(
                    text(
                        "select count(*) from pg_constraint where conname like 'ck_sender_ops_snapshots%'"
                    )
                ).scalar()
                == 1
            )
    eng.dispose()


# =============================================================================================================
# bashkimi në pamjen operacionale
# =============================================================================================================


@pytest.fixture
def admin(renv):
    mk(renv.eng, "ro@example.com", role="operator")
    return bearer(token_for(renv, "a1@example.com")), bearer(token_for(renv, "ro@example.com"))


def view(env, headers):
    opsvc.clear_cache()
    r = env.get(ADMIN_URL, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def sec(body):
    return {s["code"]: s for s in body["sections"]}


def chk(body):
    return {c["code"]: c for s in body["sections"] for c in s["checks"]}


def seed_central_evidence(env, senders=4, tenants=2, unresolved=0):
    with Session(env.eng, expire_on_commit=False) as s:
        reg = s.scalar(select(SenderRegistry.id).limit(1))
        if reg is None:
            from apps.central.services import senders as csvc

            admin_user = s.get(CentralUser, env.ids["admin"])
            reg = csvc.request_sender(
                s, admin_user, env.ids["e1"], "r1", "AL", "Acme", None, source="admin"
            ).sender.id
        s.add(
            SenderRequestOperation(
                operation_id=uuid.uuid4(),
                enterprise_id=env.ids["e1"],
                registry_id=reg,
                external_ref="r1",
                operation="request",
                request_hash="0" * 64,
                outcome="created",
                auto="not_applicable",
                status_after="pending",
                received_at=datetime.now(UTC),
            )
        )
        s.add(
            SenderBootstrapRun(
                artifact_hash="a" * 64,
                status="completed",
                started_at=NOW,
                completed_at=NOW,
                tenant_count=tenants,
                sender_count=senders,
                imported_count=senders,
                unresolved_count=unresolved,
            )
        )
        s.commit()


def age_snapshot(env, seconds):
    with Session(env.eng) as s:
        row = s.scalar(select(SenderOpsSnapshot))
        row.received_at = datetime.now(UTC) - timedelta(seconds=seconds)
        s.commit()


def test_without_a_snapshot_everything_enterprise_stays_unknown_and_never_500(renv, admin):
    body = view(renv, admin[0])
    assert (
        body["status"] == "unknown"
        and body["metrics"]["enterprise"] is None
        and body["metrics"]["enterprise_snapshot"] == {"present": False, "deployments": 0}
    )
    snap = chk(body)["enterprise_snapshot"]
    assert snap["status"] == "unknown" and "No Enterprise snapshot" in snap["summary"]
    for code in (
        "enterprise_sync_runtime",
        "enterprise_request_outbox",
        "shadow_evidence",
        "cutover_evidence",
        "enterprise_alerts",
        "bootstrap_issue_categories",
    ):
        assert chk(body)[code]["status"] == "unknown" and chk(body)[code]["source"] == "enterprise"


def test_fresh_healthy_snapshot_gives_real_values_and_an_overall_pass(renv, admin):
    seed_central_evidence(renv)
    assert put(renv, doc()).status_code == 201
    body = view(renv, admin[0])
    assert body["status"] == "pass", [(s["code"], s["status"]) for s in body["sections"]]
    m = body["metrics"]["enterprise"]
    assert (
        m["sync"]["cursor_seq"] == 40
        and m["requests"]["sent"] == 7
        and m["shadow"]["comparisons_total"] == 250
        and m["bootstrap"]["state"]["sender_count"] == 4
    )
    meta = body["metrics"]["enterprise_snapshot"]
    assert (
        meta["state"] == "fresh"
        and meta["age_seconds"] <= 5
        and meta["stale_after_seconds"] == central_settings.sender_ops_stale_seconds
        and meta["generated_at"]
        and meta["received_at"]
        and meta["reporter"] == "rep"
    )
    c = chk(body)
    assert (
        c["enterprise_sync_runtime"]["status"] == "pass"
        and c["enterprise_sync_runtime"]["details"]["cursor_seq"] == 40
        and c["enterprise_sync_runtime"]["details"]["lag"] == 0
    )
    assert (
        c["enterprise_request_outbox"]["status"] == "pass"
        and c["enterprise_request_outbox"]["details"]["failed"] == 0
    )  # 0 i matur ≠ unknown
    assert (
        c["shadow_evidence"]["status"] == "pass"
        and c["cutover_evidence"]["status"] == "pass"
        and c["bootstrap_evidence_agreement"]["status"] == "pass"
    )
    assert body["alerts"] == []


def test_zero_unavailable_and_not_configured_are_never_confused(renv, admin):
    seed_central_evidence(renv)
    lag_unknown = [{"name": "sender_sync_lag", "level": "ok", "value": None, "threshold": 1000}]
    r_unknown = put(
        renv,
        doc(
            requests=None,
            sync=dict(lag=None, latest_central_seq=None),
            alerts={"items": lag_unknown, "evaluated_age_seconds": 0},
        ),
    )
    assert r_unknown.status_code == 201
    c = chk(view(renv, admin[0]))
    assert (
        c["enterprise_request_outbox"]["status"] == "unknown"
        and "unavailable" in c["enterprise_request_outbox"]["summary"]
    )
    assert c["enterprise_sync_lag"]["status"] == "unknown"  # lag null = e padisponueshme, jo "pass"
    assert put(renv, doc(NOW + timedelta(seconds=5), sync=dict(enabled=False))).status_code == 201
    assert (
        chk(view(renv, admin[0]))["enterprise_sync_runtime"]["status"] == "unknown"
    )  # i paaktivizuar = "not configured"
    assert (
        put(
            renv,
            doc(
                NOW + timedelta(seconds=9),
                shadow=dict(mode="local", comparisons_total=0, match_total=0, match_rate=None),
            ),
        ).status_code
        == 201
    )
    assert chk(view(renv, admin[0]))["shadow_evidence"]["status"] == "unknown"
    assert (
        put(
            renv,
            doc(
                NOW + timedelta(seconds=12),
                bootstrap={
                    "state": None,
                    "issues": {"open": 0, "resolved": 0, "open_by_category": {}},
                },
            ),
        ).status_code
        == 201
    )
    assert chk(view(renv, admin[0]))["bootstrap_issue_categories"]["status"] == "unknown"


def test_request_backlog_and_permanent_failures(renv, admin):
    seed_central_evidence(renv)
    r = dict(
        pending=4,
        retry=2,
        failed=3,
        oldest_open_age_seconds=1200,
        failed_by_category={"http_422": 3, "Weird!": 1} if False else {"http_422": 3},
    )
    assert (
        put(
            renv,
            doc(
                requests=r,
                alerts={
                    "items": alert_items(
                        sender_request_oldest_age_seconds="warning",
                        sender_request_permanent_failures="critical",
                    ),
                    "evaluated_age_seconds": 0,
                },
            ),
        ).status_code
        == 201
    )
    body = view(renv, admin[0])
    c = chk(body)
    assert (
        c["enterprise_request_outbox"]["status"] == "fail"
        and c["enterprise_request_outbox"]["details"]["failed_http_422"] == 3
    )
    assert (
        c["enterprise_request_age"]["status"] == "warn"
        and c["enterprise_request_failures"]["status"] == "fail"
    )
    assert body["status"] == "fail" and sec(body)["requests"]["status"] == "fail"


def test_sync_lag_and_gap_recoveries_use_the_enterprise_alert_levels(renv, admin):
    seed_central_evidence(renv)
    s = dict(cursor_seq=10, latest_central_seq=2500, lag=2490, gap_recoveries=2)
    assert (
        put(
            renv,
            doc(
                sync=s,
                alerts={
                    "items": [
                        {
                            "name": "sender_sync_lag",
                            "level": "warning",
                            "value": 2490,
                            "threshold": 1000,
                        },
                        {
                            "name": "sender_sync_gap_recoveries_total",
                            "level": "warning",
                            "value": 2,
                            "threshold": 1,
                        },
                    ],
                    "evaluated_age_seconds": 0,
                },
            ),
        ).status_code
        == 201
    )
    body = view(renv, admin[0])
    c = chk(body)
    assert (
        c["enterprise_sync_lag"]["status"] == "warn"
        and c["enterprise_sync_lag"]["details"]["value"] == 2490
        and c["enterprise_sync_gap_recoveries"]["status"] == "warn"
    )
    assert c["enterprise_sync_runtime"]["details"]["lag"] == 2490 and body["status"] == "warn"
    assert {(a["source"], a["code"]) for a in body["alerts"]} >= {
        ("enterprise", "sender_sync_lag"),
        ("enterprise", "sender_sync_gap_recoveries_total"),
    }


def test_critical_drift_fails_and_shadow_sample_progress_warns(renv, admin):
    seed_central_evidence(renv)
    drift = dict(
        comparisons_total=300,
        match_total=290,
        match_rate=0.9667,
        critical_total=4,
        central_missing=3,
        local_allow_central_deny=4,
        policy_mismatch=1,
    )
    assert (
        put(
            renv,
            doc(
                shadow=drift,
                alerts={
                    "items": alert_items(sender_shadow_critical_drift="critical"),
                    "evaluated_age_seconds": 0,
                },
            ),
        ).status_code
        == 201
    )
    body = view(renv, admin[0])
    sh = chk(body)["shadow_evidence"]
    assert (
        sh["status"] == "fail"
        and sh["details"]["critical_total"] == 4
        and sh["details"]["central_missing"] == 3
        and sh["details"]["local_allow_central_deny"] == 4
    )
    assert body["status"] == "fail" and any(
        a["source"] == "enterprise"
        and a["code"] == "sender_shadow_critical_drift"
        and a["severity"] == "critical"
        for a in body["alerts"]
    )
    assert (
        put(
            renv,
            doc(
                NOW + timedelta(seconds=7),
                shadow=dict(comparisons_total=40, match_total=40, min_samples=100),
            ),
        ).status_code
        == 201
    )
    sh = chk(view(renv, admin[0]))["shadow_evidence"]
    assert sh["status"] == "warn" and "40 of 100" in sh["summary"]


def test_stale_snapshot_is_never_healthy(renv, admin):
    seed_central_evidence(renv)
    assert put(renv, doc()).status_code == 201
    age_snapshot(renv, central_settings.sender_ops_stale_seconds + 60)
    body = view(renv, admin[0])
    meta = body["metrics"]["enterprise_snapshot"]
    assert (
        meta["state"] == "stale" and meta["age_seconds"] > central_settings.sender_ops_stale_seconds
    )
    c = chk(body)
    assert c["enterprise_snapshot"]["status"] == "warn" and body["status"] == "warn"
    assert (
        c["enterprise_sync_runtime"]["status"] == "unknown"
        and "Stale evidence" in c["enterprise_sync_runtime"]["summary"]
    )  # pass i vjetruar ⇒ unknown
    assert not any(
        x["status"] == "pass" and x["source"] == "enterprise" and x["code"] != "enterprise_snapshot"
        for x in c.values()
    )
    assert any(
        a["code"] == "enterprise_snapshot" and a["severity"] == "warning" for a in body["alerts"]
    )
    age_snapshot(renv, central_settings.sender_ops_stale_seconds * ev.EXPIRE_FACTOR + 60)
    expired = view(renv, admin[0])
    assert (
        chk(expired)["enterprise_snapshot"]["status"] == "fail"
        and expired["status"] == "fail"
        and expired["metrics"]["enterprise_snapshot"]["state"] == "expired"
    )


def test_stale_snapshot_keeps_bad_news_visible(renv, admin):
    seed_central_evidence(renv)
    assert (
        put(
            renv,
            doc(
                shadow=dict(critical_total=2, comparisons_total=300),
                alerts={
                    "items": alert_items(sender_shadow_critical_drift="critical"),
                    "evaluated_age_seconds": 0,
                },
            ),
        ).status_code
        == 201
    )
    age_snapshot(renv, central_settings.sender_ops_stale_seconds + 60)
    assert chk(view(renv, admin[0]))["shadow_evidence"]["status"] == "fail"


def test_central_and_enterprise_bootstrap_evidence_disagreement_is_flagged(renv, admin):
    seed_central_evidence(renv, senders=10, tenants=2)  # Enterprise raporton 4 sender-a
    assert put(renv, doc()).status_code == 201
    body = view(renv, admin[0])
    a = chk(body)["bootstrap_evidence_agreement"]
    assert (
        a["status"] == "warn"
        and a["summary"] == "Central and Enterprise bootstrap evidence disagree."
    )
    assert a["details"]["central_senders"] == 10 and a["details"]["enterprise_senders"] == 4
    assert any(
        x["code"] == "bootstrap_evidence_agreement" and x["source"] == "enterprise"
        for x in body["alerts"]
    )


def test_bootstrap_issue_categories_are_bounded_and_unknown_ones_become_other(renv, admin):
    seed_central_evidence(renv)
    b = {
        "state": data()["bootstrap"]["state"],
        "issues": {
            "open": 3,
            "resolved": 1,
            "open_by_category": {"identity_conflict": 2, "brand_new_category": 1},
        },
    }
    assert put(renv, doc(bootstrap=b)).status_code == 201
    c = chk(view(renv, admin[0]))["bootstrap_issue_categories"]
    assert (
        c["status"] == "warn"
        and c["details"]["open_identity_conflict"] == 2
        and c["details"]["open_other"] == 1
        and "open_brand_new_category" not in c["details"]
    )


def test_cutover_acknowledgement_rules(renv, admin):
    seed_central_evidence(renv)
    bad = dict(authority_mode="central", ack_configured=True, ack_valid=False)
    assert put(renv, doc(cutover=bad)).status_code == 201
    assert chk(view(renv, admin[0]))["cutover_evidence"]["status"] == "fail"
    assert (
        put(
            renv,
            doc(NOW + timedelta(seconds=4), cutover=dict(evidence_count=0, latest_evidence=None)),
        ).status_code
        == 201
    )
    assert chk(view(renv, admin[0]))["cutover_evidence"]["status"] == "unknown"


def test_alerts_are_merged_with_a_source_and_deduplicated(renv, admin):
    seed_central_evidence(renv, unresolved=3)
    b = dict(
        data()["bootstrap"],
        issues={"open": 2, "resolved": 0, "open_by_category": {"policy_denied": 2}},
    )
    items = alert_items(
        sender_bootstrap_unresolved="warning", sender_policy_readiness_fail="critical"
    ) + [{"name": "sender_sync_lag", "level": "warning", "value": 5, "threshold": 1}]
    assert (
        put(
            renv,
            doc(bootstrap=b, alerts={"items": items[:11] + items[11:], "evaluated_age_seconds": 0}),
        ).status_code
        == 201
    )
    body = view(renv, admin[0])
    keys = [(a["source"], a["code"]) for a in body["alerts"]]
    assert len(keys) == len(set(keys))
    assert (
        ("central", "bootstrap_unresolved") in keys
        and ("enterprise", "sender_bootstrap_unresolved") in keys
        and ("enterprise", "sender_policy_readiness_fail") in keys
    )
    assert all(
        a["source"] in ("central", "enterprise") and a["observed_at"] for a in body["alerts"]
    )
    assert "enterprise_alerts" not in {a["code"] for a in body["alerts"]}


def test_corrupt_payload_and_multiple_deployments_do_not_break_the_view(renv, admin):
    assert put(renv, doc()).status_code == 201
    with Session(renv.eng) as s:
        s.scalar(select(SenderOpsSnapshot)).payload = {"unexpected": True}
        s.commit()
    body = view(renv, admin[0])
    assert (
        body["status"] != "pass"
        and chk(body)["enterprise_snapshot"]["status"] == "unknown"
        and body["metrics"]["enterprise"] is None
    )
    add_client(renv, "second", ["sender:report"], [renv.ids["e2"]])
    assert (
        put(
            renv, doc(NOW + timedelta(seconds=60)), "second", "kx", token=tok(renv, "second", "kx")
        ).status_code
        == 201
    )
    body = view(renv, admin[0])
    assert (
        body["metrics"]["enterprise_snapshot"]["deployments"] == 2
        and body["metrics"]["enterprise_snapshot"]["reporter"] == "second"
    )


def test_admin_and_operator_read_the_same_merged_view_and_service_clients_cannot(renv, admin):
    seed_central_evidence(renv)
    assert put(renv, doc()).status_code == 201
    a, o = view(renv, admin[0]), view(renv, admin[1])
    assert (
        a["status"] == o["status"] and a["sections"] == o["sections"] and a["alerts"] == o["alerts"]
    )
    assert a["metrics"]["enterprise"] == o["metrics"]["enterprise"]
    assert renv.get(ADMIN_URL).status_code == 401
    assert (
        renv.get(ADMIN_URL, headers=auth(tok(renv))).status_code == 401
    )  # JWT de servisi ≠ staf Central
    assert renv.post(ADMIN_URL, headers=admin[0]).status_code == 405


def test_merged_payload_leaks_no_secrets_or_sender_values(renv, admin):
    seed_central_evidence(renv)
    assert put(renv, doc()).status_code == 201
    text_ = renv.get(ADMIN_URL, headers=admin[0]).text
    for needle in (
        "Acme",
        "acme",
        "password",
        "Bearer",
        "postgresql://",
        "BEGIN PRIVATE KEY",
        "sms-sender-",
        "client_pk",
        "k1",
    ):
        assert needle not in text_, needle


def test_merged_view_query_count_is_fixed_and_it_writes_nothing(renv, admin):
    seed_central_evidence(renv)
    assert put(renv, doc()).status_code == 201
    opsvc.clear_cache()
    renv.get(ADMIN_URL, headers=admin[0])  # ngroh cache-in e kontrolleve të plota
    stmts: list[str] = []
    event.listen(renv.eng, "before_cursor_execute", lambda _c, _cur, s, *_a: stmts.append(s))
    with Session(renv.eng) as s:
        before = (
            s.scalar(select(func.count()).select_from(SenderOpsSnapshot)),
            s.execute(text("select count(*) from audit_log")).scalar(),
        )
    renv.get(ADMIN_URL, headers=admin[0])
    n_small = len([x for x in stmts if x.lstrip().upper().startswith("SELECT")])
    assert n_small <= 22
    with Session(renv.eng) as s:
        assert (
            s.scalar(select(func.count()).select_from(SenderOpsSnapshot)),
            s.execute(text("select count(*) from audit_log")).scalar(),
        ) == before
    assert not [x for x in stmts if x.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
