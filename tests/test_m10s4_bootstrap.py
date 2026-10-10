# ruff: noqa: F811
"""M10-S4 — bootstrap i senderave ekzistues: eksport Enterprise → dry-run/apply Central (idempotent, në batch-e, i auditueshëm) → S2 → rakordim Enterprise me gjendje të qëndrueshme."""

import json
import uuid

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import SessionLocal
from app.models.enterprise_registry import resolve_id
from app.models.messaging import ApprovalStatus, SenderDecision, SenderId, SenderKind
from app.models.sender_authority import SenderBootstrapState
from app.services import control_plane_client as cc
from app.services import sender_authority_readiness as ar
from app.services import sender_bootstrap as eb
from app.services import sender_ids as sid
from app.services import sender_sync_poller as sp
from apps.central.core.errors import Invalid
from apps.central.models.sender import (
    SenderBootstrapRun,
    SenderRegistry,
)
from apps.central.models.sender import (
    SenderDecision as CDecision,
)
from apps.central.services import audit as caudit
from apps.central.services import sender_bootstrap as cb
from apps.central.services import senders as csvc
from apps.central.tools import sender_import
from packages.contracts.control_plane.sender import request_v1 as rv
from tests.test_central import central_alembic, make_db  # noqa: F401
from tests.test_m10s3_central import A, mutate, renv  # noqa: F401
from tests.test_m10s3_enterprise import loop, reporter  # noqa: F401
from tests.test_pipeline import fake, world  # noqa: F401


def mk(db, value, status="approved", country="AL", owner="c1"):
    s = sid.request(db, owner, country, value)
    if status == "approved":
        sid.approve(db, s.id, "staff")
    elif status == "rejected":
        sid.reject(db, s.id, "staff", "no")
    db.commit()
    return s


def central_register(loop, ref, country, value, approve=False, which="local"):
    eid = loop.eid if which == "local" else loop.ids["e2"]

    def go(s, a):
        r = csvc.request_sender(s, a, eid, ref, country, value).sender
        if approve:
            csvc.approve(s, a, r.id)
        return r.id

    return mutate(loop, go)


@pytest.fixture
def scenario(db, loop):
    """Gjendje lokale për çdo kategori; kthen {emri: SenderId}."""
    out = {}
    out["A"] = mk(db, "APPRV")  # i miratuar, mungon në Central
    out["B"] = mk(db, "PENDG", "pending")
    out["C"] = mk(db, "REJEC", "rejected")
    out["G"] = mk(db, "EXACT")  # i barabartë me Central
    central_register(loop, rv.external_ref_for(out["G"].id), "AL", "EXACT", approve=True)
    out["H"] = mk(db, "CONFL")  # konflikt identiteti: Central e ka nën ref tjetër
    central_register(loop, "other-ref", "AL", "CONFL")
    out["I"] = mk(db, "GLOB1")  # konflikt çelësi global: një tenant tjetër e ka të miratuar
    central_register(loop, "glob-other", "AL", "GLOB1", approve=True, which="other")
    out["J"] = mk(db, "CPEND")  # lokalisht i miratuar, në Central pending
    central_register(loop, rv.external_ref_for(out["J"].id), "AL", "CPEND")
    out["F"] = mk(db, "POLDN", country="XK")  # politika Central e ndalon
    mutate(loop, lambda s, a: csvc.set_policy(s, a, "XK", "alphanumeric", False, True, "ban"))
    bad = SenderId(
        owner_ref="c1",
        country="AL",
        value="ab",
        kind=SenderKind.ALPHANUMERIC,
        status=ApprovalStatus.APPROVED,
        norm_value="ab",
    )
    db.add(bad)
    db.commit()
    out["D"] = bad
    return out


def central_run(loop, art, **kw):
    with Session(loop.eng) as s:
        rep = cb.run(s, art, **kw)
        return rep


def cat_of(rep, sender):
    return {i["sender_id"]: i["category"] for i in rep["items"]}[sender.id]


