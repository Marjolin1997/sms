# ruff: noqa: F811
"""M7-g: enforcement (off/shadow/enforce), tabela e vendimit, kufiri/min, të gjitha rrugët e dërgimit,
rollback, gatishmëria, performanca. Vetëm Enterprise (Central sinkronizohet nga teste të veçanta)."""

import ast
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import event, select

from app.core.config import Settings, settings
from app.core.db import SessionLocal, engine
from app.core.errors import DomainError
from app.models.campaigns import CampaignStatus
from app.models.control_plane import Entitlement
from app.models.enterprise import Enterprise
from app.models.sending import AccountPlan, Message
from app.services import campaigns as camp
from app.services import consent, emails, entitlements
from app.services import contacts as contacts_svc
from app.services import control_plane_client as cc
from app.services import control_plane_poller as poller
from app.services import control_plane_shadow as shadow
from app.services import control_plane_sync as cps
from app.services import messages as svc
from packages.contracts.control_plane import v1
from tests.test_campaigns import audience, campaign, drive, start
from tests.test_email import FROM, TO, fake_dns, fake_email_provider, verified  # noqa: F401
from tests.test_pipeline import OK, fake, world  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
NOW = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def mode(monkeypatch):
    """Zgjedh modalitetin; cache me TTL 0 që çdo ndryshim i gjendjes CP të duket menjëherë."""
    monkeypatch.setattr(shadow, "CACHE_TTL_S", 0.0)
    monkeypatch.setattr(entitlements, "CACHE_TTL_S", 0.0)
    shadow.stats.reset()
    entitlements.stats.reset()
    shadow.clear_cache()

    def set_(m):
        monkeypatch.setattr(settings, "cp_sync_mode", m)
        shadow.clear_cache()

    yield set_
    shadow.clear_cache()


def eid_of(db):
    return db.scalar(select(AccountPlan.enterprise_id))


def put_cp(db, eid, *, ent_status="active", sms=None, email=None, last_ok=None, rev=3):
    """Gjendje CP lokale. `sms`/`email` = status ose (status, rate_limit_per_min)."""
    e = db.get(Enterprise, eid)
    e.status, e.cp_revision = ent_status, rev
    db.query(Entitlement).filter(Entitlement.enterprise_id == eid).delete()
    for ch, v in (("sms", sms), ("email", email)):
        if v is None:
            continue
        st, lim = v if isinstance(v, tuple) else (v, None)
        db.add(Entitlement(enterprise_id=eid, assignment_id=uuid.uuid4(), product_id=uuid.uuid4(),
                           product_code=f"{ch}_std", channel=ch, status=st, revision=1,
                           rate_limit_per_min=lim))  # fmt: skip
    cur = cps.get_cursor(db)
    cur.epoch, cur.authorization_generation = cur.epoch or uuid.uuid4(), 1
    cur.last_success_at = last_ok or datetime.now(UTC)
    db.commit()


def attempt(db, ch, key="k1", **kw):
    """→ "ok" ose kodi i gabimit publik. Një submit i vetëm, me commit/rollback."""
    try:
        if ch == "sms":
            svc.submit(db, "c1", key, OK, "ACME", text="hi", **kw)
        else:
            emails.submit(db, "c1", key, FROM, TO, "subject", "body", **kw)
        db.commit()
        return "ok"
    except DomainError as e:
        db.rollback()
        return e.code


def disable_legacy(db):
    db.scalar(select(AccountPlan)).enabled = False
    db.commit()


BOTH = pytest.mark.parametrize("ch", ["sms", "email"])


# --- autorizim: off / shadow / enforce -------------------------------------------------------------


@BOTH
def test_off_is_legacy_allow_and_legacy_deny_and_never_touches_cp(db, world, verified, mode, ch):
    mode("off")
    seen = []
    event.listen(engine, "before_cursor_execute", lambda *a: seen.append(a[2]))
    assert attempt(db, ch) == "ok"
    assert not [s for s in seen if "sms_entitlements" in s or "sms_cp_cursor" in s]  # 0 SQL CP
    disable_legacy(db)
    assert attempt(db, ch, "k2") == "account_disabled"


