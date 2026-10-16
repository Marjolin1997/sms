# ruff: noqa: F811
"""M11-D2 — Enterprise: ndërtimi i snapshot-it `sender.ops.v1` (pa N+1, pa vlera sender, pa shkrim), raportimi asinkron me izolim dështimesh, klienti, worker-i."""

import ast
import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from sqlalchemy import event, func, select

from app.core.config import settings
from app.core.db import SessionLocal, engine
from app.models.messaging import SenderId
from app.models.sender_authority import (
    SenderAuthorityComparison,
    SenderBootstrapIssue,
    SenderBootstrapState,
    SenderCutoverEvidence,
)
from app.models.sender_request import SenderRequestOutbox
from app.models.sender_sync import SenderSyncCursor
from app.services import control_plane_client as cc
from app.services import sender_ids as sid
from app.services import sender_ops_snapshot as ops
from packages.contracts.control_plane.sender import ops_v1 as v1
from tests.test_central_sync_api import keypair

NOW = datetime(2031, 1, 1, tzinfo=UTC)
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean():
    ops.reset_state()
    yield
    ops.reset_state()


def mk_senders(db, n, prefix="SECRET"):
    for i in range(n):
        sid.request(db, f"c{i % 3}", "AL", f"{prefix}{i}")
    db.commit()


def statements(fn):
    out: list[str] = []

    def cb(_c, _cur, statement, *_a):
        out.append(statement)

    event.listen(engine, "before_cursor_execute", cb)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", cb)
    return out


class Stub:
    """Klient i falsifikuar: regjistron thirrjet; `ops_script`/`req_script` = përjashtime për radhë."""

    def __init__(self, ops_script=(), req_script=()):
        self.ops_calls, self.req_calls = [], []
        self.ops_script, self.req_script = list(ops_script), list(req_script)

    def post_sender_ops(self, payload):
        self.ops_calls.append(payload)
        step = self.ops_script.pop(0) if self.ops_script else None
        if isinstance(step, Exception):
            raise step
        return {"status": "accepted", "snapshot_id": payload["snapshot_id"]}

    def post_sender_request(self, payload):
        self.req_calls.append(payload)
        step = self.req_script.pop(0) if self.req_script else None
        if isinstance(step, Exception):
            raise step
        return {
            "status": "accepted",
            "operation_id": payload["operation_id"],
            "outcome": "created",
            "registry_ref": str(uuid.uuid4()),
        }

    def close(self):
        pass


# --- ndërtimi ------------------------------------------------------------------------------------------------


def test_build_is_a_valid_contract_document_and_never_writes(db):
    assert db.get(SenderSyncCursor, 1) is None
    doc = ops.build(db, NOW)
    v1.SenderOpsV1.parse(doc)
    assert (
        doc["schema"] == "sender.ops.v1"
        and doc["scope"] == "deployment"
        and doc["generated_at"] == "2031-01-01T00:00:00Z"
    )
    assert all(
        doc["data"][k] is not None
        for k in ("sync", "requests", "shadow", "bootstrap", "cutover", "alerts")
    )
    db.rollback()
    assert db.get(SenderSyncCursor, 1) is None  # snapshot-i NUK krijon rreshtin e kursorit
    assert db.scalar(select(func.count()).select_from(SenderRequestOutbox)) == 0