def counts(loop):
    with Session(loop.eng) as s:
        return tuple(
            s.scalar(select(func.count()).select_from(m))
            for m in (SenderRegistry, CDecision, SenderBootstrapRun)
        )


def test_dry_run_classifies_every_category_and_writes_nothing(db, loop, scenario):
    art = eb.export(db, "rev-1")
    before = counts(loop)
    rep = central_run(loop, art)
    assert rep["mode"] == "dry_run" and counts(loop) == before  # asnjë shkrim, asnjë rresht run
    want = {"A": "missing_in_central", "B": "local_pending", "C": "local_inactive", "D": "invalid_legacy_identity", "F": "policy_denied", "G": "exact_match",
            "H": "identity_conflict", "I": "global_key_conflict", "J": "local_approved_central_pending"}  # fmt: skip
    assert {k: cat_of(rep, v) for k, v in scenario.items()} == want
    assert rep["unresolved"] == 5 and rep["imported"] == 0 and rep["senders"] == 9
    assert all(i["result"] is None for i in rep["items"]) and len(rep["report_hash"]) == 64
    assert cb.artifact_hash(art) == rep["artifact_hash"]


def test_missing_enterprise_mapping_is_reported_for_unmapped_tenants(db, loop, monkeypatch):
    monkeypatch.setattr(settings, "enterprise_dual_write", False)
    s = mk(db, "NOENT", owner="legacy-owner")
    assert s.enterprise_id is None
    rep = central_run(loop, eb.export(db))
    assert cat_of(rep, s) == "missing_enterprise_mapping" and rep["unresolved"] == 1
    unknown = eb.export(db)
    unknown["senders"][0]["enterprise_id"] = str(uuid.uuid4())  # enterprise i panjohur te Central
    assert cat_of(central_run(loop, unknown), s) == "missing_enterprise_mapping"


def test_apply_imports_only_safe_items_with_import_provenance_and_is_idempotent(db, loop, scenario):
    art = eb.export(db, "rev-1")
    rep = central_run(loop, art, apply=True, source_revision="rev-1")
    res = {i["sender_id"]: i["result"] for i in rep["items"] if i["result"]}
    assert res == {scenario["A"].id: "imported_approved", scenario["B"].id: "imported_pending"}
    assert rep["imported"] == 2 and "run_id" in rep
    with Session(loop.eng) as s:
        a = s.scalar(
            select(SenderRegistry).where(
                SenderRegistry.external_ref == rv.external_ref_for(scenario["A"].id)
            )
        )
        assert (a.source, a.current_status, a.approved_key) == ("import", "approved", "AL:apprv")
        b = s.scalar(
            select(SenderRegistry).where(
                SenderRegistry.external_ref == rv.external_ref_for(scenario["B"].id)
            )
        )
        assert (b.source, b.current_status) == ("import", "pending")
        decs = list(
            s.scalars(
                select(CDecision).where(CDecision.registry_id == a.id).order_by(CDecision.seq)
            )
        )
        assert [(d.decision, d.actor_label, d.source) for d in decs] == [
            ("requested", "system:sender-bootstrap", "import"),
            ("approved", "system:sender-bootstrap", "import"),
        ]
        assert all(d.evidence_ref.startswith("bootstrap:") for d in decs)
        # asnjë regjistër për konfliktet / politikën / identitetin e pavlefshëm / refuzuarin
        for k in ("C", "D", "F", "H", "I", "J"):
            assert s.scalar(
                select(SenderRegistry).where(
                    SenderRegistry.external_ref == rv.external_ref_for(scenario[k].id)
                )
            ) is None or k in ("J",)
        run = s.scalar(select(SenderBootstrapRun))
        assert (
            run.status,
            run.sender_count,
            run.imported_count,
            run.unresolved_count,
            run.report_hash,
            run.artifact_hash,
        ) == ("completed", 9, 2, 5, rep["report_hash"], rep["artifact_hash"])
        assert run.completed_at is not None and run.source_revision == "rev-1"
        assert (
            s.scalar(
                select(func.count())
                .select_from(caudit.AuditLog)
                .where(caudit.AuditLog.action == "sender.bootstrap")
            )
            == 1
        )
    snap = counts(loop)
    again = central_run(loop, art, apply=True)  # ri-ekzekutim: asgjë e re
    assert (
        again["imported"] == 0
        and again["summary"]["exact_match"] == 3
        and counts(loop)[:2] == snap[:2]
    )
    with Session(loop.eng) as s:
        n = s.scalar(
            select(func.count())
            .select_from(SenderRegistry)
            .where(SenderRegistry.approved_key == "AL:apprv")
        )
        assert n == 1  # pa miratim të dyfishtë