@BOTH
def test_shadow_outcome_is_identical_to_legacy_even_when_cp_denies(db, world, verified, mode, ch):
    eid = eid_of(db)
    put_cp(db, eid, ent_status="suspended", sms="suspended", email="suspended")
    mode("shadow")
    assert attempt(db, ch) == "ok"  # CP deny, legacy allow ⇒ shadow NUK refuzon
    disable_legacy(db)
    put_cp(db, eid, sms="active", email="active")
    assert attempt(db, ch, "k2") == "account_disabled"  # legacy deny fiton edhe me CP allow
    assert shadow.stats.snapshot()["legacy_allow_cp_deny"] == 1


@BOTH
def test_enforce_decision_table(db, world, verified, mode, ch):
    eid = eid_of(db)
    mode("enforce")
    put_cp(db, eid, sms="active", email="active")
    assert attempt(db, ch, "a") == "ok"  # legacy allow | CP allow ⇒ allow
    put_cp(db, eid, sms="suspended", email="suspended")
    assert attempt(db, ch, "b") == "product_suspended"  # legacy allow | CP deny ⇒ deny
    disable_legacy(db)
    put_cp(db, eid, sms="active", email="active")
    assert attempt(db, ch, "c") == "account_disabled"  # legacy deny | CP allow ⇒ deny (lokal)
    put_cp(db, eid, sms="suspended", email="suspended")
    assert attempt(db, ch, "d") == "account_disabled"  # legacy deny | CP deny ⇒ deny


@BOTH
def test_enforce_enterprise_suspended_entitlement_suspended_and_withdrawn(
    db, world, verified, mode, ch
):
    eid = eid_of(db)
    mode("enforce")
    put_cp(db, eid, ent_status="suspended", sms="active", email="active")
    assert attempt(db, ch, "a") == "enterprise_suspended"
    put_cp(db, eid, sms="suspended", email="suspended")
    assert attempt(db, ch, "b") == "product_suspended"
    put_cp(db, eid, sms="withdrawn", email="withdrawn")
    assert attempt(db, ch, "c") == "product_not_entitled"
    put_cp(db, eid, sms="active", email="active")
    assert attempt(db, ch, "d") == "ok"  # rikthim: nuk mbetet asgjë e ngecur


@BOTH
def test_enforce_never_synced_is_deny_and_does_not_fall_back_to_legacy_allow(
    db, world, verified, mode, ch
):
    mode("enforce")
    assert attempt(db, ch) == "product_not_entitled"  # cp_revision = 0
    put_cp(
        db,
        eid_of(db),
        sms="active" if ch == "email" else None,
        email="active" if ch == "sms" else None,
    )
    assert (
        attempt(db, ch, "k2") == "product_not_entitled"
    )  # sinkronizuar por pa entitlement të kanalit


@BOTH
def test_stale_but_previously_synced_keeps_last_known_good_and_only_logs(
    db, world, verified, mode, ch, caplog
):
    put_cp(
        db, eid_of(db), sms="active", email="active", last_ok=datetime.now(UTC) - timedelta(hours=3)
    )
    mode("enforce")
    caplog.set_level(logging.WARNING, logger="sms.cp.enforce")
    assert attempt(db, ch) == "ok"  # fail-static: s'ka deny nga vjetërsia
    assert "CRITICAL".lower() in caplog.text.lower() and "fail-static" in caplog.text
    put_cp(
        db,
        eid_of(db),
        sms="suspended",
        email="suspended",
        last_ok=datetime.now(UTC) - timedelta(hours=3),
    )
    assert attempt(db, ch, "k2") == "product_suspended"  # last-known-good vlen edhe për deny


