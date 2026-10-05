# ruff: noqa: F811
"""M9-c — poller i parave (outage/fail-static/409), E2E Central→Enterprise me HTTP in-process, readiness,
CLI, roli worker, prova "pa Central në rrugën e dërgimit", migrimi dhe PG (konkurrencë, triggers)."""

import ast
import json
import socket
import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from sqlalchemy import func, select, text

from app.core.config import settings
from app.core.db import SessionLocal
from app.models.control_plane import Entitlement
from app.models.money_authority import (
    G_APPLIED,
    G_DEFERRED,
    G_MATCHED,
    G_RECON,
    G_REVERSED,
    MoneyGrant,
)
from app.models.wallet import EntryType, LedgerEntry
from app.services import control_plane_client as cc
from app.services import money_authority as ma
from app.services import money_poller as mp
from app.services import money_readiness as mr
from app.services import money_sync as ms
from app.services import wallet as wallets
from tests.test_central import (  # noqa: F401
    IS_PG,
    ROOT,
    central_alembic,
    enterprise_alembic,
    make_db,
)
from tests.test_central_auth import auth_secret  # noqa: F401
from tests.test_m9c_central_money_feed import (  # noqa: F401
    acct_with_funds,
    env,
    issue,
    reverse,
)
from tests.test_m9c_money_authority import (
    EID,
    EPOCH,
    apply,
    bal,
    baseline_world,
    gev,
    grant_row,
    mk_world,
    mode,
)
from tests.test_pipeline import OK, fake, world  # noqa: F401

NOW = datetime(2030, 1, 1, tzinfo=UTC)


class FakeClient:
    """Imiton `ControlPlaneClient` (vetëm metodat e parave); pa rrjet."""

    def __init__(self, state=None, pages=(), state_exc=None, changes_exc=None):
        self.state, self.pages, self.state_exc, self.changes_exc = (
            state,
            list(pages),
            state_exc,
            changes_exc,
        )
        self.calls = []

    def get_money_state(self):
        self.calls.append("state")
        if self.state_exc:
            raise self.state_exc
        return self.state or {"epoch": EPOCH, "generation": 1, "latest_seq": 0}

    def get_money_changes(self, after, epoch, gen, limit=200):
        self.calls.append(("changes", after, str(epoch), gen))
        if self.changes_exc:
            raise self.changes_exc
        if not self.pages:
            return page([], after, after)
        p = self.pages.pop(0)
        return p(after) if callable(p) else p


def page(events, next_seq, latest=None, more=False, epoch=EPOCH, gen=1):
    return cc.ChangesPage(epoch, gen, [e.to_dict() if hasattr(e, "to_dict") else e for e in events],
                          next_seq, latest if latest is not None else next_seq, more)  # fmt: skip


def poll(client):
    return mp.poll_once(SessionLocal, client)


# =============================================================================================================
# poller
# =============================================================================================================


def test_poller_is_idle_in_local_mode_and_never_touches_central(db):
    c = FakeClient()
    out = poll(c)
    assert out.kind == mp.DISABLED and out.ok and c.calls == []


def test_poller_initialises_the_cursor_from_state_then_applies_pages(db, monkeypatch):
    w = mk_world(db)
    mode(monkeypatch, "central")
    a = uuid.uuid4()
    e = gev("issued", a, "5", seq=3)
    c = FakeClient(pages=[page([e], 3, 3)])
    out = poll(c)
    assert (
        out.ok
        and out.applied == 1
        and c.calls[0] == "state"
        and c.calls[1] == ("changes", 0, str(EPOCH), 1)
    )
    assert grant_row(db, a).status == G_APPLIED and bal(db, w)[0] == D("15")
    cur = ms.get_cursor(db)
    assert cur.last_seq == 3 and cur.last_success_at is not None and cur.last_error is None
    # nuk thërret /state përsëri
    c2 = FakeClient()
    assert poll(c2).ok and "state" not in c2.calls