def test_apply_is_batched_tenant_bounded_and_resumable(db, loop, scenario):
    art = eb.export(db)
    other = central_run(
        loop, art, apply=True, enterprise_id=uuid.uuid4(), batch_size=1
    )  # tenant tjetër: asgjë
    assert other["senders"] == 0 and other["imported"] == 0
    first = central_run(loop, art, apply=True, batch_size=1)  # batch=1 ⇒ commit pas çdo importi
    assert first["imported"] == 2
    # "rifillim": një senderë i ri lokal shtohet; rikalimi importon vetëm atë
    mk(db, "LATER")
    again = central_run(loop, eb.export(db), apply=True, batch_size=1)
    assert again["imported"] == 1 and [i["result"] for i in again["items"] if i["result"]] == [
        "imported_approved"
    ]
    with pytest.raises(Invalid):
        central_run(loop, art, apply=True, batch_size=0)


def test_artifact_validation_is_strict(db):
    for bad in (
        {},
        {"schema": "x", "senders": []},
        {"schema": eb.SCHEMA, "senders": [{"sender_id": 1}]},
    ):
        with pytest.raises(Invalid):
            cb.validate(bad)
    good = {"schema": eb.SCHEMA, "senders": [{"sender_id": 1, "external_ref": "sms-sender-1", "enterprise_id": None, "country": "AL", "display_value": "Acme", "kind": "alphanumeric", "status": "approved"}]}  # fmt: skip
    assert len(cb.validate(good)) == 1
    dup = {**good, "senders": good["senders"] * 2}
    with pytest.raises(Invalid):
        cb.validate(dup)
    with pytest.raises(Invalid):
        cb.validate({**good, "senders": [{**good["senders"][0], "status": "bogus"}]})


def test_export_is_read_only_and_cli_writes_a_hashable_artifact(
    db, loop, scenario, tmp_path, capsys
):
    from scripts import sender_bootstrap as cli

    before = db.scalar(select(func.count()).select_from(SenderDecision))
    out = tmp_path / "s.json"
    assert cli.main(["export", "--out", str(out), "--source-revision", "r9"]) == 0
    art = json.loads(out.read_text())
    assert (
        art["schema"] == eb.SCHEMA and art["source_revision"] == "r9" and len(art["senders"]) == 9
    )
    assert all(
        set(x)
        == {
            "sender_id",
            "external_ref",
            "enterprise_id",
            "country",
            "display_value",
            "kind",
            "status",
        }
        for x in art["senders"]
    )
    assert "owner_ref" not in out.read_text()
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(SenderDecision)) == before