@BOTH
def test_central_outage_does_not_change_the_local_decision(db, world, verified, mode, ch):
    eid = eid_of(db)
    put_cp(db, eid, sms="active", email="active")
    mode("enforce")
    assert attempt(db, ch, "a") == "ok"

    def boom(request):
        raise httpx.ConnectError("down", request=request)

    cfg = cc.ControlPlaneConfig("http://central.test", "c", "k", Ed25519PrivateKey.generate(), 5.0)
    client = cc.ControlPlaneClient(cfg, httpx.Client(transport=httpx.MockTransport(boom)))
    out = poller.poll_once(SessionLocal, client, snapshot_interval_s=1)
    assert out.kind == "network_error"
    assert attempt(db, ch, "b") == "ok"  # pa ndryshim
    put_cp(db, eid, sms="suspended", email="suspended")
    out = poller.poll_once(SessionLocal, client, snapshot_interval_s=1)
    assert out.kind == "network_error" and attempt(db, ch, "c") == "product_suspended"


# --- pavarësia e kanaleve ----------------------------------------------------------------------------


def test_channels_are_independent_and_enterprise_suspension_denies_both(db, world, verified, mode):
    eid = eid_of(db)
    mode("enforce")
    put_cp(db, eid, sms="active", email="suspended")
    assert (attempt(db, "sms", "a"), attempt(db, "email", "a")) == ("ok", "product_suspended")
    put_cp(db, eid, sms="suspended", email="active")
    assert (attempt(db, "sms", "b"), attempt(db, "email", "b")) == ("product_suspended", "ok")
    put_cp(db, eid, ent_status="suspended", sms="active", email="active")
    assert (attempt(db, "sms", "c"), attempt(db, "email", "c")) == ("enterprise_suspended",) * 2


def test_one_product_event_does_not_mutate_the_other(db, world, mode):
    eid = eid_of(db)
    put_cp(db, eid, sms="active", email="active")
    cur = cps.get_cursor(db)
    cur.epoch, cur.authorization_generation, cur.last_seq = uuid.uuid4(), 1, 10
    db.commit()
    rows = {e.channel: e for e in db.scalars(select(Entitlement))}
    sms = rows["sms"]
    ev = v1.ControlPlaneEventV1(
        str(uuid.uuid4()), 11, "enterprise_product.upserted", str(eid), str(sms.assignment_id), 2, NOW,
        v1.EnterpriseProductStateV1(str(sms.assignment_id), str(eid), str(sms.product_id), "sms_std",
                                    "sms", "suspended", 50),
    )  # fmt: skip
    cps.apply_feed_batch(db, epoch=cur.epoch, authorization_generation=1, events=[ev], next_seq=11)
    db.commit()
    db.expire_all()
    rows = {e.channel: e for e in db.scalars(select(Entitlement))}
    assert (rows["sms"].status, rows["sms"].rate_limit_per_min, rows["sms"].revision) == (
        "suspended",
        50,
        2,
    )
    assert (rows["email"].status, rows["email"].rate_limit_per_min, rows["email"].revision) == (
        "active",
        None,
        1,
    )


# --- kufiri/min --------------------------------------------------------------------------------------


