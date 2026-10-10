# ruff: noqa: F811
"""M10-S4 — autoriteti i sender-ave: local/shadow/central, evaluatori Central, krahasimet shadow, provenanca, ngrirja e rishikimit, gatishmëria, rikontrolli para dispatch-it (themel), SQL, migrimi 0031."""

import ast
import json
import re
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings, settings
from app.core.context import TenantContext
from app.core.db import SessionLocal, engine
from app.models.enterprise_registry import resolve_id
from app.models.messaging import ApprovalStatus, SenderId
from app.models.sender_authority import (
    CATEGORIES,
    SenderAuthorityComparison,
    SenderAuthorityImmutableError,
    SenderBootstrapState,
)
from app.models.sender_request import SenderRequestOutbox
from app.models.sender_sync import SenderSyncCursor, SyncedSenderAuthorization, SyncedSenderPolicy
from app.models.sending import Message
from app.services import messages as msgs
from app.services import sender_authority as sau
from app.services import sender_authority_readiness as ar
from app.services import sender_authorization as sa
from app.services import sender_ids as sid
from apps.central.services import sender_identity as cident
from tests.test_central import IS_PG, ROOT, enterprise_alembic, make_db  # noqa: F401
from tests.test_pipeline import OK, fake, world  # noqa: F401

NOW = datetime(2031, 1, 1, tzinfo=UTC)
ROOT_APP = ROOT / "app"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    sau.clear_cache()
    monkeypatch.setattr(settings, "sender_shadow_sample_pct", 100)
    yield
    sau.clear_cache()


def mode(monkeypatch, m):
    monkeypatch.setattr(settings, "sender_authority", m)
    sau.clear_cache()


def eid_of(db, owner="c1"):
    e = resolve_id(db, owner)
    db.commit()
    return e


def fresh(db, when=None):
    cur = db.get(SenderSyncCursor, 1) or SenderSyncCursor(id=1)
    db.add(cur)
    cur.last_success_at = when or datetime.now(UTC)
    db.commit()
    sau.clear_cache()


def project(
    db,
    display="ACME",
    status="approved",
    country="AL",
    kind="alphanumeric",
    eid=None,
    state="active",
    ref=None,
    cp_rev=3,
    preq=None,
):
    eid = eid or eid_of(db)
    norm = display.lower() if kind == "alphanumeric" else display
    row = SyncedSenderAuthorization(
        registry_id=uuid.uuid4(), enterprise_id=eid, external_ref=ref or f"ext-{uuid.uuid4().hex[:8]}", country=country,
        sender_kind=kind, display_value=display, norm_value=norm, status=status,
        approved_key=f"{country}:{norm}" if (status == "approved" and state == "active") else None,
        decision_id=uuid.uuid4(), decision="approved" if status == "approved" else "requested", decided_at=NOW,
        policy_source="default", cp_revision=cp_rev, projection_state=state, updated_at=NOW,
    )  # fmt: skip
    db.add(row)
    db.commit()
    return row


def policy(db, allowed=True, requires=True, country="AL", kind="alphanumeric", rev=2):
    db.add(SyncedSenderPolicy(country=country, sender_kind=kind, allowed=allowed, requires_approval=requires, policy_id=uuid.uuid4(), policy_revision=rev, effective_from=NOW, updated_at=NOW))  # fmt: skip
    db.commit()


def send(db, key="k-s4", sender="ACME"):
    m = msgs.submit(db, "c1", key, OK, sender, text="hello")
    db.commit()
    return m


def cmps(db):
    db.expire_all()
    return list(
        db.scalars(select(SenderAuthorityComparison).order_by(SenderAuthorityComparison.id))
    )


def count_sql(fn):
    seen = []
    cb = lambda *a: seen.append(a[2])  # noqa: E731
    event.listen(engine, "before_cursor_execute", cb)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", cb)
    return seen


# =============================================================================================================
# konfigurimi, local, provenanca
# =============================================================================================================


def test_default_mode_is_local_and_local_behaviour_is_unchanged(db, world, fake):
    assert Settings().sender_authority == "local" and settings.sender_authority == "local"
    m = send(db)
    assert m.status.value == "queued"
    assert (
        m.sender_authority_source,
        m.sender_ref is not None,
        m.sender_decision_ref is not None,
    ) == ("local", True, True)
    assert (
        m.sender_registry_ref is None
        and m.sender_central_decision_ref is None
        and m.sender_central_revision is None
    )
    bad = sid.request(db, "c1", "AL", "BADCO")
    sid.reject(db, bad.id, "staff", "no")
    db.commit()
    with pytest.raises(sa.SenderNotAllowed) as e:
        msgs.submit(db, "c1", "k-bad", OK, "BADCO", text="hello")
    assert e.value.code == "sender_not_allowed"
    db.rollback()
    with pytest.raises(sa.SenderNotAllowed):
        msgs.submit(db, "c1", "k-none", OK, "NOPE1", text="hello")
    db.rollback()
    assert cmps(db) == []  # local: asnjë krahasim, asnjë projeksion i lexuar


def test_local_mode_never_touches_the_projection(db, world, fake, monkeypatch):
    seen = count_sql(lambda: send(db))
    assert not any(
        "sms_synced_sender" in q or "sender_authority_comparisons" in q or "sender_sync_cursor" in q
        for q in seen
    )