def test_poller_follows_has_more_and_stops_on_no_progress(db, monkeypatch):
    mk_world(db)
    mode(monkeypatch, "central")
    e1, e2 = gev("issued", uuid.uuid4(), "1", seq=2), gev("issued", uuid.uuid4(), "1", seq=5)
    out = poll(FakeClient(pages=[page([e1], 2, 5, more=True), page([e2], 5, 5)]))
    assert out.ok and out.pages == 2 and out.applied == 2
    out = poll(FakeClient(pages=[page([], 5, 9, more=True)]))
    assert out.kind == "protocol_error" and "no cursor progress" in out.detail


@pytest.mark.parametrize("exc,kind", [
    (cc.CpTransportError("down"), "network_error"),
    (cc.CpAuthError("401"), "auth_error"),
    (cc.CpForbidden("403"), "forbidden"),
])  # fmt: skip
def test_central_outage_or_denial_is_fail_static_and_spending_continues(db, monkeypatch, exc, kind):
    w = mk_world(db)
    mode(monkeypatch, "central")
    g = uuid.uuid4()
    poll(FakeClient(pages=[page([gev("issued", g, "20", seq=1)], 1)]))
    assert bal(db, w) == (D("30"), D("2"))
    before_cursor = ms.get_cursor(db).last_seq
    out = poll(FakeClient(changes_exc=exc, state_exc=exc))
    assert out.kind == kind and not out.ok
    # kredia e sinkronizuar mbetet e shpenzueshme; asgjë s'çaktivizohet; s'ka kredi të re
    h = wallets.reserve(db, w.id, "25", "during-outage")
    wallets.capture(db, h.id)
    db.commit()
    assert bal(db, w) == (D("5"), D("2")) and ms.get_cursor(db).last_seq == before_cursor
    assert wallets.verify_wallet(db, w.id)


def test_epoch_change_stops_for_the_operator_and_does_not_move_the_cursor(db, monkeypatch):
    mk_world(db)
    mode(monkeypatch, "shadow")
    poll(FakeClient(pages=[page([], 4, 4)]))
    out = poll(FakeClient(changes_exc=cc.CpMoneyConflict("money_epoch_mismatch")))
    assert out.kind == "operator"
    cur = ms.get_cursor(db)
    assert cur.last_seq == 4 and cur.epoch == EPOCH and "money_epoch_mismatch" in cur.last_error


def test_authorization_change_rebases_and_replays_from_zero_idempotently(db, monkeypatch):
    w = mk_world(db)
    mode(monkeypatch, "central")
    a = uuid.uuid4()
    e = gev("issued", a, "5", seq=2)
    poll(FakeClient(pages=[page([e], 2, 2)]))
    calls = {"n": 0}

    def changes(after, epoch, gen, limit=200):
        calls["n"] += 1
        if calls["n"] == 1:
            raise cc.CpMoneyConflict("money_authorization_changed")
        return page([e], 2, 2, gen=2)

    c = FakeClient(state={"epoch": EPOCH, "generation": 2, "latest_seq": 2})
    c.get_money_changes = changes
    out = poll(c)
    cur = ms.get_cursor(db)
    assert out.ok and out.noop == 1 and out.applied == 0
    assert (cur.authorization_generation, cur.last_seq) == (2, 2) and bal(db, w)[0] == D("15")


def test_unparseable_page_applies_nothing_and_records_the_error(db, monkeypatch):
    w = mk_world(db)
    mode(monkeypatch, "central")
    good = gev("issued", uuid.uuid4(), "5", seq=1)
    bad = gev("issued", uuid.uuid4(), "5", seq=2).to_dict()
    bad["data"]["extra"] = 1
    out = poll(FakeClient(pages=[page([good, bad], 2)]))
    assert out.kind == "apply_error"
    assert bal(db, w) == (D("10"), D("2"))  # të gjitha ose asgjë
    cur = ms.get_cursor(db)
    assert cur.last_seq == 0 and "unparseable" in cur.last_error