def test_applier_stores_event_and_snapshot_rate_limits_and_null(db, world):
    eid = eid_of(db)
    put_cp(db, eid, sms=("active", 120), email="active")
    rows = {e.channel: e.rate_limit_per_min for e in db.scalars(select(Entitlement))}
    assert rows == {"sms": 120, "email": None}
    for bad in (0, -1, 1_000_001, True, "5", 1.5):
        with pytest.raises(v1.ContractError):
            v1.EnterpriseProductStateV1(str(uuid.uuid4()), str(eid), str(uuid.uuid4()), "sms_std",
                                        "sms", "active", bad)  # fmt: skip
    # snapshot e ruan; i munguar në JSON të vjetër = NULL (aditiv)
    snap = cps.parse_snapshot({
        "epoch": str(uuid.uuid4()), "authorization_generation": 1, "snapshot_seq": 1,
        "enterprises": [{"entity": {"type": "enterprise", "id": str(eid)}, "enterprise_id": str(eid),
                         "revision": 5, "data": {"id": str(eid), "name": "N", "status": "active"}}],
        "assignments": [
            {"entity": {"type": "enterprise_product", "id": str(a)}, "enterprise_id": str(eid),
             "revision": 1, "data": {**d, "assignment_id": str(a), "enterprise_id": str(eid)}}
            for a, d in ((uuid.uuid4(), {"product": {"id": str(uuid.uuid4()), "code": "sms_x", "channel": "sms"},
                                         "status": "active", "rate_limit_per_min": 77}),
                         (uuid.uuid4(), {"product": {"id": str(uuid.uuid4()), "code": "email_x", "channel": "email"},
                                         "status": "active"}))],
    })  # fmt: skip
    cps.apply_snapshot(db, snap, now=NOW)
    db.commit()
    got = {
        e.product_code: e.rate_limit_per_min
        for e in db.scalars(select(Entitlement))
        if e.product_code.endswith("_x")
    }
    assert got == {"sms_x": 77, "email_x": None}


@BOTH
def test_enforce_uses_cp_limit_null_uses_local_default_and_legacy_limit_is_not_authoritative(
    db, world, verified, mode, ch
):
    eid = eid_of(db)
    plan = db.scalar(select(AccountPlan))
    plan.rate_limit_per_min, plan.email_rate_limit_per_min = 1000, 1000  # legacy larg
    db.commit()
    mode("enforce")
    put_cp(db, eid, sms=("active", 2), email=("active", 2))
    assert [attempt(db, ch, f"k{i}") for i in range(3)] == [
        "ok",
        "ok",
        "rate_limited",
    ]  # 2/min nga CP
    put_cp(db, eid, sms=("active", None), email=("active", None))
    plan.rate_limit_per_min, plan.email_rate_limit_per_min = 1, 1  # legacy i ngushtë: ignorohet
    db.commit()
    assert attempt(db, ch, "k-null") == "ok"  # NULL ⇒ DEFAULT lokal (600), jo legacy
    assert svc.DEFAULT_RATE_LIMIT == 600


@BOTH
def test_off_still_uses_legacy_limit_and_rate_limit_algorithm_is_unchanged(
    db, world, verified, mode, ch
):
    mode("off")
    plan = db.scalar(select(AccountPlan))
    plan.rate_limit_per_min, plan.email_rate_limit_per_min = 2, 2
    db.commit()
    assert [attempt(db, ch, f"k{i}") for i in range(3)] == ["ok", "ok", "rate_limited"]
    src = (APP / "services/messages.py").read_text() + (APP / "services/emails.py").read_text()
    assert (
        src.count("created_at > now - timedelta(minutes=1)") == 2
    )  # i njëjti numërues, pa dyfishim


def test_effective_limit_is_the_minimum_of_active_non_null_limits():
    mk = shadow.CpState
    assert (
        shadow.effective_rate_limit(
            mk(True, "active", (("active", 50), ("active", 20), ("active", None)), None)
        )
        == 20
    )
    assert (
        shadow.effective_rate_limit(
            mk(True, "active", (("suspended", 5), ("withdrawn", 1), ("active", None)), None)
        )
        is None
    )


def test_shadow_compares_limit_sources_without_changing_outcome(db, world, verified, mode):
    plan = db.scalar(select(AccountPlan))
    plan.rate_limit_per_min = 5
    db.commit()
    put_cp(db, eid_of(db), sms=("active", 5), email="active")
    mode("shadow")
    attempt(db, "sms", "a")
    put_cp(db, eid_of(db), sms=("active", 9), email="active")
    attempt(db, "sms", "b")
    put_cp(db, eid_of(db), sms="active", email="active")
    attempt(db, "sms", "c")
    assert shadow.stats.rate == {"equal": 1, "different": 1, "cp_null": 1}


# --- erros publike ------------------------------------------------------------------------------------