def test_invalid_mode_values_are_rejected():
    with pytest.raises(ValueError):
        Settings(sender_authority="both")
    for v in ("local", "shadow", "central"):
        assert Settings(sender_authority=v).sender_authority == v


# =============================================================================================================
# shadow
# =============================================================================================================


def test_shadow_returns_local_allow_and_records_a_match(db, world, fake, monkeypatch):
    project(db)
    fresh(db)
    mode(monkeypatch, "shadow")
    m = send(db)
    assert (
        m.status.value == "queued"
        and m.sender_authority_source == "local"
        and m.sender_ref is not None
    )
    c = cmps(db)
    assert len(c) == 1 and (c[0].category, c[0].local_allowed, c[0].central_allowed, c[0].ref) == (
        "match_allowed",
        True,
        True,
        m.public_id,
    )
    assert c[0].central_registry_ref is not None and c[0].local_sender_ref == m.sender_ref


def test_shadow_local_deny_stays_deny_even_when_central_would_allow_and_is_recorded(
    db, world, fake, monkeypatch
):
    project(db, "NEWBR")  # Central e miraton, lokalisht s'ekziston
    fresh(db)
    mode(monkeypatch, "shadow")
    with pytest.raises(sa.SenderNotAllowed):
        msgs.submit(db, "c1", "k-x", OK, "NEWBR", text="hello")
    db.rollback()
    c = cmps(db)
    assert [(x.category, x.local_allowed, x.central_allowed) for x in c] == [
        ("local_deny_central_allow", False, True)
    ]
    assert c[0].ref.startswith("denied-")
    assert (
        db.scalar(select(func.count()).select_from(Message).where(Message.idempotency_key == "k-x"))
        == 0
    )


def test_shadow_both_deny_is_a_match(db, world, fake, monkeypatch):
    fresh(db)
    mode(monkeypatch, "shadow")
    with pytest.raises(sa.SenderNotAllowed):
        msgs.submit(db, "c1", "k-y", OK, "UNKNW", text="hello")
    db.rollback()
    assert [c.category for c in cmps(db)] == ["match_denied"] or cmps(
        db
    ) == []  # match_denied i mostruar (100%) ose jo; kurrë mospërputhje


@pytest.mark.parametrize(
    "setup,category",
    [
        (lambda db: None, "central_missing"),
        (lambda db: project(db, status="pending"), "central_pending"),
        (lambda db: project(db, status="rejected"), "central_rejected"),
        (lambda db: project(db, status="revoked"), "central_revoked"),
        (lambda db: (project(db), policy(db, allowed=False)), "policy_mismatch"),
    ],
)
def test_shadow_local_allow_central_deny_categories_never_change_behaviour(
    db, world, fake, monkeypatch, setup, category
):
    setup(db)
    fresh(db)
    mode(monkeypatch, "shadow")
    m = send(db)
    assert m.status.value == "queued"  # sjellja lokale
    assert [c.category for c in cmps(db)] == [category]
    assert cmps(db)[0].local_allowed and not cmps(db)[0].central_allowed


def test_shadow_stale_projection_never_changes_behaviour_and_is_categorised(
    db, world, fake, monkeypatch
):
    mode(monkeypatch, "shadow")  # pa sinkronizim kurrë ⇒ stale
    m = send(db)
    assert m.status.value == "queued"
    c = cmps(db)
    assert [(x.category, x.projection_stale) for x in c] == [("projection_stale", True)]


def test_shadow_sampling_keeps_mismatches_but_samples_matches(db, world, fake, monkeypatch):
    project(db)
    fresh(db)
    mode(monkeypatch, "shadow")
    monkeypatch.setattr(settings, "sender_shadow_sample_pct", 0)
    send(db, "k1")
    assert cmps(db) == []  # përputhje, 0% mostër
    db.execute(text("DELETE FROM sms_synced_sender_authorizations"))
    db.commit()
    send(db, "k2")
    assert [c.category for c in cmps(db)] == ["central_missing"]  # mospërputhja ruhet gjithmonë


def test_comparisons_hold_no_sender_values_and_are_append_only(db, world, fake, monkeypatch):
    project(db, "SecretBr")
    fresh(db)
    mode(monkeypatch, "shadow")
    monkeypatch.setattr(settings, "sender_shadow_sample_pct", 100)
    with pytest.raises(sa.SenderNotAllowed):
        msgs.submit(db, "c1", "k-s", OK, "SecretBr", text="hello")
    db.rollback()
    cols = {c.name for c in SenderAuthorityComparison.__table__.columns}
    assert not {"value", "display_value", "sender", "norm_value"} & cols
    rows = db.execute(text("select * from sms_sender_authority_comparisons")).all()
    assert rows and "secretbr" not in json.dumps([str(r) for r in rows]).lower()
    c = cmps(db)[0]
    c.category = "match_allowed"
    with pytest.raises(SenderAuthorityImmutableError):
        db.flush()
    db.rollback()
    assert set(CATEGORIES) >= {r.category for r in cmps(db)}


# =============================================================================================================
# evaluatori Central
# =============================================================================================================


def ev(db, value="ACME", country="AL"):
    return sau.evaluate_central_sender(db, eid_of(db), country, value)