def test_import_cli_dry_run_then_apply_requires_the_hash_and_a_real_admin(
    db, loop, scenario, tmp_path, capsys
):
    art = eb.export(db)
    f = tmp_path / "a.json"
    f.write_text(json.dumps(art))
    assert sender_import.main(["--artifact", str(f), "--json"], engine=loop.eng) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["mode"] == "dry_run"
    h = rep["artifact_hash"]
    assert (
        sender_import.main(
            [
                "--artifact",
                str(f),
                "--apply",
                "--actor-email",
                "a1@example.com",
                "--ack-artifact-hash",
                "0" * 64,
            ],
            engine=loop.eng,
        )
        == 1
    )
    assert (
        sender_import.main(
            [
                "--artifact",
                str(f),
                "--apply",
                "--actor-email",
                "ghost@example.com",
                "--ack-artifact-hash",
                h,
            ],
            engine=loop.eng,
        )
        == 1
    )
    assert counts(loop)[2] == 0
    assert (
        sender_import.main(
            [
                "--artifact",
                str(f),
                "--apply",
                "--actor-email",
                "a1@example.com",
                "--ack-artifact-hash",
                h,
                "--json",
            ],
            engine=loop.eng,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["imported"] == 2
    with Session(loop.eng) as s:
        assert s.scalar(select(SenderBootstrapRun.actor_id)) == loop.ids["admin"]


def test_reconcile_reports_unresolved_and_does_not_mark_completion_when_any_remain(
    db, loop, scenario
):
    central_run(loop, eb.export(db), apply=True)
    assert sp.poll_once(SessionLocal, reporter(loop, cc.SENDER_SCOPE), snapshot_interval_s=3600).ok
    rep = eb.reconcile(db, record=True, source_revision="rev-1")
    db.commit()
    s = rep["summary"]
    assert s.get("exact_match", 0) >= 3  # A, B, G
    assert (
        rep["unresolved"] > 0
        and s["invalid_legacy_identity"] == 1
        and s["local_approved_central_pending"] == 1
    )
    st = db.get(SenderBootstrapState, 1)
    assert (
        st.completed_at is None
        and st.unresolved_count == rep["unresolved"]
        and st.report_hash == rep["report_hash"]
    )
    assert (
        ar.checks(db)[[c.name for c in ar.checks(db)].index("bootstrap_complete")].level == "FAIL"
    )


def test_clean_bootstrap_reconciles_records_completion_and_satisfies_readiness(db, loop):
    a, b, c = mk(db, "CLEAN1"), mk(db, "CLEAN2", "pending"), mk(db, "CLEAN3", "rejected")
    rep = central_run(loop, eb.export(db, "rev-2"), apply=True, source_revision="rev-2")
    assert rep["unresolved"] == 0 and rep["imported"] == 2
    assert sp.poll_once(SessionLocal, reporter(loop, cc.SENDER_SCOPE), snapshot_interval_s=3600).ok
    dry = eb.reconcile(db)
    db.rollback()
    assert (
        db.get(SenderBootstrapState, 1) is None
        or db.get(SenderBootstrapState, 1).completed_at is None
    )  # reconcile pa --record s'shkruan
    done = eb.reconcile(db, record=True, source_revision="rev-2")
    db.commit()
    assert done["unresolved"] == 0 and done["report_hash"] == dry["report_hash"]
    st = db.get(SenderBootstrapState, 1)
    assert (
        (
            st.bootstrap_version,
            st.sender_count,
            st.tenant_count,
            st.unresolved_count,
            st.source_revision,
        )
        == (1, 3, 1, 0, "rev-2")
        and st.completed_at
        and st.started_at
    )
    items = {x.name: x for x in ar.checks(db)}
    assert (
        items["bootstrap_complete"].level == "PASS"
        and items["local_approved_covered"].level == "PASS"
    )
    del a, b, c


def test_reconcile_cli_exit_codes_and_json(db, loop, scenario, capsys):
    from scripts import sender_bootstrap as cli

    assert cli.main(["reconcile", "--json"]) == 1  # të pazgjidhura
    doc = json.loads(capsys.readouterr().out)
    assert (
        doc["schema"] == eb.REPORT_SCHEMA
        and doc["unresolved"] > 0
        and "APPRV" not in json.dumps(doc)
    )
    db.expire_all()
    st = db.get(SenderBootstrapState, 1)
    assert st is None or st.completed_at is None


def test_central_0029_is_additive_reversible_and_matches_metadata(make_db):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    import apps.central.models  # noqa: F401
    from apps.central.core.db import Base

    url = make_db()
    central_alembic(url, "upgrade", "0028")
    eng = create_engine(url)
    before = {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    }
    assert "sender_bootstrap_runs" not in before
    central_alembic(url, "upgrade", "0029")
    assert "sender_bootstrap_runs" in inspect(eng).get_table_names()
    for t, cols in before.items():
        if t != "central_alembic_version":
            assert {c["name"] for c in inspect(eng).get_columns(t)} == cols

    def drift():
        with eng.connect() as c:
            ctx = MigrationContext.configure(c, opts={"compare_type": True})
            return [
                d
                for d in compare_metadata(ctx, Base.metadata)
                if "central_alembic_version" not in repr(d)
            ]

    assert drift() == []
    central_alembic(url, "downgrade", "0028")
    assert "sender_bootstrap_runs" not in inspect(eng).get_table_names()
    central_alembic(url, "upgrade", "head")
    assert drift() == []
    eng.dispose()


def test_central_bootstrap_code_does_not_import_enterprise_code():
    import ast
    from pathlib import Path

    for p in (Path(cb.__file__), Path(sender_import.__file__)):
        tree = ast.parse(p.read_text())
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert not any(m == "app" or m.startswith("app.") for m in mods), p
        assert "SMS_" not in p.read_text()
    assert resolve_id  # importi i përdorur nga fixture-t


def test_end_to_end_request_central_decision_sync_then_shadow_evidence_and_central_enforcement(
    db, loop, world, fake, monkeypatch
):
    """S3 (kërkesa) → Central miraton → S2 (projeksioni) → shadow regjistron driftin → central vendos; lokali mbetet pending gjatë gjithë kohës."""
    from datetime import timedelta

    from app.services import messages as msgs
    from app.services import sender_authority as sau
    from app.services import sender_request_outbox as ob
    from app.services.sender_authorization import SenderNotAllowed
    from tests.test_pipeline import OK

    monkeypatch.setattr(settings, "sender_shadow_sample_pct", 100)
    s = mk(db, "E2EOKX", "pending")
    now = __import__("datetime").datetime(2031, 1, 1, tzinfo=__import__("datetime").UTC)
    assert ob.deliver(SessionLocal, reporter(loop), now=now).ok
    rid = Session(loop.eng).scalar(
        select(SenderRegistry.id).where(SenderRegistry.external_ref == rv.external_ref_for(s.id))
    )
    mutate(loop, lambda cs, a: csvc.approve(cs, a, rid))
    assert sp.poll_once(SessionLocal, reporter(loop, cc.SENDER_SCOPE), snapshot_interval_s=3600).ok
    # shadow: lokali (pending) vendos ⇒ mohim; Central do ta lejonte ⇒ drift i regjistruar
    monkeypatch.setattr(settings, "sender_authority", "shadow")
    sau.clear_cache()
    with pytest.raises(SenderNotAllowed):
        msgs.submit(db, "c1", "k-e2e1", OK, "E2EOKX", text="hello")
    db.rollback()
    rows = db.execute(text("select category from sms_sender_authority_comparisons")).scalars().all()
    assert rows == ["local_deny_central_allow"] or rows == [
        "projection_stale"
    ]  # stale nëse sinkronizimi s'është "i freskët" sipas orës së testit
    # central: projeksioni vendos; SenderId lokal mbetet pending
    monkeypatch.setattr(settings, "sender_authority", "central")
    sau.clear_cache()
    m = msgs.submit(db, "c1", "k-e2e2", OK, "E2EOKX", text="hello")
    db.commit()
    assert (
        m.sender_authority_source == "central"
        and m.sender_registry_ref == rid
        and m.sender_ref is None
    )
    db.refresh(s)
    assert s.status == ApprovalStatus.PENDING and s.approved_key is None
    # Central revokon ⇒ sinkronizim ⇒ mohim, pa asnjë thirrje rrjeti në submit
    mutate(loop, lambda cs, a: csvc.revoke(cs, a, rid, "abuse"))
    assert sp.poll_once(SessionLocal, reporter(loop, cc.SENDER_SCOPE), snapshot_interval_s=3600).ok
    sau.clear_cache()
    with pytest.raises(SenderNotAllowed):
        msgs.submit(db, "c1", "k-e2e3", OK, "E2EOKX", text="hello")
    db.rollback()
    assert timedelta(0) == timedelta(0)