def test_public_errors_are_stable_403_and_leak_no_sync_internals(db, world, verified, mode, client):
    eid = eid_of(db)
    mode("enforce")
    body = {"owner_ref": "c1", "to": OK, "sender": "ACME", "text": "hi"}
    put_cp(db, eid, ent_status="suspended", sms="active", email="active")
    for ent_status, sms_status, code in (("suspended", "active", "enterprise_suspended"),
                                         ("active", "suspended", "product_suspended"),
                                         ("active", "withdrawn", "product_not_entitled")):  # fmt: skip
        put_cp(db, eid, ent_status=ent_status, sms=sms_status, email="active")
        r = client.post("/v1/messages", json=body, headers={"Idempotency-Key": f"i-{code}"})
        assert r.status_code == 403 and r.json()["detail"]["code"] == code, r.text
        text = r.text.lower()
        assert not any(
            w in text for w in ("cp_", "cursor", "epoch", "stale", "sync", "revision", "withdrawn_")
        ), text
    put_cp(db, eid)  # kurrë i sinkronizuar kanal: product_not_entitled, pa "cp_missing"
    r = client.post("/v1/messages", json=body, headers={"Idempotency-Key": "i-none"})
    assert r.status_code == 403 and "cp_missing" not in r.text and "never" not in r.text.lower()
    er = client.post(
        "/v1/email/messages",
        json={"owner_ref": "c1", "from_email": FROM, "to": TO, "subject": "s", "text": "b"},
        headers={"Idempotency-Key": "e1"},
    )
    assert er.status_code in (403, 404, 422)  # forma e rrugës mund të ndryshojë; s'është 500
    assert er.status_code != 500


# --- të gjitha rrugët e dërgimit -------------------------------------------------------------------------


def _calls(path: Path, names: set[str]):
    tree = ast.parse(path.read_text())
    out = []
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and getattr(n.func, "id", None) in names:
                    out.append((fn.name, n.lineno))
    return out


def test_message_and_email_rows_are_created_only_inside_the_gated_submit_functions():
    """Pa bypass: i vetmi vend ku krijohet Message/Email është `submit`, dhe `gate` thirret PARA tij."""
    for f in APP.rglob("*.py"):
        rel = f.relative_to(APP).as_posix()
        if rel.startswith("models/"):
            continue
        for name, line in _calls(f, {"Message"}):
            assert (rel, name) == ("services/messages.py", "submit"), (rel, name, line)
        for name, line in _calls(f, {"Email"}):
            assert (rel, name) == ("services/emails.py", "submit"), (rel, name, line)
    for rel, ctor in (("services/messages.py", "Message"), ("services/emails.py", "Email")):
        tree = ast.parse((APP / rel).read_text())
        sub = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "submit")
        gate = [
            n.lineno
            for n in ast.walk(sub)
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "gate"
        ]
        make = [
            n.lineno
            for n in ast.walk(sub)
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == ctor
        ]
        assert gate and make and min(gate) < min(make), rel


def test_every_caller_of_submit_goes_through_the_gated_functions():
    """API direkt, console, fushata (SMS/Email) dhe auto-përgjigjja e inbox thërrasin `submit`."""
    callers = {}
    for f in APP.rglob("*.py"):
        text = f.read_text()
        if "submit(" in text and f.relative_to(APP).as_posix() not in {
            "services/messages.py",
            "services/emails.py",
        }:
            callers[f.relative_to(APP).as_posix()] = text.count("submit(")
    assert set(callers) >= {
        "api/messages.py",
        "api/email.py",
        "services/campaigns.py",
        "services/inbox.py",
    }
    # asnjë modul tjetër nuk fut direkt në radhën e dërgimit pa kaluar nga submit
    for f in APP.rglob("*.py"):
        rel = f.relative_to(APP).as_posix()
        if rel in {
            "services/messages.py",
            "services/emails.py",
            "queue/postgres.py",
            "queue/dispatch.py",
        }:
            continue
        assert "PostgresDispatchQueue(" not in f.read_text() or rel.startswith("queue/"), rel