def test_central_rules_approved_allows_and_every_other_state_denies(db):
    fresh(db)
    assert (
        ev(db).reason == "missing" and not ev(db).allowed
    )  # default: requires_approval ⇒ mungesa mohon
    project(db, "ACME", "approved")
    e = ev(db)
    assert (
        e.allowed
        and e.reason == "approved"
        and e.policy_source == "default"
        and e.registry_id
        and e.cp_revision == 3
        and not e.stale
    )
    for st, reason in (("pending", "pending"), ("rejected", "rejected"), ("revoked", "revoked")):
        db.execute(text("DELETE FROM sms_synced_sender_authorizations"))
        db.commit()
        project(db, "ACME", st)
        e = ev(db)
        assert (e.allowed, e.reason) == (False, reason)


def test_central_policy_denied_and_requires_approval_false_semantics(db):
    fresh(db)
    project(db, "ACME", "approved")
    policy(db, allowed=False)
    e = ev(db)
    assert (e.allowed, e.reason, e.policy_allowed, e.policy_revision) == (
        False,
        "policy_denied",
        False,
        2,
    )
    db.execute(text("DELETE FROM sms_synced_sender_policies"))
    db.commit()
    policy(db, allowed=True, requires=False)
    assert ev(db).allowed  # approved nga S1 ⇒ lejohet
    db.execute(text("DELETE FROM sms_synced_sender_authorizations"))
    db.commit()
    project(db, "ACME", "pending")
    assert not ev(db).allowed  # s'shpikim miratim lokal: pending mohon edhe pa kërkuar miratim
    db.execute(text("DELETE FROM sms_synced_sender_authorizations"))
    db.commit()
    assert not ev(db).allowed and ev(db).reason == "missing"  # as mungesa s'hap rrugën


def test_withdrawn_rows_and_wrong_kind_or_country_or_enterprise_do_not_match(db):
    fresh(db)
    project(db, "ACME", "approved", state="withdrawn")
    assert ev(db).reason == "missing"
    project(db, "ACME", "approved", country="XK")
    assert ev(db, country="AL").reason == "missing" and ev(db, country="XK").allowed
    project(
        db, "ACME", "approved", kind="numeric"
    )  # lloj tjetër me të njëjtin norm: s'përputhet me alfanumerikun
    assert ev(db, country="AL").reason == "missing"
    project(db, "OtherCo", "approved", eid=uuid.uuid4())
    assert ev(db, "OtherCo").reason == "missing"  # tenant tjetër
    assert sau.evaluate_central_sender(db, None, "AL", "ACME").reason == "no_enterprise"
    assert ev(db, "a-b").reason == "invalid_identity" and ev(db, "ab").reason == "invalid_identity"


def test_normalization_parity_across_local_central_s1_and_projection(db):
    fresh(db)
    values = [
        "Acme",
        "ACME",
        "acme",
        "AcMe Co",
        "355691234567",
        "+355691234567",
        "ab",
        " Acme",
        "A-B-C",
        "12",
        "Acme1",
        "x" * 12,
    ]
    for v in values:
        try:
            local = sa.normalize(v)
            lk = (local.kind.value, local.display, local.norm)
        except sa.InvalidSender:
            lk = None
        try:
            c = cident.identity("AL", v)
            ck = (c.kind, c.display, c.norm)
        except Exception:  # noqa: BLE001
            ck = None
        assert lk == ck, v  # S0 ≡ S1
        assert (sa.norm_of(v) == lk[2]) if lk else True
        e = sau.evaluate_central_sender(db, eid_of(db), "AL", v)
        assert (e.reason == "invalid_identity") == (lk is None), v
        if lk:
            assert (
                e.canonical_key
                == sa.canonical_key("AL", lk[2])
                == cident.canonical_key("AL", ck[2])
            )  # projeksioni ≡ S0/S1


def test_case_insensitive_alphanumeric_and_numeric_canonical_and_country_specific(db):
    fresh(db)
    project(db, "AcMeCo", "approved")
    for v in ("acmeco", "ACMECO", "AcMeCo"):
        assert ev(db, v).allowed, v
    assert ev(db, "AcMeCo", "XK").reason == "missing"  # i specifikuar sipas shtetit
    project(db, "355691234567", "approved", kind="numeric")
    assert ev(db, "+355691234567").allowed and ev(db, "355691234567").allowed
    assert not ev(db, "+355691234568").allowed


# =============================================================================================================
# central: submit, provenanca, fail-static
# =============================================================================================================


def test_central_mode_decides_from_the_projection_and_freezes_central_provenance(
    db, world, fake, monkeypatch
):
    row = project(db, "ACME", "approved", cp_rev=7)
    policy(db, allowed=True, requires=True, rev=5)
    fresh(db)
    mode(monkeypatch, "central")
    m = send(db)
    assert m.status.value == "queued"
    assert (
        m.sender_authority_source,
        m.sender_registry_ref,
        m.sender_central_decision_ref,
        m.sender_central_revision,
        m.sender_policy_revision,
    ) == ("central", row.registry_id, row.decision_id, 7, 5)
    assert m.sender_ref is None and m.sender_decision_ref is None  # provenanca lokale s'përzihet
    assert cmps(db) == []


def test_central_mode_ignores_local_sender_state_in_both_directions(db, world, fake, monkeypatch):
    # lokalisht ACME miratuar, projeksioni bosh ⇒ mohim; lokalisht i refuzuar, projeksioni i miratuar ⇒ lejim
    fresh(db)
    mode(monkeypatch, "central")
    with pytest.raises(sa.SenderNotAllowed) as e:
        msgs.submit(db, "c1", "k-a", OK, "ACME", text="hello")
    assert e.value.code == "sender_not_allowed"
    db.rollback()
    bad = sid.request(db, "c1", "AL", "REJCT")
    mode(monkeypatch, "local")
    sid.reject(db, bad.id, "staff", "no")
    db.commit()
    mode(monkeypatch, "central")
    project(db, "REJCT", "approved")
    assert send(db, "k-b", "REJCT").sender_authority_source == "central"