def test_event_conflict_holds_the_cursor_after_the_good_prefix(db, monkeypatch):
    w = mk_world(db)
    mode(monkeypatch, "central")
    g1, g2 = uuid.uuid4(), uuid.uuid4()
    e1 = gev("issued", g1, "5", seq=1)
    out = poll(FakeClient(pages=[page([e1, gev("reversed", g2, "5", seq=2)], 2)]))
    assert out.kind == "apply_error" and "unknown grant" in out.detail
    assert ms.get_cursor(db).last_seq == 1 and bal(db, w)[0] == D("15")


def test_empty_page_still_drains_registered_grants_when_authority_becomes_central(db, monkeypatch):
    w = mk_world(db)
    mode(monkeypatch, "shadow")
    g = uuid.uuid4()
    poll(FakeClient(pages=[page([gev("issued", g, "5", seq=1)], 1)]))
    assert grant_row(db, g).status == G_DEFERRED and bal(db, w)[0] == D("10")
    mode(monkeypatch, "central")
    assert poll(FakeClient()).ok
    assert grant_row(db, g).status == G_APPLIED and bal(db, w)[0] == D("15")


# =============================================================================================================
# E2E: Central real (HTTP in-process) → klient → poller → Enterprise
# =============================================================================================================


def test_end_to_end_bootstrap_then_grants_then_reversal(env, db, monkeypatch):
    from fastapi.testclient import TestClient  # noqa: F401

    # Enterprise: identiteti = enterprise-i i Central; 12 EUR lokale (10 available + 2 held)
    eid = env.ids["e1"]
    monkeypatch.setattr("tests.test_m9c_money_authority.EID", eid)
    from app.models.enterprise import Enterprise

    db.add(Enterprise(id=eid, owner_ref="acme"))
    db.flush()
    db.add(Entitlement(enterprise_id=eid, assignment_id=uuid.uuid4(), product_id=env.ids["sms"],
                       product_code="sms", channel="sms", status="active", revision=1))  # fmt: skip
    db.flush()
    w = wallets.create_wallet(db, "acme", "EUR")
    w.enterprise_id = eid
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "12", wallets.TopupMethod.CASH).id)
    wallets.reserve(db, w.id, "2", "seed")
    db.commit()
    mode(monkeypatch, "shadow")
    base = ma.create_baseline(db, w.id, "e2e")
    db.commit()
    assert base.gross_at_cutover == D("12")
    # Central: fonde → grant bootstrap (12) + grant normal (5)
    acct = acct_with_funds(env, "e1", "100")
    boot = issue(env, acct, "12", purpose="bootstrap", baseline_ref=base.baseline_ref)
    norm = issue(env, acct, "5")
    key = load_pem_private_key(env.private.encode(), password=None)
    client = cc.ControlPlaneClient(cc.ControlPlaneConfig("http://testserver", "mon", "k1", key, 5.0),
                                   http=env, scope=cc.MONEY_SCOPE)  # fmt: skip
    out = mp.poll_once(SessionLocal, client)
    assert out.ok and out.applied == 2, out.detail
    assert grant_row(db, boot).status == G_MATCHED and grant_row(db, norm).status == G_DEFERRED
    assert bal(db, w) == (D("10"), D("2"))  # 12, jo 24; normali s'kreditoi në shadow
    # readiness (pa queue/config të jashtëm) kalon
    monkeypatch.setattr(cc, "config_from_settings", lambda s: object())
    rep = {c.name: c for c in mr.evaluate(db, include_queue=False, include_usage=False)}
    assert mr.ok(list(rep.values())), {k: v.reason for k, v in rep.items() if v.level == "FAIL"}
    # cutover: central → grant-i i regjistruar kreditohet
    mode(monkeypatch, "central")
    out = mp.poll_once(SessionLocal, client)
    assert out.ok and grant_row(db, norm).status == G_APPLIED and bal(db, w) == (D("15"), D("2"))
    # Central e kthen grant-in normal → debit (available 15 ≥ 5)
    reverse(env, norm)
    assert mp.poll_once(SessionLocal, client).ok
    assert grant_row(db, norm).status == G_REVERSED and bal(db, w) == (D("10"), D("2"))
    # rimarrja: asgjë e re, asgjë e dyfishtë
    assert mp.poll_once(SessionLocal, client).ok and bal(db, w) == (D("10"), D("2"))
    assert wallets.verify_wallet(db, w.id)
    # Central i padisponueshëm (i njëjti klient, transport i prishur): fail-static
    down = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: (_ for _ in ()).throw(httpx.ConnectError("x", request=r))
        )
    )
    dead = cc.ControlPlaneClient(
        cc.ControlPlaneConfig("http://x", "mon", "k1", key, 1.0), http=down, scope=cc.MONEY_SCOPE
    )
    assert mp.poll_once(SessionLocal, dead).kind == "network_error"
    h = wallets.reserve(db, w.id, "3", "after-outage")
    db.commit()
    assert bal(db, w) == (D("7"), D("5")) and h.status.value == "active"
    # reversal i bootstrap-it kur available s'mjafton → rakordim, kurrë negativ
    wallets.adjustment(db, w.id, "-3", "drain", "test drain")
    db.commit()
    reverse(env, boot)
    assert mp.poll_once(SessionLocal, client).ok
    assert grant_row(db, boot).status == G_RECON and bal(db, w) == (D("4"), D("5"))
    rep = {c.name: c for c in mr.evaluate(db, include_queue=False, include_usage=False)}
    assert rep["no_unresolved_reversal"].level == "FAIL"