def test_queued_sms_accepted_before_suspension_is_delivered_like_legacy_kill_switch(
    db, world, fake, mode
):
    """Semantikë e njëjtë me AccountPlan.enabled: kontrolli është te pranimi (submit); mesazhi i pranuar
    (me fonde të bllokuara) dërgohet. Enforce nuk e ndryshon këtë kontratë."""
    eid = eid_of(db)
    mode("enforce")
    put_cp(db, eid, sms="active", email="active")
    assert attempt(db, "sms", "q1") == "ok"
    put_cp(db, eid, sms="suspended", email="active")
    assert attempt(db, "sms", "q2") == "product_suspended"  # i ri refuzohet
    assert svc.process_one(db) is not None  # i pranuari dërgohet
    assert db.query(Message).count() == 1


def test_sms_campaign_is_blocked_by_enforcement_and_paused_with_the_public_code(db, world, mode):
    eid = eid_of(db)
    lst, _ = audience(db, 3)
    c = campaign(db, lst)
    mode("enforce")
    put_cp(db, eid, sms="suspended", email="active")
    start(db, c)
    drive(db)
    db.refresh(c)
    assert c.status == CampaignStatus.PAUSED and c.pause_reason == "product_suspended"
    assert db.query(Message).count() == 0  # asnjë mesazh: s'ka bypass nga worker-i i fushatave
    put_cp(db, eid, sms="active", email="active")
    camp.resume(db, "c1", c.id)
    db.commit()
    drive(db)
    assert db.query(Message).count() == 3


def test_email_campaign_is_blocked_by_enforcement(db, world, verified, mode):
    eid = eid_of(db)
    lst = contacts_svc.create_list(db, "c1", "mail-list")
    ids = []
    for i in range(2):
        c_, _ = contacts_svc.upsert(db, "c1", email=f"p{i}@customer.org", first_name="N")
        consent.record(db, "c1", "email", c_.email, "opt_in", "x", "form", "u", "evidence")
        ids.append(c_.id)
    contacts_svc.add_members(db, "c1", lst.id, ids)
    db.commit()
    c = camp.create(db, "c1", "mail", lst.id, sender="", created_by="t", channel="email",
                    subject="Hi", text="Body", from_email=FROM)  # fmt: skip
    db.commit()
    mode("enforce")
    put_cp(db, eid, sms="active", email="suspended")
    start(db, c)
    drive(db)
    db.refresh(c)
    assert c.status == CampaignStatus.PAUSED and c.pause_reason == "product_suspended"
    from app.models.email import Email

    assert db.query(Email).count() == 0


def test_queued_email_accepted_before_suspension_is_still_processed(db, world, verified, mode):
    eid = eid_of(db)
    mode("enforce")
    put_cp(db, eid, sms="active", email="active")
    assert attempt(db, "email", "q1") == "ok"
    put_cp(db, eid, sms="active", email="suspended")
    assert attempt(db, "email", "q2") == "product_suspended"
    assert emails.process_one(db) is not None


# --- rollback: enforce → shadow/off pa ndryshim DB -------------------------------------------------------


@BOTH
def test_rollback_to_shadow_or_off_restores_legacy_behavior_without_db_changes(
    db, world, verified, mode, ch
):
    eid = eid_of(db)
    put_cp(db, eid, ent_status="suspended", sms="suspended", email="suspended")
    before = (
        [
            (c.name, getattr(db.scalar(select(AccountPlan)), c.name))
            for c in AccountPlan.__table__.columns
        ],
        [(e.status, e.revision, e.rate_limit_per_min) for e in db.scalars(select(Entitlement))],
    )
    mode("enforce")
    assert attempt(db, ch, "a") == "enterprise_suspended"
    mode("shadow")
    assert attempt(db, ch, "b") == "ok"  # vetëm konfigurim: sjellja legacy
    mode("off")
    assert attempt(db, ch, "c") == "ok"
    mode("enforce")
    assert attempt(db, ch, "d") == "enterprise_suspended"  # kthim te enforce: pa gjendje të ngecur
    db.expire_all()
    after = (
        [
            (c.name, getattr(db.scalar(select(AccountPlan)), c.name))
            for c in AccountPlan.__table__.columns
        ],
        [(e.status, e.revision, e.rate_limit_per_min) for e in db.scalars(select(Entitlement))],
    )
    assert before == after  # AccountPlan dhe entitlement-et e paprekura nga ndërrimi i mode