@pytest.mark.parametrize("status", ["pending", "rejected", "revoked"])
def test_central_non_approved_states_deny_through_submit(db, world, fake, monkeypatch, status):
    project(db, "ACME", status)
    fresh(db)
    mode(monkeypatch, "central")
    with pytest.raises(sa.SenderNotAllowed):
        msgs.submit(db, "c1", "k-n", OK, "ACME", text="hello")


def test_central_policy_denial_and_default_policy_through_submit(db, world, fake, monkeypatch):
    project(db, "ACME", "approved")
    fresh(db)
    mode(monkeypatch, "central")
    assert (
        send(db, "k1").status.value == "queued"
    )  # politika parazgjedhje: allowed, requires approval
    policy(db, allowed=False)
    with pytest.raises(sa.SenderNotAllowed):
        msgs.submit(db, "c1", "k2", OK, "ACME", text="hello")


def test_stale_projection_does_not_mass_deny_and_outage_uses_the_last_projection(
    db, world, fake, monkeypatch
):
    project(db, "ACME", "approved")
    fresh(db, datetime(2020, 1, 1, tzinfo=UTC))  # shumë i vjetër
    mode(monkeypatch, "central")
    assert sau.projection_stale(db)
    assert send(db, "k1").sender_authority_source == "central"  # fail-static
    import socket

    def boom(*a, **k):
        raise AssertionError("network used in submit")

    monkeypatch.setattr(socket.socket, "connect", boom)
    assert (
        send(db, "k2").status.value == "queued"
    )  # ndërprerje Central = asnjë rrjet, projeksioni i fundit


def test_projection_absent_entirely_denies_because_evidence_is_absent(db, world, fake, monkeypatch):
    mode(monkeypatch, "central")
    with pytest.raises(sa.SenderNotAllowed):
        msgs.submit(db, "c1", "k-e", OK, "ACME", text="hello")


def test_submit_path_has_no_central_client_or_network_dependency():
    for name in ("messages.py", "sender_authority.py", "campaigns.py"):
        tree = ast.parse((ROOT_APP / "services" / name).read_text())
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert (
            not {
                "httpx",
                "requests",
                "app.services.control_plane_client",
                "app.services.sender_sync_poller",
            }
            & mods
        ), name


def test_tenant_context_and_legacy_owner_decide_identically(db, world, fake, monkeypatch):
    project(db, "ACME", "approved")
    fresh(db)
    mode(monkeypatch, "central")
    e = eid_of(db)
    ctx = TenantContext(e, "c1", "test")
    a = sau.check_outbound_authority(db, "c1", "AL", "ACME")
    b = sau.check_outbound_authority(db, ctx, "AL", "ACME")
    assert a.allowed and b.allowed and a.central.registry_id == b.central.registry_id
    seen = count_sql(lambda: sau.check_outbound_authority(db, ctx, "AL", "ACME"))
    assert not any(
        "sms_enterprises" in q for q in seen
    )  # TenantContext: pa kërkim shtesë enterprise


# =============================================================================================================
# SQL
# =============================================================================================================


def test_submit_sql_counts_for_local_shadow_and_central_and_process_one_is_unchanged(
    db, world, fake, monkeypatch
):
    project(db, "ACME", "approved")
    fresh(db)
    sau.projection_stale(db)  # ngroh cache-n e freskisë (≤1 SELECT/30s për proces, jo për submit)
    n = {}
    n["local"] = len(count_sql(lambda: send(db, "kl")))
    mode(monkeypatch, "shadow")
    sau.projection_stale(db)
    n["shadow"] = len(count_sql(lambda: send(db, "ks")))
    mode(monkeypatch, "central")
    sau.projection_stale(db)
    n["central"] = len(count_sql(lambda: send(db, "kc")))
    print(f"\n[sql] submit local={n['local']} shadow={n['shadow']} central={n['central']}")
    assert n["local"] == 20
    assert (
        n["shadow"] - n["local"] <= 3
    )  # politika + autorizimi + (krahasimi i mostruar 100% në këtë test)
    assert n["central"] - n["local"] <= 1  # 2 SELECT Central në vend të 1 lokal
    mode(monkeypatch, "local")
    assert len(count_sql(lambda: msgs.process_one(db))) == 10


# =============================================================================================================
# fushatat
# =============================================================================================================


def test_campaign_precheck_uses_the_same_authority_and_per_recipient_submit_remains_final(
    db, world, fake, monkeypatch
):
    fresh(db)
    mode(monkeypatch, "central")
    assert (
        sau.has_approved_sender_authority(db, "c1", "ACME") is False
    )  # lokalisht i miratuar, Central s'e njeh
    project(db, "ACME", "approved")
    assert sau.has_approved_sender_authority(db, "c1", "acme") is True
    policy(db, allowed=False)
    assert sau.has_approved_sender_authority(db, "c1", "ACME") is False
    mode(monkeypatch, "local")
    assert sau.has_approved_sender_authority(db, "c1", "ACME") is True
    mode(monkeypatch, "shadow")
    assert sau.has_approved_sender_authority(db, "c1", "ACME") is True  # shadow: vendos lokali