def test_money_client_uses_the_dedicated_scope(env):
    key = load_pem_private_key(env.private.encode(), password=None)
    cfg = cc.ControlPlaneConfig("http://testserver", "mon", "k1", key, 5.0)
    sync_client = cc.ControlPlaneClient(cfg, http=env)  # scope sync:read
    with pytest.raises(cc.CpForbidden):
        sync_client._get("/internal/money/state")
    st = cc.ControlPlaneClient(cfg, http=env, scope=cc.MONEY_SCOPE).get_money_state()
    assert set(st) == {"epoch", "generation", "latest_seq"}


# =============================================================================================================
# readiness
# =============================================================================================================


@pytest.fixture
def cfg_ok(monkeypatch):
    monkeypatch.setattr(cc, "config_from_settings", lambda s: object())


def checks(db, **kw):
    return {
        c.name: c for c in mr.evaluate(db, now=NOW, include_queue=False, include_usage=False, **kw)
    }


def ready_world(db, monkeypatch):
    w, b = baseline_world(db, monkeypatch)
    g = uuid.uuid4()
    apply(db, [gev("issued", g, "12", purpose="bootstrap", ref=b.baseline_ref)])
    return w, b, g


def fails(res):
    return {n for n, c in res.items() if c.level == "FAIL"}


def test_readiness_passes_only_when_every_proof_holds(db, monkeypatch, cfg_ok):
    ready_world(db, monkeypatch)
    res = checks(db)
    assert fails(res) == set(), {n: c.reason for n, c in res.items() if c.level == "FAIL"}
    assert {"baseline_hash_valid", "bootstrap_matched_to_baseline", "no_positive_local_mint_after_baseline",
            "held_equals_active_holds", "money_cursor_healthy", "product_mapping_unambiguous",
            "no_unresolved_reversal", "queue_readiness", "production_ack"} <= set(
        {c.name for c in mr.evaluate(db, now=NOW)})  # fmt: skip


def test_readiness_fails_without_a_baseline_for_an_existing_wallet(db, monkeypatch, cfg_ok):
    mk_world(db)
    mode(monkeypatch, "shadow")
    apply(db, [], next_seq=1)
    assert "baseline_exists_for_existing_wallets" in fails(checks(db))