def test_zero_false_and_unavailable_are_distinguishable(db, monkeypatch):
    d = ops.build(db, NOW)["data"]
    assert (
        d["requests"]["failed"] == 0 and d["requests"]["enabled"] is False
    )  # të matura: zero/false
    assert (
        d["sync"]["initialized"] is False
        and d["sync"]["cursor_seq"] is None
        and d["sync"]["lag"] is None
    )  # nuk ka kursor: null, jo 0
    assert (
        d["bootstrap"]["state"] is None
        and d["cutover"]["evidence_count"] == 0
        and d["cutover"]["latest_evidence"] is None
    )
    monkeypatch.setattr(
        ops.ob, "stats", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    d2 = ops.build(db, NOW)["data"]
    assert (
        d2["requests"] is None and d2["sync"] is not None
    )  # seksioni i prishur = e padisponueshme, pa rrëzuar të tjerët


def test_sections_report_real_values(db, monkeypatch):
    monkeypatch.setattr(settings, "sender_authority", "shadow")
    mk_senders(db, 2)
    cur = SenderSyncCursor(
        id=1,
        epoch=uuid.uuid4(),
        last_seq=40,
        latest_central_seq=45,
        last_success_at=NOW - timedelta(seconds=30),
        failure_count=2,
        gap_recoveries=1,
        drift_repairs=0,
    )
    db.add(cur)
    for cat, n in (
        ("match_allowed", 8),
        ("central_missing", 2),
        ("policy_mismatch", 1),
        ("sender_identity_mismatch", 1),
        ("local_deny_central_allow", 3),
    ):
        for i in range(n):
            db.add(
                SenderAuthorityComparison(
                    ref=f"m{cat}{i}",
                    country="AL",
                    category=cat,
                    local_allowed=True,
                    central_allowed=True,
                    central_reason="x",
                    identity_hash="0" * 16,
                    created_at=NOW,
                )
            )
    db.add(
        SenderBootstrapState(
            id=1,
            bootstrap_version=1,
            completed_at=NOW,
            tenant_count=3,
            sender_count=2,
            unresolved_count=1,
            report_hash="ab" * 32,
        )
    )
    s0 = db.scalars(select(SenderId)).first()
    db.add(
        SenderBootstrapIssue(
            enterprise_id=None,
            sender_id=s0.id,
            category="identity_conflict",
            identity_hash="1" * 16,
        )
    )
    db.add(
        SenderCutoverEvidence(
            kind="pre_cutover",
            evidence_hash="cd" * 32,
            authority_version=1,
            environment="development",
            code_revision="r",
            actor="a",
            readiness_status="WARN",
            readiness_hash="e" * 64,
            payload={},
            created_at=NOW,
        )
    )
    db.commit()
    d = ops.build(db, NOW)["data"]
    assert (
        d["sync"]["cursor_seq"],
        d["sync"]["latest_central_seq"],
        d["sync"]["lag"],
        d["sync"]["failure_count"],
        d["sync"]["gap_recoveries"],
    ) == (40, 45, 5, 2, 1)
    assert d["sync"]["last_success_age_seconds"] == 30 and d["sync"]["enabled"] is False
    r = d["requests"]
    assert (r["pending"], r["failed"], r["oldest_open_age_seconds"] is not None) == (2, 0, True)
    sh = d["shadow"]
    assert (
        sh["mode"] == "shadow"
        and sh["comparisons_total"] == 15
        and sh["match_total"] == 8
        and sh["central_missing"] == 2
        and sh["identity_mismatch"] == 1
    )
    assert (
        sh["policy_mismatch"] == 1
        and sh["local_deny_central_allow"] == 3
        and sh["critical_total"] >= 4
        and sh["by_category"]["match_allowed"] == 8
    )
    b = d["bootstrap"]
    assert b["state"] == {
        "completed": True,
        "version": 1,
        "completed_at": "2031-01-01T00:00:00Z",
        "tenant_count": 3,
        "sender_count": 2,
        "unresolved_count": 1,
        "report_hash": "abababababab",
    }
    assert b["issues"] == {"open": 1, "resolved": 0, "open_by_category": {"identity_conflict": 1}}
    c = d["cutover"]
    assert (
        c["authority_mode"] == "shadow"
        and c["evidence_count"] == 1
        and c["latest_evidence"]["hash"] == "cdcdcdcdcdcd"
        and c["ack_configured"] is False
        and c["ack_valid"] is None
    )
    names = {a["name"] for a in d["alerts"]["items"]}
    assert {
        "sender_sync_lag",
        "sender_request_oldest_age_seconds",
        "sender_shadow_critical_drift",
        "sender_policy_readiness_fail",
    } <= names


def test_snapshot_contains_no_sender_values_owner_refs_or_secrets(db):
    mk_senders(db, 3, prefix="TOPSECRET")
    sid.request(db, "acme-owner", "AL", "+355691234567")
    db.commit()
    doc = ops.build(db, NOW)
    # `snapshot_id` është një uuid4 i rastësishëm (jo vlerë biznesi): mund të përmbajë rastësisht nënstringje si "c0"
    # (owner_ref-et e seeded janë c0..c2). E verifikojmë formatin dhe e heqim nga teksti ku kërkohen rrjedhjet.
    assert str(uuid.UUID(doc["snapshot_id"])) == doc["snapshot_id"]
    text = json.dumps({k: v for k, v in doc.items() if k != "snapshot_id"})
    for needle in (
        "TOPSECRET",
        "topsecret",
        "355691234567",
        "acme-owner",
        "c0",
        "sms-sender-",
        "password",
        "BEGIN",
        "postgresql",
        "Bearer",
    ):
        assert needle not in text, needle


def test_query_count_is_independent_of_sender_volume(db):
    mk_senders(db, 1)
    ops.clear_cache()
    small = len(statements(lambda: ops.build(db, NOW)))
    mk_senders(db, 60, prefix="MORE")
    for i in range(30):
        db.add(
            SenderAuthorityComparison(
                ref=f"v{i}",
                country="AL",
                category="match_allowed",
                local_allowed=True,
                central_allowed=True,
                central_reason="x",
                identity_hash="0" * 16,
            )
        )
    db.commit()
    ops.clear_cache()
    big = len(statements(lambda: ops.build(db, NOW)))
    assert big == small and small <= 80


def test_expensive_alert_evaluation_is_cached_between_snapshots(db):
    ops.clear_cache()
    first = len(statements(lambda: ops.build(db, NOW)))
    second = len(statements(lambda: ops.build(db, NOW)))
    assert second < first
    assert ops.build(db, NOW)["data"]["alerts"]["evaluated_age_seconds"] >= 0


# --- raportimi -------------------------------------------------------------------------------------------


def factory_spy():
    opened = {"n": 0}

    def factory():
        s = SessionLocal()
        opened["n"] += 1
        orig_close = s.close

        def close():
            opened["n"] -= 1
            orig_close()

        s.close = close
        return s

    return factory, opened


def test_http_runs_outside_any_database_session(db):
    factory, opened = factory_spy()
    seen = {}

    class Spy(Stub):
        def post_sender_ops(self, payload):
            seen["open_sessions"] = opened["n"]
            return super().post_sender_ops(payload)

    stub = Spy()
    out = ops.report_once(factory, stub, now=NOW, mono=100.0)
    assert out.kind == "ok" and out.status == "accepted" and len(stub.ops_calls) == 1
    assert seen["open_sessions"] == 0  # seanca e leximit u mbyll para thirrjes së rrjetit
    v1.SenderOpsV1.parse(stub.ops_calls[0])


def test_interval_gates_reports_and_force_overrides(db):
    stub = Stub()
    assert ops.report_once(SessionLocal, stub, now=NOW, mono=100.0).kind == "ok"
    assert (
        ops.report_once(
            SessionLocal, stub, now=NOW, mono=100.0 + settings.sender_ops_interval_seconds - 1
        ).kind
        == "not_due"
    )
    assert len(stub.ops_calls) == 1
    assert ops.report_once(SessionLocal, stub, now=NOW, mono=101.0, force=True).kind == "ok"
    assert (
        ops.report_once(
            SessionLocal, stub, now=NOW, mono=100.0 + 2 * settings.sender_ops_interval_seconds + 5
        ).kind
        == "ok"
    )
    assert len(stub.ops_calls) == 3


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (cc.CpTransportError("net"), "transport_error"),
        (cc.CpAuthError("401"), "auth_error"),
        (cc.CpForbidden("403"), "forbidden_error"),
        (cc.CpProtocolError("bad"), "protocol_error"),
        (cc.CpReportRejected(422, "invalid"), "rejected"),
    ],
)
def test_transport_failures_are_contained_and_back_off(db, exc, kind):
    stub = Stub(ops_script=[exc])
    out = ops.report_once(SessionLocal, stub, now=NOW, mono=100.0)
    assert out.kind == kind and not out.ok
    assert ops.STATE.consecutive_failures == 1 and ops.STATE.failed == 1
    assert ops.report_once(SessionLocal, stub, now=NOW, mono=101.0).kind == "not_due"  # backoff
    ok = ops.report_once(SessionLocal, stub, now=NOW, mono=100.0 + 10_000)
    assert ok.kind == "ok" and ops.STATE.consecutive_failures == 0