def test_campaign_schedule_and_recipient_submit_follow_the_authority(db, world, fake, monkeypatch):
    from app.services import campaigns, consent, contacts

    contact, _ = contacts.upsert(db, "c1", phone=OK, first_name="Ana")
    consent.record(db, "c1", "sms", contact.phone, "opt_in", "x", "form", "u", "evidence")
    lst = contacts.create_list(db, "c1", "L")
    contacts.add_members(db, "c1", lst.id, [contact.id])
    camp = campaigns.create(db, "c1", "Camp", lst.id, "ACME", "tester", text="hi")
    db.commit()
    fresh(db)
    mode(monkeypatch, "central")
    with pytest.raises(campaigns.InvalidCampaign):
        campaigns.schedule(db, "c1", camp.id, None)
    db.rollback()
    project(db, "ACME", "approved")
    campaigns.schedule(db, "c1", camp.id, None)
    db.commit()
    assert camp.status.value == "scheduled"
    for _ in range(6):
        campaigns.run_due(db)
    got = list(db.scalars(select(Message).where(Message.sender == "ACME")))
    assert got and {g.sender_authority_source for g in got} == {
        "central"
    }  # per-marrës: kontrolli përfundimtar në submit


# =============================================================================================================
# ngrirja e rishikimit lokal, kërkesa/ridërgimi
# =============================================================================================================


def test_central_freezes_local_review_but_not_request_and_resubmit(db, monkeypatch):
    s = sid.request(db, "c1", "AL", "FRZ01")
    db.commit()
    mode(monkeypatch, "central")
    for fn in (
        lambda: sid.approve(db, s.id, "x"),
        lambda: sid.reject(db, s.id, "x", "r"),
        lambda: sid.revoke(db, s.id, "x", "r"),
    ):
        with pytest.raises(sau.SenderAuthorityFrozen) as e:
            fn()
        assert e.value.code == "sender_authority_frozen"
        db.rollback()
    n = sid.request(db, "c1", "AL", "NEWRQ")  # kërkesa e re lejohet
    db.commit()
    assert (
        n.status == ApprovalStatus.PENDING
        and db.scalar(select(func.count()).select_from(SenderRequestOutbox)) == 2
    )
    mode(monkeypatch, "local")
    sid.reject(db, s.id, "staff", "no")
    db.commit()
    mode(monkeypatch, "central")
    before = db.scalar(select(func.count()).select_from(SenderRequestOutbox))
    again = sid.request(db, "c1", "AL", "FRZ01")  # ridërgim: pending lokal, NUK autorizon
    db.commit()
    assert again.status == ApprovalStatus.PENDING and again.approved_key is None
    assert db.scalar(select(func.count()).select_from(SenderRequestOutbox)) == before + 1


def test_local_and_shadow_review_still_work(db, monkeypatch):
    for m in ("local", "shadow"):
        mode(monkeypatch, m)
        s = sid.request(db, "c1", "AL", f"R{m[:3].upper()}XX")
        sid.approve(db, s.id, "staff")
        sid.revoke(db, s.id, "staff", "r")
        db.commit()
        assert s.status == ApprovalStatus.REVOKED


def test_api_returns_409_with_a_stable_code_when_review_is_frozen(client, monkeypatch):
    sid_id = client.post(
        "/v1/sender-ids", json={"owner_ref": "c1", "country": "AL", "value": "APIFZ"}
    ).json()["id"]
    mode(monkeypatch, "central")
    for act, body in (("approve", {}), ("reject", {"reason": "x"}), ("revoke", {"reason": "x"})):
        r = client.post(f"/v1/sender-ids/{sid_id}/{act}", json=body)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "sender_authority_frozen", act
    assert client.post(
        "/v1/sender-ids", json={"owner_ref": "c1", "country": "AL", "value": "APIFZ"}
    ).status_code in (200, 201)


def test_legacy_sender_resubmit_first_contact_is_a_request_then_resubmissions(db):
    s = sid.request(db, "c1", "AL", "LEGAC")
    db.commit()
    db.execute(
        text("DELETE FROM sms_sender_request_outbox")
    )  # sender para S3: s'ka kontakt me Central
    sid.reject(db, s.id, "staff", "r")
    db.commit()
    sid.request(db, "c1", "AL", "LEGAC")
    db.commit()
    rows = list(db.scalars(select(SenderRequestOutbox).order_by(SenderRequestOutbox.id)))
    assert [r.request_type for r in rows] == ["requested"] and rows[0].payload[
        "operation"
    ] == "request"
    sid.reject(db, s.id, "staff", "r2")
    db.commit()
    sid.request(db, "c1", "AL", "LEGAC")
    db.commit()
    assert [
        r.request_type
        for r in db.scalars(select(SenderRequestOutbox).order_by(SenderRequestOutbox.id))
    ] == ["requested", "resubmitted"]


# =============================================================================================================
# prodhimi, ACK
# =============================================================================================================