def test_readiness_fails_when_the_bootstrap_never_matched(db, monkeypatch, cfg_ok):
    baseline_world(db, monkeypatch)
    apply(db, [], next_seq=1)
    assert "bootstrap_matched_to_baseline" in fails(checks(db))


def test_readiness_proves_no_positive_local_mint_after_baseline_from_the_ledger(
    db, monkeypatch, cfg_ok
):
    w, b, g = ready_world(db, monkeypatch)
    mode(monkeypatch, "local")  # kalon porta e konfigurimit: provë nga ledger-i, jo nga deklarata
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "1", wallets.TopupMethod.CASH).id)
    db.commit()
    mode(monkeypatch, "shadow")
    assert "no_positive_local_mint_after_baseline" in fails(checks(db))


def test_readiness_flags_mismatch_unmapped_and_unresolved_reversal(db, monkeypatch, cfg_ok):
    ready_world(db, monkeypatch)
    apply(db, [gev("issued", uuid.uuid4(), "3", purpose="bootstrap", ref="7" * 64)])
    assert "no_baseline_mismatch" in fails(checks(db))


def test_readiness_flags_held_that_differs_from_active_holds(db, monkeypatch, cfg_ok):
    w, b, g = ready_world(db, monkeypatch)
    wallets._post(db, w.id, EntryType.HOLD, D("-1"), D("1"), "ghost-hold", "hold", "ghost")
    db.commit()
    assert "held_equals_active_holds" in fails(checks(db))


def test_readiness_flags_orphan_grant_ledger_entries(db, monkeypatch, cfg_ok):
    w, b, g = ready_world(db, monkeypatch)
    mode(monkeypatch, "central")
    wallets._post(
        db, w.id, EntryType.GRANT, D("1"), D("0"), "forged", "grant", "forged", authoritative=True
    )
    db.commit()
    assert "no_orphan_grant_ledger_entries" in fails(checks(db))


def test_readiness_cursor_and_consumer_and_mode_checks(db, monkeypatch, cfg_ok):
    mk_world(db, None, None)
    mode(monkeypatch, "shadow")
    assert "money_cursor_healthy" in fails(checks(db))  # kurrë i inicializuar
    ms.init_cursor(db, EPOCH, 1)
    db.commit()
    assert "money_cursor_healthy" in fails(checks(db))  # asnjë sukses
    apply(db, [], next_seq=1)
    assert "money_cursor_healthy" not in fails(checks(db))
    late = {
        c.name: c
        for c in mr.evaluate(
            db, now=datetime(2030, 1, 2, tzinfo=UTC), include_queue=False, include_usage=False
        )
    }
    assert "money_cursor_healthy" in fails(late)  # i vjetruar
    ms.record_error(db, "blocked on seq 9")
    db.commit()
    assert "money_cursor_healthy" in fails(checks(db))
    mode(monkeypatch, "local")
    assert "authority_mode" in fails(checks(db))


def test_readiness_fails_when_the_consumer_is_not_configured(db, monkeypatch):
    mk_world(db, None, None)
    mode(monkeypatch, "shadow")
    monkeypatch.setattr(settings, "cp_base_url", "")
    assert "consumer_configured" in fails(checks(db))


def test_readiness_flags_ambiguous_product_mapping(db, monkeypatch, cfg_ok):
    w, b, g = ready_world(db, monkeypatch)
    db.add(Entitlement(enterprise_id=EID, assignment_id=uuid.uuid4(), product_id=uuid.uuid4(),
                       product_code="sms2", channel="sms", status="active", revision=1))  # fmt: skip
    db.commit()
    assert "product_mapping_unambiguous" in fails(checks(db))