def test_enforce_never_writes_accountplan_and_break_glass_is_not_overridden_by_cp(
    db, world, verified, mode
):
    eid = eid_of(db)
    put_cp(db, eid, sms="active", email="active")
    mode("enforce")
    disable_legacy(db)  # break-glass lokal
    assert attempt(db, "sms") == "account_disabled" and attempt(db, "email") == "account_disabled"
    src = (APP / "services/entitlements.py").read_text()
    assert (
        "AccountPlan.enabled =" not in src and ".enabled =" not in src
    )  # CP kurrë s'e vendos true


def test_gate_fails_closed_in_enforce_on_lookup_error(db, world, verified, mode, monkeypatch):
    mode("enforce")
    put_cp(db, eid_of(db), sms="active", email="active")
    monkeypatch.setattr(
        shadow, "load_state", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db"))
    )
    assert attempt(db, "sms") == "product_not_entitled"  # pa fallback te legacy allow


# --- konfigurim, health, gatishmëria --------------------------------------------------------------------


def test_default_mode_is_off_and_production_enforce_needs_explicit_readiness_ack():
    assert Settings(_env_file=None).cp_sync_mode == "off"
    base = dict(
        cp_sync_mode="enforce", cp_base_url="https://c.example", env="production", _env_file=None
    )
    assert any("READINESS_ACK" in p for p in Settings(**base).production_problems())
    assert not any(
        "READINESS_ACK" in p
        for p in Settings(**base, cp_enforce_readiness_ack=True).production_problems()
    )
    assert not any(
        "READINESS" in p
        for p in Settings(
            cp_sync_mode="shadow", env="production", _env_file=None
        ).production_problems()
    )


def test_sync_health_levels_do_not_deny_anything():
    h = entitlements.sync_health
    assert (h(0), h(299), h(301), h(899), h(901), h(None)) == (
        "healthy", "healthy", "warning", "warning", "critical", "critical")  # fmt: skip


def seed_ready(db, world):
    eid = eid_of(db)
    put_cp(db, eid, sms="active", email="active")
    cur = cps.get_cursor(db)
    cur.epoch, cur.authorization_generation, cur.last_snapshot_at = (
        uuid.uuid4(),
        1,
        datetime.now(UTC),
    )
    db.commit()
    return eid


def test_readiness_requires_snapshot_consistency_and_accepts_explicit_exceptions(db, world):
    r = entitlements.can_enable_enforce(db)
    assert not r.ok and "no snapshot" in r.problems[0]
    eid = seed_ready(db, world)
    r = entitlements.can_enable_enforce(db)
    assert r.ok and r.sync_health == "healthy" and r.problems == [] and r.mismatches == {}
    put_cp(db, eid, sms="suspended", email="active")  # legacy allow / CP deny
    r = entitlements.can_enable_enforce(db)
    assert not r.ok and r.mismatches == {"sms": {"legacy_allow_cp_deny": 1}}
    r = entitlements.can_enable_enforce(db, {(eid, "sms")})
    assert r.ok and r.accepted == 1  # pranuar eksplicit nga operatori
    put_cp(db, eid, sms="active", email=None)  # email mungon ⇒ cp_missing
    assert entitlements.can_enable_enforce(db).mismatches == {"email": {"cp_missing": 1}}
    put_cp(db, eid, sms="withdrawn", email="active")
    assert entitlements.can_enable_enforce(db).mismatches == {"sms": {"cp_withdrawn": 1}}