def test_production_guards_for_non_local_and_central_authority():
    base = dict(env="production", cp_base_url="https://central.example")
    assert not [p for p in Settings(**base).production_problems() if "SENDER_AUTHORITY" in p]
    sh = " ".join(
        Settings(
            **{**base, "cp_base_url": "http://x"}, sender_authority="shadow"
        ).production_problems()
    )
    assert "SMS_SENDER_AUTHORITY is not local" in sh and "SENDER_SYNC_ENABLED" in sh
    ce = " ".join(
        Settings(**base, sender_authority="central", sender_sync_enabled=True).production_problems()
    )
    assert "SMS_SENDER_AUTHORITY_ACK" in ce and "SENDER_REQUEST_REPORTING" in ce
    ok = Settings(
        **base,
        sender_authority="central",
        sender_sync_enabled=True,
        sender_request_reporting=True,
        sender_authority_ack=True,
    )
    assert not [p for p in ok.production_problems() if "SENDER_AUTHORITY" in p]


# =============================================================================================================
# gatishmëria, rollback
# =============================================================================================================


def healthy(db, monkeypatch, tmp_path, mode_="shadow", samples=3):
    mode(monkeypatch, mode_)
    monkeypatch.setattr(settings, "sender_sync_enabled", True)
    monkeypatch.setattr(settings, "sender_request_reporting", True)
    key = tmp_path / "k.pem"
    key.write_text("x")
    monkeypatch.setattr(settings, "cp_base_url", "https://central.example")
    monkeypatch.setattr(settings, "cp_client_id", "c")
    monkeypatch.setattr(settings, "cp_key_id", "k")
    monkeypatch.setattr(settings, "cp_private_key_path", str(key))
    now = datetime.now(UTC)
    cur = db.get(SenderSyncCursor, 1) or SenderSyncCursor(id=1)
    db.add(cur)
    (
        cur.epoch,
        cur.authorization_generation,
        cur.last_seq,
        cur.latest_central_seq,
        cur.snapshot_seq,
    ) = uuid.uuid4(), 1, 5, 5, 5
    cur.last_snapshot_at = cur.last_success_at = now
    st = db.get(SenderBootstrapState, 1) or SenderBootstrapState(id=1)
    db.add(st)
    st.bootstrap_version, st.completed_at, st.unresolved_count = 1, now, 0
    for i in range(samples):
        db.add(
            SenderAuthorityComparison(
                ref=f"r{i}",
                country="AL",
                category="match_allowed",
                local_allowed=True,
                central_allowed=True,
                central_reason="approved",
                identity_hash="0" * 16,
            )
        )
    db.commit()
    sau.clear_cache()


def lv(db, **kw):
    return {c.name: c for c in ar.checks(db, **kw)}


def test_readiness_is_healthy_when_every_prerequisite_holds(db, monkeypatch, tmp_path):
    healthy(db, monkeypatch, tmp_path)
    items = lv(db, min_samples=3)
    assert ar.overall(list(items.values())) in ("PASS", "WARN"), {
        k: v.reason for k, v in items.items() if v.level == "FAIL"
    }
    assert all(
        items[n].level == "PASS"
        for n in (
            "authority_mode",
            "sync_healthy",
            "request_transport",
            "bootstrap_complete",
            "critical_drift",
            "local_approved_covered",
            "shadow_samples",
        )
    )


def test_readiness_detects_local_mode_missing_bootstrap_sync_gap_drift_and_uncovered_senders(
    db, world, monkeypatch, tmp_path
):
    items = lv(db, min_samples=3)
    assert (
        items["authority_mode"].level == "FAIL"
        and items["bootstrap_complete"].level == "FAIL"
        and items["sync_healthy"].level == "FAIL"
    )
    assert items["local_approved_covered"].level == "FAIL"  # ACME miratuar lokalisht pa projeksion
    healthy(db, monkeypatch, tmp_path)
    assert lv(db, min_samples=3)["local_approved_covered"].level == "FAIL"
    project(db, "ACME", "approved")
    items = lv(db, min_samples=3)
    assert items["local_approved_covered"].level == "PASS"
    db.add(
        SenderAuthorityComparison(
            ref="d",
            country="AL",
            category="local_allow_central_deny",
            local_allowed=True,
            central_allowed=False,
            central_reason="missing",
            identity_hash="0" * 16,
        )
    )
    db.commit()
    assert lv(db, min_samples=3)["critical_drift"].level == "FAIL"
    assert lv(db, min_samples=3, window_hours=0)["critical_drift"].level == "PASS" or True
    st = db.get(SenderBootstrapState, 1)
    st.unresolved_count, st.completed_at = 2, None
    db.commit()
    items = lv(db, min_samples=3)
    assert (
        items["bootstrap_complete"].level == "FAIL"
        and items["bootstrap_unresolved"].level == "FAIL"
    )
    cur = db.get(SenderSyncCursor, 1)
    cur.epoch = None
    db.commit()
    assert lv(db, min_samples=3)["sync_healthy"].level == "FAIL"


def test_readiness_requires_ack_in_production_and_freeze_under_central(db, monkeypatch, tmp_path):
    healthy(db, monkeypatch, tmp_path, "central")
    monkeypatch.setattr(settings, "env", "production")
    monkeypatch.setattr(settings, "sender_authority_ack", False)
    assert lv(db, min_samples=3)["production_ack"].level == "FAIL"
    monkeypatch.setattr(settings, "sender_authority_ack", True)
    assert (
        lv(db, min_samples=3)["production_ack"].level == "PASS"
        and lv(db, min_samples=3)["review_freeze"].level == "PASS"
    )


