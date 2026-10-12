# ruff: noqa: F811
"""M11-D — pamja operacionale READ-ONLY e sender governance (GET /admin/operations/sender): auth/RBAC, skema, statuset, cache, kufij pyetjesh, pa rrjedhje."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text

from apps.central.main import create_app
from apps.central.models import AuditLog
from apps.central.models.sender import SenderBootstrapRun, SenderRequestOperation
from apps.central.services import sender_operations as ops
from apps.central.services import senders as svc
from tests.test_central import IS_PG, central_alembic, make_db  # noqa: F401
from tests.test_central_auth import PW, auth_secret, bearer, mk, token_for  # noqa: F401
from tests.test_central_products import cdb  # noqa: F401
from tests.test_m9g1_billing import U, b  # noqa: F401

T0 = datetime(2024, 1, 1, tzinfo=UTC)
URL = "/admin/operations/sender"


@pytest.fixture(autouse=True)
def fresh_cache():
    ops.clear_cache()
    yield
    ops.clear_cache()


@pytest.fixture
def api(b):
    mk(b.eng, "ro@example.com", role="operator")
    c = TestClient(create_app(b.eng))
    return c, bearer(token_for(c, "a1@example.com")), bearer(token_for(c, "ro@example.com"))


def seed(b, n=1, value="SecretBr"):
    ids = []
    with b.F() as s:
        a = U(s, b.a1)
        svc.set_policy(s, a, "AL", "alphanumeric", True, True, "x", now=T0)
        for i in range(n):
            r = svc.request_sender(
                s,
                a,
                b.e1,
                f"r-{uuid.uuid4().hex[:10]}",
                "AL",
                f"{value}{i}" if n > 1 else value,
                now=T0 + timedelta(minutes=i + 1),
            )
            ids.append(r.sender.id)
        s.commit()
    return ids


def sections(body):
    return {s["code"]: s for s in body["sections"]}


def checks(body):
    return {c["code"]: c for s in body["sections"] for c in s["checks"]}


# --- auth / RBAC -----------------------------------------------------------------------------


def test_requires_authentication_and_has_no_write_methods(api):
    c, ad, _ = api
    assert c.get(URL).status_code == 401
    assert c.get(URL, headers=bearer("garbage")).status_code == 401
    for method in ("post", "put", "patch", "delete"):
        assert getattr(c, method)(URL, headers=ad).status_code == 405


def test_admin_and_operator_read_the_same_view(api, b):
    c, ad, ro = api
    seed(b)
    a, o = c.get(URL, headers=ad), c.get(URL, headers=ro)
    assert a.status_code == o.status_code == 200
    strip = lambda d: {k: v for k, v in d.items() if k not in ("generated_at",)}  # noqa: E731
    assert strip(a.json())["sections"] == strip(o.json())["sections"]


def test_disabled_user_is_rejected(api, b):
    c, ad, _ = api
    with b.eng.begin() as conn:
        conn.execute(text("UPDATE users SET status = 'disabled' WHERE email = 'a1@example.com'"))
    assert c.get(URL, headers=ad).status_code == 401


# --- skema / statuse ---------------------------------------------------------------------------


def test_empty_state_is_unknown_not_pass_and_schema_is_stable(api):
    c, ad, _ = api
    body = c.get(URL, headers=ad).json()
    assert set(body) >= {
        "status",
        "generated_at",
        "sections",
        "metrics",
        "alerts",
        "readiness_cached",
        "readiness_ttl_seconds",
    }
    assert [s["code"] for s in body["sections"]] == [
        "central_readiness",
        "sync",
        "requests",
        "bootstrap",
        "enterprise_evidence",
    ]
    for s in body["sections"]:
        assert s["status"] in ("pass", "warn", "fail", "unknown")
        for ch in s["checks"]:
            assert set(ch) == {"code", "status", "summary", "details"} and isinstance(
                ch["details"], dict
            )
    assert sections(body)["central_readiness"]["status"] == "pass"
    assert sections(body)["sync"]["status"] == "unknown"  # runtime Enterprise e panjohur
    assert body["status"] == "unknown" and body["alerts"] == []


def test_enterprise_only_evidence_is_always_unknown_with_a_tool_hint(api):
    c, ad, _ = api
    ch = checks(c.get(URL, headers=ad).json())
    for code, tool in ops.ENTERPRISE_ONLY.items():
        assert ch[code]["status"] == "unknown", code
        assert ch[code]["details"] == {"source": "enterprise", "tool": tool}
        assert "Not available to Central" in ch[code]["summary"]


def test_unknown_is_never_reported_as_pass(api):
    c, ad, _ = api
    body = c.get(URL, headers=ad).json()
    assert body["status"] != "pass"
    assert sections(body)["enterprise_evidence"]["status"] == "unknown"
    assert (
        ops.worst(["pass", "unknown"]) == "unknown"
        and ops.worst(["pass", "unknown", "warn"]) == "warn"
    )
    assert ops.worst(["warn", "fail", "unknown"]) == "fail" and ops.worst([]) == "unknown"


def test_sync_metrics_follow_the_feed_and_gap_is_a_critical_alert(api, b):
    c, ad, _ = api
    seed(b, 2)
    body = c.get(URL, headers=ad).json()
    m = body["metrics"]["sync"]
    assert (
        m["last_seq"] >= 3
        and m["retained_events"] == m["last_seq"] - m["floor_seq"]
        and m["last_event_at"]
    )
    assert checks(body)["sync_sequence_consistent"]["status"] == "pass"
    with b.eng.begin() as conn:
        conn.execute(text("UPDATE sender_sync_sequence SET last_seq = last_seq + 5"))
    body = c.get(URL, headers=ad).json()
    assert checks(body)["sync_sequence_consistent"]["status"] == "fail" and body["status"] == "fail"
    assert {"code": "sync_gap", "severity": "critical"}.items() <= next(
        a for a in body["alerts"] if a["code"] == "sync_gap"
    ).items()


def test_central_readiness_failure_maps_to_fail_and_a_readiness_alert(api, b):
    if b.eng.dialect.name != "sqlite":
        pytest.skip("PG triggers prevent corrupting the registry")
    c, ad, _ = api
    seed(b)
    with b.eng.begin() as conn:
        conn.execute(text("UPDATE sender_registry SET current_decision_id = NULL"))
    body = c.get(URL, headers=ad).json()
    assert sections(body)["central_readiness"]["status"] == "fail" and body["status"] == "fail"
    assert any(
        a["code"] == "readiness_fail" and a["severity"] == "critical" for a in body["alerts"]
    )


def test_request_ingest_evidence(api, b):
    c, ad, _ = api
    ids = seed(b)
    assert checks(c.get(URL, headers=ad).json())["request_ingest"]["status"] == "unknown"
    with b.F() as s:
        s.add(
            SenderRequestOperation(
                operation_id=uuid.uuid4(),
                enterprise_id=b.e1,
                registry_id=ids[0],
                external_ref="sms-sender-1",
                operation="request",
                request_hash="0" * 64,
                outcome="created",
                auto="not_applicable",
                status_after="pending",
                received_at=datetime.now(UTC),
            )
        )
        s.commit()
    body = c.get(URL, headers=ad).json()
    r = body["metrics"]["requests"]
    assert checks(body)["request_ingest"]["status"] == "pass"
    assert (
        r["total"] == 1
        and r["by_outcome"] == {"created": 1}
        and r["received_24h"] == 1
        and r["last_received_at"]
    )
    assert (
        checks(body)["enterprise_request_outbox"]["status"] == "unknown"
    )  # backlog/retry/failed: runtime Enterprise


@pytest.mark.parametrize(
    ("status", "unresolved", "run_status", "unres_status", "alert"),
    [
        ("completed", 0, "pass", "pass", None),
        ("completed", 3, "pass", "warn", "bootstrap_unresolved"),
        ("running", 0, "warn", "unknown", None),
        ("failed", 0, "fail", "unknown", "bootstrap_failed"),
    ],
)
def test_bootstrap_states(api, b, status, unresolved, run_status, unres_status, alert):
    c, ad, _ = api
    assert checks(c.get(URL, headers=ad).json())["bootstrap_run"]["status"] == "unknown"
    with b.F() as s:
        s.add(
            SenderBootstrapRun(
                artifact_hash="a" * 64,
                report_hash="b" * 64,
                source_revision="8c820ff",
                status=status,
                started_at=T0,
                completed_at=T0 if status == "completed" else None,
                tenant_count=2,
                sender_count=10,
                imported_count=7,
                unresolved_count=unresolved,
            )
        )
        s.commit()
    body = c.get(URL, headers=ad).json()
    latest = body["metrics"]["bootstrap"]["latest"]
    assert (
        checks(body)["bootstrap_run"]["status"],
        checks(body)["bootstrap_unresolved"]["status"],
    ) == (run_status, unres_status)
    assert (
        latest["artifact_hash"] == "a" * 12
        and latest["report_hash"] == "b" * 12
        and latest["source_revision"] == "8c820ff"
    )
    assert ("a" * 64) not in json.dumps(body)
    assert ({a["code"] for a in body["alerts"]} & {"bootstrap_unresolved", "bootstrap_failed"}) == (
        {alert} if alert else set()
    )
    assert checks(body)["bootstrap_issue_categories"]["status"] == "unknown"
    if unresolved:
        assert checks(body)["bootstrap_unresolved"]["details"] == {
            "unresolved_count": 3,
            "blocks_cutover": True,
        }


def test_unsafe_source_revision_is_dropped(api, b):
    c, ad, _ = api
    with b.F() as s:
        s.add(
            SenderBootstrapRun(
                artifact_hash="a" * 64,
                source_revision="https://user:pw@host/x",
                status="completed",
                started_at=T0,
                completed_at=T0,
            )
        )
        s.commit()
    assert (
        c.get(URL, headers=ad).json()["metrics"]["bootstrap"]["latest"]["source_revision"] is None
    )


# --- privatësi / jo-shkrim / performancë --------------------------------------------------------------------


def test_payload_has_no_sender_values_secrets_or_tokens(api, b):
    c, ad, _ = api
    seed(b, 2, value="SecretBr")
    text_ = c.get(URL, headers=ad).text
    for needle in (
        "SecretBr",
        "secretbr",
        "password",
        "Bearer",
        ad["Authorization"][7:],
        "postgresql://",
        "BEGIN PRIVATE KEY",
        "sms-sender-",
    ):
        assert needle not in text_, needle


def test_reads_write_nothing_and_create_no_audit_rows(api, b):
    c, ad, ro = api
    seed(b)
    with b.F() as s:
        before = (
            s.scalar(select(func.count()).select_from(AuditLog)),
            s.execute(text("SELECT last_seq FROM sender_sync_sequence")).scalar(),
        )
    for _ in range(3):
        for h in (ad, ro):
            assert c.get(URL, headers=h).status_code == 200
    with b.F() as s:
        after = (
            s.scalar(select(func.count()).select_from(AuditLog)),
            s.execute(text("SELECT last_seq FROM sender_sync_sequence")).scalar(),
        )
    assert before == after


def _count_statements(b, fn):
    stmts: list[str] = []

    def cb(_c, _cur, statement, *_a):
        stmts.append(statement)

    event.listen(b.eng, "before_cursor_execute", cb)
    try:
        fn()
    finally:
        event.remove(b.eng, "before_cursor_execute", cb)
    return stmts


def test_readiness_is_cached_and_cached_polls_run_a_fixed_number_of_queries(api, b):
    c, ad, _ = api
    seed(b)
    first = c.get(URL, headers=ad).json()
    assert first["readiness_cached"] is False
    stmts = _count_statements(b, lambda: c.get(URL, headers=ad))
    body = c.get(URL, headers=ad).json()
    assert (
        body["readiness_cached"] is True
        and body["readiness_computed_at"] == first["readiness_computed_at"]
    )
    assert not any(
        "FROM sender_decisions" in s for s in stmts
    )  # kontrollet e plota nuk rilexohen brenda TTL
    small = len([s for s in stmts if s.lstrip().upper().startswith("SELECT")])
    seed(b, 30)  # shumë më tepër sender-a: numri i pyetjeve nuk rritet
    stmts_big = _count_statements(b, lambda: c.get(URL, headers=ad))
    assert len([s for s in stmts_big if s.lstrip().upper().startswith("SELECT")]) == small <= 14


def test_cache_expires_after_the_ttl(api, b, monkeypatch):
    c, ad, _ = api
    assert c.get(URL, headers=ad).json()["readiness_cached"] is False
    clock = iter([10_000.0, 10_000.0 + ops.READINESS_TTL_SECONDS + 1] * 3)
    monkeypatch.setattr(ops, "monotonic", lambda: next(clock))
    ops.clear_cache()
    first = c.get(URL, headers=ad).json()
    second = c.get(URL, headers=ad).json()
    assert first["readiness_cached"] is False and second["readiness_cached"] is False


def test_central_checks_are_the_cli_checks_not_a_second_implementation(api, b):
    from apps.central.services import sender_central_readiness as ready

    c, ad, _ = api
    seed(b)
    body = c.get(URL, headers=ad).json()
    with b.F() as s:
        cli = [(x.name, x.level.lower(), x.reason) for x in ready.checks(s)]
    assert [
        (x["code"], x["status"], x["summary"])
        for x in sections(body)["central_readiness"]["checks"]
    ] == cli