def test_readiness_flags_never_synced_and_critical_stale_and_missing_last_success(db, world):
    eid = seed_ready(db, world)
    e = db.get(Enterprise, eid)
    e.cp_revision = 0
    db.commit()
    r = entitlements.can_enable_enforce(db)
    assert not r.ok and r.never_synced_enterprises == 1
    put_cp(db, eid, sms="active", email="active", last_ok=datetime.now(UTC) - timedelta(hours=2))
    r = entitlements.can_enable_enforce(db)
    assert not r.ok and r.sync_health == "critical" and any("stale" in p for p in r.problems)
    cur = cps.get_cursor(db)
    cur.last_success_at = None
    db.commit()
    assert any("last_success_at" in p for p in entitlements.can_enable_enforce(db).problems)


def test_readiness_script_exit_codes_and_never_changes_config(db, world, tmp_path, capsys):
    from scripts import cp_enforce_readiness as script

    assert script.main([]) == 1  # pa snapshot
    eid = seed_ready(db, world)
    assert script.main([]) == 0
    put_cp(db, eid, sms="suspended", email="active")
    assert script.main([]) == 1
    f = tmp_path / "ok.json"
    f.write_text('{"exceptions": [{"enterprise_id": "' + str(eid) + '", "channel": "sms"}]}')
    assert script.main(["--exceptions", str(f)]) == 0
    f.write_text('{"exceptions": [{"enterprise_id": "x", "channel": "voice"}]}')
    assert script.main(["--exceptions", str(f)]) == 2
    assert settings.cp_sync_mode == "off"
    capsys.readouterr()


# --- performanca: off / shadow / enforce, SMS dhe Email ----------------------------------------------------


def _round(db, ch, label, n):
    t = time.perf_counter()
    for i in range(n):
        assert attempt(db, ch, f"{label}-{i}") == "ok"
    return (time.perf_counter() - t) / n * 1000


@BOTH
def test_performance_budget_off_shadow_enforce(db, world, verified, monkeypatch, ch):
    """Raunde të ndërthurura (off/shadow/enforce) për të ulur zhurmën e makinës; minimumi për mode."""
    from app.models.wallet import Wallet
    from app.services import wallet as wallets

    w = db.scalar(select(Wallet))
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "10000", wallets.TopupMethod.CASH).id)
    put_cp(db, eid_of(db), sms=("active", 100000), email=("active", 100000))
    plan = db.scalar(select(AccountPlan))
    plan.rate_limit_per_min = plan.email_rate_limit_per_min = 100000
    db.commit()
    sql = {"n": 0}

    def count(*a):
        sql["n"] += 1

    event.listen(engine, "before_cursor_execute", count)
    best: dict[str, float] = {}
    per: dict[str, float] = {}
    n = 25
    try:
        for r in range(6):
            order = ("off", "shadow", "enforce")
            for m in (
                order[r % 3 :] + order[: r % 3]
            ):  # rrotullim: numëruesi i minutës rritet me raundet
                monkeypatch.setattr(settings, "cp_sync_mode", m)
                if r == 0:
                    shadow.clear_cache()
                    _round(db, ch, f"warm-{m}", 3)  # ngroh cache-n (1 SELECT CP në nisje)
                sql["n"] = 0
                ms = _round(db, ch, f"{ch}-{m}-{r}", n)
                best[m] = min(best.get(m, 1e9), ms)
                per[m] = sql["n"] / n
    finally:
        event.remove(engine, "before_cursor_execute", count)
    line = f"\n[perf {ch}] off {best['off']:.2f} ms/{per['off']:.1f} sql"
    for m in ("shadow", "enforce"):
        line += (
            f" · {m} {best[m]:.2f} ms/{per[m]:.1f} sql ({(best[m] / best['off'] - 1) * 100:+.1f}%)"
        )
        assert (
            per[m] - per["off"] <= 1.0
        )  # buxheti SQL: cache ⇒ ≈0 shtesë në gjendje të qëndrueshme
        assert best[m] <= best["off"] * 1.05 + 0.3  # ≤5% (+ tolerancë zhurme 0.3 ms)
    print(line)