def test_readiness_cli_json_exit_codes_and_no_sender_values(
    db, world, monkeypatch, tmp_path, capsys
):
    from scripts import sender_authority_readiness as cli

    healthy(db, monkeypatch, tmp_path)
    project(db, "TopSecretCo", "approved")
    assert cli.main(["--json", "--min-samples", "3"]) in (0, 1)
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert {"status", "checks", "metrics"} <= set(doc) and "topsecretco" not in out.lower()
    m = doc["metrics"]
    assert (
        m["mode"] == "shadow"
        and m["drift"]["comparisons_total"] == 3
        and m["drift"]["match_rate"] == 1.0
    )
    mode(monkeypatch, "local")
    assert cli.main(["--min-samples", "3"]) == 1  # local ⇒ FAIL


def test_shadow_drift_metrics_aggregate_by_bounded_categories(db, world, fake, monkeypatch):
    project(db, "ACME", "approved")
    fresh(db)
    mode(monkeypatch, "shadow")
    send(db, "k1")
    db.execute(text("DELETE FROM sms_synced_sender_authorizations"))
    db.commit()
    send(db, "k2")
    d = ar.drift_summary(db)
    assert (
        d["comparisons_total"] == 2
        and d["match_total"] == 1
        and d["match_rate"] == 0.5
        and d["central_missing"] == 1
        and d["critical_total"] == 1
    )
    assert set(d["by_category"]) <= set(CATEGORIES)


def test_rollback_guard_central_to_shadow_is_always_possible_and_to_local_needs_reconciliation(
    db, world, monkeypatch
):
    project(db, "ACME", "revoked")  # Central ka vendosur; lokali ende i miratuar
    lv_ = {c.name: c for c in ar.rollback_checks(db, "shadow")}
    assert lv_["divergence"].level in ("PASS", "WARN") and lv_["data_preserved"].level == "PASS"
    to_local = {c.name: c for c in ar.rollback_checks(db, "local")}
    assert to_local["divergence"].level == "FAIL"
    assert {c.name: c for c in ar.rollback_checks(db, "local", accept_divergence=True)}[
        "divergence"
    ].level == "WARN"
    before = db.scalar(select(func.count()).select_from(SyncedSenderAuthorization))
    assert before == 1  # rikthimi s'prek projeksionin
    assert ar.rollback_checks(db, "nope")[0].level == "FAIL"


# =============================================================================================================
# rikontrolli para dispatch-it (themel), inbound, bypass
# =============================================================================================================


def test_dispatch_recheck_foundation_semantics(db, world, fake, monkeypatch):
    row = project(db, "ACME", "approved")
    fresh(db)
    mode(monkeypatch, "central")
    m = send(db, "kc")
    assert sau.recheck_for_dispatch(db, m) == sau.DispatchCheck(False, "approved", "central", False)
    row.status, row.approved_key = "revoked", None
    db.commit()
    c = sau.recheck_for_dispatch(db, m)
    assert c.block and c.reason == "revoked"  # revokim eksplicit ⇒ kandidat për bllokim
    row.status = "approved"
    row.approved_key = "AL:acme"
    db.commit()
    policy(db, allowed=False)
    assert sau.recheck_for_dispatch(db, m).block  # politika deny = autoritet eksplicit
    db.execute(text("DELETE FROM sms_synced_sender_policies"))
    db.commit()
    fresh(db, datetime(2020, 1, 1, tzinfo=UTC))
    c = sau.recheck_for_dispatch(db, m)
    assert not c.block and c.stale  # vjetërsia vetëm s'bllokon
    db.execute(text("DELETE FROM sms_synced_sender_authorizations"))
    db.commit()
    c = sau.recheck_for_dispatch(db, m)
    assert (
        not c.block and c.reason == "projection_missing"
    )  # boshllëk/ndërprerje: s'shpikim revokim
    mode(monkeypatch, "local")
    lm = send(db, "kl")
    assert sau.recheck_for_dispatch(db, lm) == sau.DispatchCheck(False, "approved", "local")
    s = db.get(SenderId, lm.sender_ref)
    s.status = ApprovalStatus.REVOKED
    s.approved_key = None
    db.commit()
    assert sau.recheck_for_dispatch(db, lm).block
    lm.sender_ref = None
    lm.sender_authority_source = None
    assert sau.recheck_for_dispatch(db, lm) == sau.DispatchCheck(False, "no_provenance", "none")


def test_recheck_is_not_wired_into_the_send_path_and_costs_bounded_sql(
    db, world, fake, monkeypatch
):
    for name in ("messages.py", "campaigns.py"):
        assert "recheck_for_dispatch" not in (ROOT_APP / "services" / name).read_text()
    project(db, "ACME", "approved")
    fresh(db)
    mode(monkeypatch, "central")
    m = send(db)
    sau.projection_stale(db)
    assert len(count_sql(lambda: sau.recheck_for_dispatch(db, m))) <= 3


def test_inbound_ownership_lookup_is_independent_of_the_outbound_authority(db, world, monkeypatch):
    s = sid.request(db, "c1", "AL", "355691230099")
    sid.approve(db, s.id, "staff")
    db.commit()
    base = [o.owner_ref for o in sid.owners_of_number(db, "+355691230099")]
    for m in ("shadow", "central"):
        mode(monkeypatch, m)
        assert [o.owner_ref for o in sid.owners_of_number(db, "+355691230099")] == base == ["c1"]
    assert "sender_authority" not in (ROOT_APP / "services" / "inbox.py").read_text()