def test_build_failure_is_reported_not_raised(db, monkeypatch):
    monkeypatch.setattr(ops, "build", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
    out = ops.report_once(SessionLocal, Stub(), now=NOW, mono=1.0)
    assert out.kind == "build_error"


# --- izolimi nga worker-i i kërkesave ---------------------------------------------------------------------------------------------------


def run_worker(monkeypatch, stub, *, requests_on, ops_on):
    from app import worker

    monkeypatch.setattr(settings, "sender_request_reporting", requests_on)
    monkeypatch.setattr(settings, "sender_ops_reporting", ops_on)
    monkeypatch.setattr(cc, "config_from_settings", lambda _s: object())
    monkeypatch.setattr(cc, "ControlPlaneClient", lambda *_a, **_k: stub)
    return worker.run_sender_request_reporter(once=True)


def pending_request(db):
    s = sid.request(db, "c1", "AL", "ACME")
    db.commit()
    return s


def sent_count(db):
    db.expire_all()
    return db.scalar(
        select(func.count())
        .select_from(SenderRequestOutbox)
        .where(SenderRequestOutbox.state == "sent")
    )


def test_ops_failure_never_blocks_sender_request_delivery(db, monkeypatch):
    pending_request(db)
    stub = Stub(ops_script=[RuntimeError("ops exploded")])
    assert run_worker(monkeypatch, stub, requests_on=True, ops_on=True) == 0  # cikli mbetet "ok"
    assert len(stub.req_calls) == 1 and sent_count(db) == 1 and len(stub.ops_calls) == 1


def test_ops_transport_failure_does_not_change_the_cycle_result(db, monkeypatch):
    pending_request(db)
    stub = Stub(ops_script=[cc.CpTransportError("down")])
    assert run_worker(monkeypatch, stub, requests_on=True, ops_on=True) == 0
    assert sent_count(db) == 1 and ops.STATE.failed == 1


def test_request_failure_does_not_stop_ops_reporting_and_keeps_its_own_outcome(db, monkeypatch):
    pending_request(db)
    stub = Stub(req_script=[cc.CpTransportError("down")])
    assert run_worker(monkeypatch, stub, requests_on=True, ops_on=True) == 1
    assert len(stub.ops_calls) == 1 and sent_count(db) == 0


def test_flags_select_which_paths_run(db, monkeypatch):
    pending_request(db)
    only_ops = Stub()
    assert run_worker(monkeypatch, only_ops, requests_on=False, ops_on=True) == 0
    assert only_ops.req_calls == [] and len(only_ops.ops_calls) == 1 and sent_count(db) == 0
    only_req = Stub()
    ops.reset_state()
    assert run_worker(monkeypatch, only_req, requests_on=True, ops_on=False) == 0
    assert only_req.ops_calls == [] and len(only_req.req_calls) == 1


# --- klienti ---------------------------------------------------------------------------------------------


def http_client(handler):
    key = load_pem_private_key(keypair()[0].encode(), password=None)
    return cc.ControlPlaneClient(
        cc.ControlPlaneConfig("http://central", "ent", "k1", key, 5.0),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        scope=cc.SENDER_REPORT_SCOPE,
    )


def test_client_posts_to_the_ops_endpoint_and_validates_the_response():
    payload = {"snapshot_id": str(uuid.uuid4())}
    seen = {}

    def handler(request):
        seen["path"], seen["body"] = request.url.path, json.loads(request.content)
        return httpx.Response(
            201, json={"status": "accepted", "snapshot_id": payload["snapshot_id"]}
        )

    assert http_client(handler).post_sender_ops(payload)["status"] == "accepted"
    assert seen["path"] == "/internal/sender/operations" and seen["body"] == payload


@pytest.mark.parametrize(
    ("resp", "exc"),
    [
        (
            httpx.Response(200, json={"status": "accepted", "snapshot_id": "other"}),
            cc.CpProtocolError,
        ),
        (httpx.Response(200, json={"status": "weird", "snapshot_id": "x"}), cc.CpProtocolError),
        (httpx.Response(422, json={"detail": {"code": "invalid"}}), cc.CpReportRejected),
        (httpx.Response(409, json={"detail": {"code": "conflict"}}), cc.CpReportRejected),
        (
            httpx.Response(403, json={"detail": {"code": "enterprise_not_authorized"}}),
            cc.CpReportRejected,
        ),
        (httpx.Response(403, json={"detail": {"code": "forbidden"}}), cc.CpForbidden),
        (httpx.Response(401, json={}), cc.CpAuthError),
        (httpx.Response(503, json={}), cc.CpTransportError),
    ],
)
def test_client_error_classification(resp, exc):
    with pytest.raises(exc):
        http_client(lambda _r: resp).post_sender_ops({"snapshot_id": "x"})


# --- shtigjet SMS të paprekura ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    [
        "messages",
        "sender_ids",
        "sender_authority",
        "sender_request_outbox",
        "sender_sync_poller",
        "campaigns",
        "wallet",
    ],
)
def test_sms_and_sender_paths_do_not_import_the_ops_reporter(module):
    path = ROOT / "app" / "services" / f"{module}.py"
    if not path.exists():
        pytest.skip("module absent")
    for node in ast.walk(ast.parse(path.read_text())):
        names = (
            [a.name for a in node.names]
            if isinstance(node, ast.Import)
            else ([node.module or ""] + [a.name for a in node.names])
            if isinstance(node, ast.ImportFrom)
            else []
        )
        assert not any("sender_ops_snapshot" in n for n in names), (module, names)


def test_importing_the_sms_pipeline_does_not_load_the_ops_reporter():
    code = "import app.services.messages, app.services.sender_authority, app.services.campaigns, sys; assert 'app.services.sender_ops_snapshot' not in sys.modules"
    r = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == 0, r.stderr[-400:]


def test_ops_reporting_requires_https_in_production_config(monkeypatch):
    monkeypatch.setattr(settings, "sender_ops_reporting", True)
    monkeypatch.setattr(settings, "cp_base_url", "http://central")
    assert any("SMS_SENDER_OPS_REPORTING" in p for p in settings.production_problems())
    monkeypatch.setattr(settings, "cp_base_url", "https://central")
    assert not any("SMS_SENDER_OPS_REPORTING" in p for p in settings.production_problems())