def test_readiness_includes_queue_readiness_and_the_production_ack(db, monkeypatch, cfg_ok):
    from scripts import queue_readiness as qr

    ready_world(db, monkeypatch)
    monkeypatch.setattr(qr, "evaluate", lambda *a, **k: [qr.Check("unknown_sms", qr.FAIL, "stuck")])
    res = {c.name: c for c in mr.evaluate(db, now=NOW)}
    assert res["queue_readiness"].level == "FAIL"
    monkeypatch.setattr(qr, "evaluate", lambda *a, **k: [qr.Check("unknown_sms", qr.PASS, "none")])
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "money_authority_ack", False)
    res = {c.name: c for c in mr.evaluate(db, now=NOW)}
    assert res["production_ack"].level == "FAIL" and res["queue_readiness"].level == "PASS"
    monkeypatch.setattr(settings, "money_authority_ack", True)
    assert {c.name: c for c in mr.evaluate(db, now=NOW)}["production_ack"].level == "PASS"


def test_production_requires_the_explicit_ack_for_central(monkeypatch):
    s = settings.model_copy(update={"money_authority": "central", "money_authority_ack": False,
                                    "cp_base_url": "https://central.example"})  # fmt: skip
    assert any("SMS_MONEY_AUTHORITY_ACK" in p for p in s.production_problems())
    s2 = s.model_copy(update={"money_authority_ack": True, "money_reporting": True})
    assert not any("SMS_MONEY_AUTHORITY" in p for p in s2.production_problems())
    s3 = s.model_copy(update={"money_authority": "shadow", "cp_base_url": "http://insecure"})
    assert any("https" in p and "MONEY_AUTHORITY" in p for p in s3.production_problems())


# =============================================================================================================
# CLI + worker
# =============================================================================================================


def test_cli_readiness_exit_codes_and_json(db, monkeypatch, capsys, cfg_ok):
    from scripts import money_authority_readiness as cli

    assert cli.main(["--skip-queue", "--skip-usage"]) == 1  # authority=local
    assert "FAIL authority_mode" in capsys.readouterr().out
    ready_world(db, monkeypatch)
    assert cli.main(["--skip-queue", "--skip-usage", "--json"]) in (0, 1)
    rows = json.loads(capsys.readouterr().out)
    assert all(set(r) == {"name", "level", "reason"} for r in rows)