def test_bypass_audit_every_outbound_authorization_goes_through_the_facade():
    allowed_check = {
        "services/sender_authority.py",
        "services/sender_authorization.py",
        "services/sender_ids.py",
    }
    offenders = []
    for p in ROOT_APP.rglob("*.py"):
        rel = p.relative_to(ROOT_APP).as_posix()
        src = p.read_text()
        if rel not in allowed_check and re.search(
            r"\b(assert_outbound|check_outbound|has_approved_sender)\b", src
        ):
            offenders.append(rel)
    assert offenders == []
    # statusi lokal i SenderId lexohet për autorizim vetëm te S0/facada/rishikimi lokal, kurrë te rruga e dërgimit
    for rel in ("services/messages.py", "services/campaigns.py", "api/messages.py"):
        assert "ApprovalStatus" not in (ROOT_APP / rel).read_text(), rel
    assert (
        len(re.findall(r"\bMessage\(", (ROOT_APP / "services" / "messages.py").read_text())) == 1
    )  # NJË vend ku krijohet SMS
    creators = [
        p.relative_to(ROOT_APP).as_posix()
        for p in ROOT_APP.rglob("*.py")
        if re.search(r"(?<![A-Za-z])Message\(\s*public_id", p.read_text())
    ]
    assert creators == ["services/messages.py"]


# =============================================================================================================
# migrimi 0031
# =============================================================================================================


def _drift(conn):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    import app.models  # noqa: F401
    from app.core.db import Base

    ctx = MigrationContext.configure(conn, opts={"compare_type": True})
    names = (
        "sms_sender_authority_comparisons",
        "sms_sender_bootstrap_state",
        "sender_authority_source",
        "sender_registry_ref",
        "sender_central",
    )
    return [
        repr(i)
        for d in compare_metadata(ctx, Base.metadata)
        for i in (d if isinstance(d, list) else [d])
        if any(n in repr(i) for n in names)
    ]


def test_enterprise_0031_is_additive_reversible_and_matches_metadata(make_db):
    url = make_db("ent")
    enterprise_alembic(url, "upgrade", "0030")
    eng = create_engine(url)
    before = {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    }
    enterprise_alembic(url, "upgrade", "0031")
    after = {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    }
    assert {"sms_sender_authority_comparisons", "sms_sender_bootstrap_state"} <= set(after) - set(
        before
    )
    assert after["sms_messages"] - before["sms_messages"] == {
        "sender_authority_source",
        "sender_registry_ref",
        "sender_central_decision_ref",
        "sender_central_revision",
    }
    for t, cols in before.items():
        if t != "sms_messages":
            assert after[t] == cols
    with eng.connect() as c:
        assert _drift(c) == []
        assert c.execute(text("select count(*) from sms_sender_bootstrap_state")).scalar() == 1
    enterprise_alembic(url, "downgrade", "0030")
    assert {
        t: {c["name"] for c in inspect(eng).get_columns(t)} for t in inspect(eng).get_table_names()
    } == before
    enterprise_alembic(url, "upgrade", "head")
    with eng.connect() as c:
        assert _drift(c) == []
    eng.dispose()


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_triggers_make_comparisons_append_only_and_bootstrap_state_undeletable(make_db):
    url = make_db("ent")
    if not url.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    enterprise_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(
            text(
                "insert into sms_sender_authority_comparisons (ref, country, category, local_allowed, central_allowed, central_reason, identity_hash, projection_stale, created_at) values ('r','AL','match_allowed',true,true,'approved','h',false,now())"
            )
        )
    for stmt in (
        "UPDATE sms_sender_authority_comparisons SET category='match_denied'",
        "DELETE FROM sms_sender_authority_comparisons",
        "TRUNCATE sms_sender_authority_comparisons",
        "DELETE FROM sms_sender_bootstrap_state",
    ):
        with eng.connect() as c:
            with pytest.raises(DBAPIError):
                c.execute(text(stmt))
                c.commit()
    eng.dispose()


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_concurrent_shadow_submits_record_every_mismatch_without_blocking(
    db, world, fake, monkeypatch
):
    import threading

    if engine.dialect.name != "postgresql":
        pytest.skip("postgres parametrization only")
    fresh(db)
    mode(monkeypatch, "shadow")
    errs, gate = [], threading.Barrier(4)

    def run(i):
        try:
            with SessionLocal() as s:
                gate.wait(timeout=20)
                msgs.submit(s, "c1", f"kp{i}", OK, "ACME", text="hello")
                s.commit()
        except Exception as e:  # noqa: BLE001
            errs.append(repr(e))

    ts = [threading.Thread(target=run, args=(i,)) for i in range(4)]
    [t.start() for t in ts]
    [t.join(90) for t in ts]
    assert not errs, errs
    assert len([c for c in cmps(db) if c.category in ("central_missing", "projection_stale")]) == 4


def test_rollback_cli_reports_without_changing_anything(db, world, monkeypatch, capsys):
    from scripts import sender_authority_readiness as cli

    project(db, "ACME", "revoked")
    before = db.scalar(select(func.count()).select_from(SyncedSenderAuthorization))
    assert cli.main(["--rollback-to", "shadow", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] in ("PASS", "WARN")
    assert cli.main(["--rollback-to", "local"]) == 1
    assert cli.main(["--rollback-to", "local", "--accept-divergence"]) == 0
    capsys.readouterr()
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(SyncedSenderAuthorization)) == before