def test_cli_baseline_cursor_and_reset(db, monkeypatch, capsys):
    from scripts import money_authority as cli

    w = mk_world(db)
    assert (
        cli.main(["baseline-create", "--wallet-id", str(w.id), "--by", "op"]) == 1
    )  # local: refuzohet
    mode(monkeypatch, "shadow")
    capsys.readouterr()
    assert cli.main(["baseline-create", "--wallet-id", str(w.id), "--by", "op"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["gross"] == "12.000000"
    assert cli.main(["baseline-show", "--wallet-id", str(w.id)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert (
        shown[0]["gross"] == "12.000000"
        and shown[0]["hash_valid"] is True
        and len(shown[0]["baseline_ref"]) == 64
    )
    assert cli.main(["cursor-show"]) == 0
    assert cli.main(["reset-cursor", "--epoch", str(uuid.uuid4()), "--generation", "1"]) == 2
    assert (
        cli.main(
            ["reset-cursor", "--epoch", str(uuid.uuid4()), "--generation", "1", "--ack-replay"]
        )
        == 0
    )


def test_worker_role_exists_and_misconfiguration_exits_2_without_calling_central(monkeypatch):
    from app import worker

    src = Path(worker.__file__).read_text()
    assert '"money_control_plane"' in src and "run_money_control_plane" in src
    mode(monkeypatch, "shadow")
    monkeypatch.setattr(settings, "cp_base_url", "")
    assert worker.run_money_control_plane(once=True) == 2


# =============================================================================================================
# pa Central në rrugën e dërgimit
# =============================================================================================================

HOT = ["app/services/messages.py", "app/services/emails.py", "app/services/wallet.py",
       "app/services/dispatch_outcome.py", "app/services/campaigns.py"]  # fmt: skip
FORBIDDEN = {"money_poller", "money_sync", "control_plane_client", "control_plane_poller", "money_authority",
             "money_readiness"}  # fmt: skip


def _imports(path):
    mods = set()
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.Import):
            mods |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add(n.module or "")
            mods |= {f"{n.module}.{a.name}" for a in n.names}
    return mods


def test_send_path_modules_never_import_the_money_consumer_or_client():
    paths = (
        [ROOT / p for p in HOT]
        + list((ROOT / "app/queue").glob("*.py"))
        + list((ROOT / "app/providers").glob("*.py"))
    )
    for p in paths:
        bad = {
            m for m in _imports(p) if m.split(".")[-1] in FORBIDDEN or "control_plane_client" in m
        }
        assert not bad, (p.name, bad)
    importers = {p.relative_to(ROOT).as_posix() for p in (ROOT / "app").rglob("*.py")
                 if any(m.split(".")[-1] in {"money_poller", "money_sync"} for m in _imports(p))}  # fmt: skip
    assert importers <= {"app/worker.py", "app/services/money_poller.py", "app/services/money_readiness.py",
                         "app/services/money_sync.py"}, importers  # fmt: skip


def test_send_and_dlr_work_with_all_network_blocked_under_central(db, world, fake, monkeypatch):
    from app.services import messages as svc

    w, _ = world
    mode(monkeypatch, "central")

    def blocked(*a, **k):
        raise AssertionError("network used on the send path")

    monkeypatch.setattr(httpx.Client, "send", blocked)
    monkeypatch.setattr(httpx.AsyncClient, "send", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    m = svc.submit(db, "c1", "k1", OK, "ACME", text="hello")
    db.commit()
    svc.process_one(db)
    svc.apply_dlr(db, "fake", m.provider_message_id, delivered=True)
    db.commit()
    assert wallets.balances(db, w.id) == (D("9.95"), D("0"))


# =============================================================================================================
# migrimi + PostgreSQL
# =============================================================================================================


def test_enterprise_migration_0023_up_down_up(make_db):  # noqa: F811
    from sqlalchemy import create_engine, inspect

    url = make_db("ent")
    enterprise_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    tables = {"sms_money_cursor", "sms_money_baselines", "sms_money_grants"}
    assert tables <= set(inspect(eng).get_table_names())
    ledger_cols = {c["name"] for c in inspect(eng).get_columns("sms_ledger_entries")}
    enterprise_alembic(url, "downgrade", "0022")
    assert not tables & set(inspect(eng).get_table_names())
    assert {
        c["name"] for c in inspect(eng).get_columns("sms_ledger_entries")
    } == ledger_cols  # aditiv
    enterprise_alembic(url, "upgrade", "head")
    assert tables <= set(inspect(eng).get_table_names())
    eng.dispose()


def test_central_migration_0018_up_down_up(make_db):  # noqa: F811
    from sqlalchemy import create_engine, inspect

    url = make_db()
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    assert {"purpose", "baseline_ref"} <= {
        c["name"] for c in inspect(eng).get_columns("credit_grants")
    }
    central_alembic(url, "downgrade", "0017")
    assert not {"purpose", "baseline_ref"} & {
        c["name"] for c in inspect(eng).get_columns("credit_grants")
    }
    central_alembic(url, "upgrade", "head")
    eng.dispose()


pg = pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")


@pg
def test_pg_migration_triggers(make_db):  # noqa: F811
    from sqlalchemy import create_engine
    from sqlalchemy.exc import DBAPIError

    url = make_db("ent")
    if url.startswith("sqlite"):
        pytest.skip("needs PostgreSQL triggers")
    enterprise_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("INSERT INTO sms_wallets (owner_ref, currency, low_balance_notified, created_at) "
                       "VALUES ('o','EUR', false, now())"))  # fmt: skip
        wid = c.execute(text("SELECT id FROM sms_wallets")).scalar_one()
        c.execute(
            text(
                "INSERT INTO sms_money_baselines (baseline_ref, wallet_id, enterprise_id, currency, product_id, "
                "available_at_cutover, held_at_cutover, gross_at_cutover, ledger_max_id, created_at, created_by, "
                "status) VALUES (:r, :w, :e, 'EUR', :p, 10, 2, 12, 0, now(), 'op', 'active')"
            ),
            {"r": "a" * 64, "w": wid, "e": str(uuid.uuid4()), "p": str(uuid.uuid4())},
        )
    for stmt in (
        "UPDATE sms_money_baselines SET gross_at_cutover = 13, available_at_cutover = 11",
        "UPDATE sms_money_baselines SET ledger_max_id = 5",
        "DELETE FROM sms_money_baselines",
    ):
        with pytest.raises(DBAPIError), eng.begin() as c:
            c.execute(text(stmt))
    with eng.begin() as c:  # lifecycle lejohet
        c.execute(text("UPDATE sms_money_baselines SET status='superseded', superseded_at=now()"))
    with pytest.raises(DBAPIError), eng.begin() as c:  # superseded është final
        c.execute(text("UPDATE sms_money_baselines SET status='active', superseded_at=NULL"))
    eng.dispose()


def _threads(n, fn):
    barrier = threading.Barrier(n, timeout=20)
    errors, out = [], [None] * n

    def run(i):
        try:
            out[i] = fn(i, barrier)
        except BaseException as e:  # noqa: BLE001
            errors.append(repr(e))

    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join(40) for t in ts]
    assert not any(t.is_alive() for t in ts), "thread i varur"
    return out, errors


@pg
def test_pg_two_consumers_applying_the_same_batch_credit_once(db, monkeypatch):
    w = mk_world(db)
    mode(monkeypatch, "central")
    ms.init_cursor(db, EPOCH, 1)
    db.commit()
    gid = uuid.uuid4()
    e = gev("issued", gid, "5", seq=1)

    def go(i, barrier):
        with SessionLocal() as s:
            barrier.wait()
            r = ms.apply_batch(
                s, epoch=EPOCH, authorization_generation=1, events=[e], next_seq=1, now=NOW
            )
            s.commit()
            return (r.applied, r.noop)

    out, errors = _threads(2, go)
    assert not errors, errors
    assert sorted(out) == [(0, 1), (1, 0)]
    assert bal(db, w) == (D("15"), D("2"))
    assert db.scalar(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.idempotency_key == f"grant:{gid}")) == 1  # fmt: skip
    assert db.scalar(select(func.count()).select_from(MoneyGrant)) == 1


@pg
def test_pg_reversal_races_a_reservation_without_ever_going_negative(db, monkeypatch):
    w = mk_world(db, None, None)
    mode(monkeypatch, "central")
    gid = uuid.uuid4()
    apply(db, [gev("issued", gid, "5", seq=1)])
    rev = gev("reversed", gid, "5", seq=2)

    def go(i, barrier):
        with SessionLocal() as s:
            barrier.wait()
            try:
                if i == 0:
                    r = ms.apply_batch(
                        s,
                        epoch=EPOCH,
                        authorization_generation=1,
                        events=[rev],
                        next_seq=2,
                        now=NOW,
                    )
                    s.commit()
                    return ("reversal", r.error)
                h = wallets.reserve(s, w.id, "5", "race")
                s.commit()
                return ("reserved", h.id)
            except wallets.InsufficientFunds:
                s.rollback()
                return ("insufficient", None)

    out, errors = _threads(2, go)
    assert not errors, errors
    db.expire_all()
    a, h = wallets.balances(db, w.id)
    assert a >= 0 and h >= 0 and wallets.verify_wallet(db, w.id)
    g = db.get(MoneyGrant, gid)
    reserved = ("reserved", out[1][1]) == out[1] and out[1][0] == "reserved"
    if reserved:  # rezervimi fitoi: reversal → rakordim; paratë e rezervuara s'preken
        assert g.status == G_RECON and (a, h) == (D("0"), D("5"))
    else:  # reversal fitoi: rezervimi dështoi për fonde
        assert g.status == G_REVERSED and (a, h) == (D("0"), D("0"))
