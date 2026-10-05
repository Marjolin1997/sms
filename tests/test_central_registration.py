# ruff: noqa: F811
"""M8-a — kërkesat e regjistrimit (domain + lifecycle): submit idempotent, token, approve/reject, audit."""

import ast
import logging
import threading
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.central.core import errors
from apps.central.core.db import Base
from apps.central.models import (
    AuditLog,
    CentralUser,
    Enterprise,
    RegistrationProduct,
    RegistrationRequest,
)
from apps.central.models.product import ImmutableError
from apps.central.models.registration import FAILED, PROVISIONED
from apps.central.services import audit as audit_svc
from apps.central.services import products as prod_svc
from apps.central.services import registration_policy as pol
from apps.central.services import registrations as reg
from apps.central.services import users
from tests.test_central import IS_PG, ROOT, central_alembic, make_db  # noqa: F401
from tests.test_central_products import cdb, db  # noqa: F401  (fixtures)
from tests.test_central_sync_outbox import run_threads

T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)
KEY = "key-0001-abcdef"


@pytest.fixture
def world(db):
    sms = prod_svc.create(db, "sms", "SMS", "sms")
    email = prod_svc.create(db, "email", "Email", "email")
    admin = users.create_user(db, "adm@example.com", "pw-Long-Enough-123", "admin")
    for p in (sms, email):  # M8-b: pa politikë të aktivizuar produkti s'kërkohet (fail-closed)
        pol.set_policy(db, p.id, admin, self_registration_enabled=True)
    db.commit()
    return db, sms, email, admin


def submit(db, products, **kw):
    kw = {"enterprise_name": "Acme Ltd", "contact_email": "Ana@Example.com"} | kw
    return reg.submit(db, product_ids=[p.id for p in products], **kw)


def table_counts(db):
    return {t: db.scalar(select(func.count()).select_from(Base.metadata.tables[t]))
            for t in ("enterprises", "enterprise_products", "sync_outbox", "registration_requests",
                      "registration_products", "audit_log")}  # fmt: skip


# --- submit: validim ------------------------------------------------------------------------------------


def test_valid_submit_creates_a_submitted_request_with_normalized_fields(world):
    db, sms, email, _ = world
    r = submit(db, [sms, email], enterprise_name="  Acme Ltd  ", contact_name="  Ana  ")
    db.commit()
    row = r.request
    assert r.created and r.access_token and len(r.access_token) >= 40
    assert (row.status, row.decision_mode, row.provisioning_status) == ("submitted", None, None)
    assert (row.enterprise_name, row.contact_email, row.contact_name) == (
        "Acme Ltd",
        "ana@example.com",
        "Ana",
    )
    assert (row.decided_at, row.decided_by_id, row.enterprise_id) == (None, None, None)
    assert {p.code for p in reg.requested_products(db, row.id)} == {"sms", "email"}
    assert all(rp.assignment_id is None for rp in db.scalars(select(RegistrationProduct)))


def test_active_product_accepted_retired_and_unknown_rejected_with_one_generic_error(world):
    db, sms, email, _ = world
    prod_svc.update(db, email.id, status="retired")
    db.commit()
    assert submit(db, [sms]).created
    for ids in ([email.id], [sms.id, email.id], [uuid.uuid4()]):
        with pytest.raises(errors.Invalid, match="products_unavailable") as ex:
            reg.submit(db, enterprise_name="X", contact_email="a@b.co", product_ids=ids)
        assert "email" not in str(ex.value).lower() or "not available" in str(
            ex.value
        )  # pa dallim retired/mungon


@pytest.mark.parametrize("n", [0, 6])
def test_product_count_must_be_between_one_and_five(world, n):
    db, *_ = world
    with pytest.raises(errors.Invalid):
        reg.submit(
            db,
            enterprise_name="X",
            contact_email="a@b.co",
            product_ids=[uuid.uuid4() for _ in range(n)],
        )


def test_five_products_are_accepted_and_duplicates_or_garbage_ids_are_rejected(world):
    db, sms, *_ = world
    extra = [prod_svc.create(db, f"p{i}", f"P{i}", "sms") for i in range(4)]
    admin = db.scalar(select(CentralUser))
    for p in extra:
        pol.set_policy(db, p.id, admin, self_registration_enabled=True)
    db.commit()
    assert submit(db, [sms, *extra]).created  # 5 = kufiri
    with pytest.raises(errors.Invalid, match="duplicate"):
        reg.submit(
            db, enterprise_name="X", contact_email="a@b.co", product_ids=[sms.id, str(sms.id)]
        )
    for bad in (["not-a-uuid"], "abc", None, [None]):
        with pytest.raises(errors.Invalid):
            reg.submit(db, enterprise_name="X", contact_email="a@b.co", product_ids=bad)


@pytest.mark.parametrize("kw", [
    {"contact_email": ""}, {"contact_email": "no-at"}, {"contact_email": "a@b"}, {"contact_email": "a b@c.de"},
    {"contact_email": "a@b\n.co"}, {"contact_email": "x" * 250 + "@b.co"}, {"contact_email": 5},
    {"enterprise_name": ""}, {"enterprise_name": "   "}, {"enterprise_name": "x" * 201},
    {"enterprise_name": "a\x00b"}, {"enterprise_name": None}, {"contact_name": "x" * 121},
    {"contact_name": "a\x07"}, {"submission_key": "short"}, {"submission_key": "bad key with spaces!"},
])  # fmt: skip
def test_invalid_email_name_contact_and_key_are_rejected(world, kw):
    db, sms, *_ = world
    with pytest.raises(errors.Invalid):
        submit(db, [sms], **kw)
    assert db.scalar(select(func.count()).select_from(RegistrationRequest)) == 0


def test_email_normalization_rule_is_strip_and_lowercase(world):
    db, sms, *_ = world
    assert reg.normalize_contact_email("  A.B+tag@Example.COM ") == "a.b+tag@example.com"
    assert (
        submit(db, [sms], contact_email="  BOB@Example.com\t").request.contact_email
        == "bob@example.com"
    )


# --- token ---------------------------------------------------------------------------------------------------


def test_token_is_returned_only_on_initial_create_and_only_its_hash_is_persisted(world):
    db, sms, *_ = world
    first = submit(db, [sms], submission_key=KEY)
    db.commit()
    token = first.access_token
    row = first.request
    assert row.access_token_hash != token and row.access_token_hash == reg.hash_access_token(token)
    assert len(row.access_token_hash) == 64
    # plaintext nuk gjendet në asnjë kolonë të asnjë rreshti (as copë e tij)
    dump = " ".join(
        str(v) for r in db.execute(text("select * from registration_requests")).all() for v in r
    )
    assert token not in dump and token[:12] not in dump
    again = submit(db, [sms], submission_key=KEY)
    assert (
        not again.created and again.access_token is None and again.request.id == row.id
    )  # pa token të ri
    assert reg.verify_access_token(row, token) is True
    assert reg.verify_access_token(row, "x" + token) is False
    for bad in (None, "", 5, b"x"):
        assert reg.verify_access_token(row, bad) is False
    assert reg.verify_access_token(None, token) is False


def test_tokens_are_unique_high_entropy_and_never_in_repr_logs_or_errors(world, caplog):
    db, sms, *_ = world
    caplog.set_level(logging.DEBUG)
    a, b = submit(db, [sms]), submit(db, [sms])
    assert a.access_token != b.access_token
    assert (
        a.access_token not in repr(a)
        and a.access_token not in str(a)
        and a.access_token not in caplog.text
    )
    submit(db, [sms], submission_key=KEY)  # çelësi lidhet me përmbajtjen e parë
    with pytest.raises(errors.Conflict) as ex:  # replay me përmbajtje tjetër: gabimi s'ka token
        submit(db, [sms], submission_key=KEY, enterprise_name="Another")
    assert a.access_token not in str(ex.value)
    src = (ROOT / "apps/central/services/registrations.py").read_text()
    tree = ast.parse(src)
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert "logging" not in imported
    assert not [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "print"
    ]
    assert "compare_digest" in src


# --- idempotencë / dedupe ---------------------------------------------------------------------------------------


def test_submission_key_is_idempotent_scoped_to_the_contact_and_detects_reuse(world):
    db, sms, email, _ = world
    a = submit(db, [sms], submission_key=KEY)
    db.commit()
    b = submit(db, [sms], submission_key=KEY)  # i njëjti (kontakt, çelës, përmbajtje) ⇒ replay
    assert (b.created, b.request.id) == (False, a.request.id)
    with pytest.raises(errors.Conflict, match="different request"):
        submit(db, [email], submission_key=KEY)  # çelës i ripërdorur me përmbajtje tjetër
    c = submit(
        db, [sms], submission_key=KEY, contact_email="other@example.com"
    )  # kontakt tjetër ⇒ i ri
    assert c.created and c.request.id != a.request.id
    d = submit(db, [sms])  # pa çelës: kërkesë e re (pa dedupe sipas emailit)
    e = submit(db, [sms])
    assert d.created and e.created and d.request.id != e.request.id
    db.commit()
    assert db.scalar(select(func.count()).select_from(RegistrationRequest)) == 4


def test_replay_does_not_duplicate_products_or_requests(world):
    db, sms, email, _ = world
    submit(db, [sms, email], submission_key=KEY)
    db.commit()
    for _ in range(3):
        assert not submit(
            db, [email, sms], submission_key=KEY
        ).created  # rend tjetër produktesh = i njëjti
    db.commit()
    assert db.scalar(select(func.count()).select_from(RegistrationRequest)) == 1
    assert db.scalar(select(func.count()).select_from(RegistrationProduct)) == 2


@pytest.fixture
def pg(make_db):  # noqa: F811
    url = make_db()
    if not url.startswith("postgresql"):
        pytest.skip("postgres parametrization only")
    central_alembic(url, "upgrade", "head")
    eng = create_engine(url)
    yield eng
    eng.dispose()


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_concurrent_same_submission_key_creates_exactly_one_request(pg):
    with Session(pg, expire_on_commit=False) as s:
        sms = prod_svc.create(s, "sms", "SMS", "sms")
        admin = users.create_user(s, "adm@example.com", "pw-Long-Enough-123", "admin")
        pol.set_policy(s, sms.id, admin, self_registration_enabled=True)
        s.commit()
    barrier = threading.Barrier(6, timeout=20)
    results = []

    def worker():
        with Session(pg, expire_on_commit=False) as s:
            barrier.wait()
            r = reg.submit(s, enterprise_name="Acme", contact_email="ana@example.com",
                           product_ids=[sms.id], submission_key=KEY)  # fmt: skip
            s.commit()
            results.append((r.created, r.access_token is not None, r.request.id))

    run_threads([worker] * 6)
    assert len(results) == 6
    assert sum(1 for c, _t, _i in results if c) == 1  # saktësisht një krijim
    assert sum(1 for _c, t, _i in results if t) == 1  # saktësisht një token i lëshuar
    assert len({i for *_, i in results}) == 1
    with Session(pg) as s:
        assert s.scalar(select(func.count()).select_from(RegistrationRequest)) == 1
        assert s.scalar(select(func.count()).select_from(RegistrationProduct)) == 1


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_pg_concurrent_approve_and_reject_end_in_one_consistent_state(pg):
    with Session(pg, expire_on_commit=False) as s:
        sms = prod_svc.create(s, "sms", "SMS", "sms")
        admin = users.create_user(s, "adm@example.com", "pw-Long-Enough-123", "admin")
        pol.set_policy(s, sms.id, admin, self_registration_enabled=True)
        rid = reg.submit(
            s, enterprise_name="Acme", contact_email="a@b.co", product_ids=[sms.id]
        ).request.id
        s.commit()
    barrier = threading.Barrier(2, timeout=20)
    outcomes = []

    def go(fn):
        def run():
            with Session(pg, expire_on_commit=False) as s:
                barrier.wait()
                try:
                    fn(s, s.get(type(admin), admin.id))
                    s.commit()
                    outcomes.append("ok")
                except errors.Conflict:
                    s.rollback()
                    outcomes.append("conflict")

        return run

    run_threads(
        [go(lambda s, a: reg.approve(s, rid, a)), go(lambda s, a: reg.reject(s, rid, a, "no"))]
    )
    with Session(pg) as s:
        row = s.get(RegistrationRequest, rid)
        n = s.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action.like("registration.%"))
        )
        assert row.status in ("approved", "rejected") and n == 1  # një vendim, një audit
        if row.status == "approved":
            assert outcomes.count("conflict") == 1  # reject i approved-pending ⇒ Conflict
        else:
            assert outcomes.count("conflict") == 1  # approve i rejected ⇒ Conflict


# --- approve ---------------------------------------------------------------------------------------------------


def test_manual_approve_sets_decision_pending_provisioning_and_audits_a_human(world):
    db, sms, email, admin = world
    row = submit(db, [sms, email]).request
    db.commit()
    out = reg.approve(db, row.id, admin, now=T0)
    db.commit()
    assert (
        out.status == "approved"
        and out.decision_mode == "manual"
        and out.provisioning_status == "pending"
    )
    assert (out.decided_by_id, out.decided_by_label, out.decided_at.replace(tzinfo=UTC)) == (
        admin.id,
        None,
        T0,
    )
    assert out.decision_reason is None and out.enterprise_id is None
    a = db.scalar(select(AuditLog).where(AuditLog.action.like("registration.%")))
    assert (a.actor_kind, a.actor_id, a.actor_label) == ("user", admin.id, None)
    assert (a.action, a.resource_type, a.resource_id) == (
        "registration.approve",
        "registration_request",
        str(row.id),
    )
    assert a.detail == {
        "decision_mode": "manual",
        "contact_verified": False,
        "products": [  # politika LIVE në momentin e vendimit
            {"code": "email", "approval_mode": "manual", "self_registration_enabled": True},
            {"code": "sms", "approval_mode": "manual", "self_registration_enabled": True},
        ],
    }


def test_approve_is_idempotent_and_does_not_touch_an_approved_request(world):
    db, sms, _, admin = world
    row = submit(db, [sms]).request
    reg.approve(db, row.id, admin, now=T0)
    db.commit()

    def as_utc(x):
        return x.replace(tzinfo=UTC) if x.tzinfo is None else x

    def snap():
        r = db.get(RegistrationRequest, row.id, populate_existing=True)
        return (
            as_utc(r.decided_at),
            as_utc(r.updated_at),
            db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action.like("registration.%"))
            ),
        )

    before = snap()
    again = reg.approve(db, row.id, admin, now=datetime(2031, 1, 1, tzinfo=UTC))
    db.commit()
    assert again.id == row.id and snap() == before  # zero ndryshim, zero audit i ri


def test_approve_rejected_is_a_conflict_and_changes_nothing(world):
    db, sms, _, admin = world
    row = submit(db, [sms]).request
    reg.reject(db, row.id, admin, "spam")
    db.commit()
    n = table_counts(db)
    with pytest.raises(errors.Conflict, match="rejected"):
        reg.approve(db, row.id, admin)
    db.rollback()
    assert table_counts(db) == n and db.get(RegistrationRequest, row.id).status == "rejected"


def test_product_retired_after_submit_blocks_approval(world):
    db, sms, email, admin = world
    row = submit(db, [sms, email]).request
    db.commit()
    prod_svc.update(db, email.id, status="retired")
    db.commit()
    with pytest.raises(errors.Conflict, match="no longer available: email"):
        reg.approve(db, row.id, admin)
    db.rollback()
    row = db.get(RegistrationRequest, row.id)
    assert row.status == "submitted" and row.provisioning_status is None
    assert (
        db.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action.like("registration.%"))
        )
        == 0
    )


def test_approve_requires_a_human_central_user_and_an_existing_request(world):
    db, sms, _, admin = world
    row = submit(db, [sms]).request
    for bad in ("system:registration_auto_approval", None, object()):
        with pytest.raises(errors.Invalid):
            reg.approve(db, row.id, bad)
        with pytest.raises(errors.Invalid):
            reg.reject(db, row.id, bad, "x")
    for rid in (uuid.uuid4(), "not-a-uuid"):
        with pytest.raises(errors.NotFound):
            reg.approve(db, rid, admin)


# --- reject ----------------------------------------------------------------------------------------------------


def test_manual_reject_requires_a_reason_and_audits_a_human(world):
    db, sms, _, admin = world
    row = submit(db, [sms]).request
    db.commit()
    for bad in ("", "   ", None, 5, "x" * 501, "a\x00b"):
        with pytest.raises(errors.Invalid):
            reg.reject(db, row.id, admin, bad)
    db.rollback()
    out = reg.reject(db, row.id, admin, "  duplicate customer  ", now=T0)
    db.commit()
    assert (out.status, out.decision_mode, out.decision_reason, out.provisioning_status) == (
        "rejected", "manual", "duplicate customer", None)  # fmt: skip
    a = db.scalar(select(AuditLog).where(AuditLog.action.like("registration.%")))
    assert (a.actor_kind, a.actor_id, a.action) == ("user", admin.id, "registration.reject")
    assert a.detail == {
        "reason": "duplicate customer",
        "from": {"status": "submitted", "provisioning_status": None},
    }


def test_reject_rejected_is_a_noop(world):
    db, sms, _, admin = world
    row = submit(db, [sms]).request
    reg.reject(db, row.id, admin, "first", now=T0)
    db.commit()
    again = reg.reject(db, row.id, admin, "second reason")
    db.commit()
    assert (
        again.decision_reason == "first"
        and db.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action.like("registration.%"))
        )
        == 1
    )


def test_reject_rules_for_approved_requests_pending_failed_provisioned(world):
    """`failed`/`provisioned` hyjnë në M8-c; këtu kontrata provohet duke vendosur gjendjen drejtpërdrejt."""
    db, sms, _, admin = world
    row = submit(db, [sms]).request
    reg.approve(db, row.id, admin)
    db.commit()
    with pytest.raises(errors.Conflict, match="awaiting provisioning"):
        reg.reject(db, row.id, admin, "x")  # approved + pending
    db.rollback()
    row = db.get(RegistrationRequest, row.id)
    e = Enterprise(name="E")
    db.add(e)
    db.flush()
    row.provisioning_status, row.enterprise_id = PROVISIONED, e.id
    db.commit()
    with pytest.raises(errors.Conflict, match="provisioned"):
        reg.reject(db, row.id, admin, "x")  # provisioned ⇒ 409, pa rollback shkatërrues
    db.rollback()
    row = db.get(RegistrationRequest, row.id)
    row.provisioning_status, row.enterprise_id = FAILED, None
    db.commit()
    out = reg.reject(
        db, row.id, admin, "closing a failed provisioning"
    )  # approved + failed ⇒ lejohet
    db.commit()
    assert (
        out.status == "rejected" and out.provisioning_status == "failed"
    )  # dështimi s'u kthye në rejected vetë
    a = list(
        db.scalars(
            select(AuditLog)
            .where(AuditLog.action.like("registration.%"))
            .order_by(AuditLog.created_at)
        )
    )
    assert a[-1].detail["from"] == {"status": "approved", "provisioning_status": "failed"}


# --- atomicitet ------------------------------------------------------------------------------------------------


def test_state_change_and_audit_roll_back_together(world, monkeypatch):
    db, sms, _, admin = world
    row = submit(db, [sms]).request
    db.commit()
    reg.approve(db, row.id, admin)
    db.rollback()  # kalimi dhe audit-i zhduken bashkë
    assert db.get(RegistrationRequest, row.id).status == "submitted"
    assert (
        db.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action.like("registration.%"))
        )
        == 0
    )

    def boom(*a, **k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(audit_svc, "record", boom)
    with pytest.raises(RuntimeError):
        reg.reject(db, row.id, admin, "x")
    db.rollback()
    assert db.get(RegistrationRequest, row.id).status == "submitted"  # asnjë kalim pa audit


def test_service_never_commits(world):
    db, sms, _, admin = world
    row = submit(db, [sms]).request
    reg.approve(db, row.id, admin)
    src = (ROOT / "apps/central/services/registrations.py").read_text()
    assert ".commit(" not in src
    db.rollback()
    assert (
        db.scalar(select(func.count()).select_from(RegistrationRequest)) == 0
    )  # as submit s'u commit-ua


# --- kufij, skemë, asnjë provisioning --------------------------------------------------------------------------


def test_no_hard_delete_orm_guard_and_no_delete_calls(world):
    db, sms, *_ = world
    row = submit(db, [sms]).request
    db.commit()
    for obj in (row, db.scalar(select(RegistrationProduct))):
        db.delete(obj)
        with pytest.raises(ImmutableError):
            db.flush()
        db.rollback()
    tree = ast.parse((ROOT / "apps/central/services/registrations.py").read_text())
    assert not [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") in {"delete", "merge"}
    ]
    rp = db.scalar(select(RegistrationProduct))
    rp.product_id = uuid.uuid4()
    with pytest.raises(ImmutableError):
        db.flush()
    db.rollback()


def test_database_checks_enforce_the_state_contract(world):
    db, sms, _, admin = world
    row = submit(db, [sms]).request
    db.commit()
    rid = row.id
    bad_updates = [
        "status = 'approved'",  # pa vendim/aktor/provisioning
        "status = 'rejected', decided_at = CURRENT_TIMESTAMP, decision_mode = 'manual', decided_by_id = :a",  # pa arsye
        "provisioning_status = 'pending'",  # submitted s'ka provisioning
        "status = 'bogus'",
        "decision_mode = 'robot'",
    ]
    for sql in bad_updates:
        with pytest.raises(IntegrityError):
            db.execute(text(f"update registration_requests set {sql} where id = :i"),
                       {"i": rid.hex if db.get_bind().dialect.name == "sqlite" else rid,
                        "a": admin.id.hex if db.get_bind().dialect.name == "sqlite" else admin.id})  # fmt: skip
            db.flush()
        db.rollback()


def test_m8a_adds_only_the_two_central_tables_and_nothing_to_the_enterprise_schema(world):
    db, *_ = world
    names = set(Base.metadata.tables)
    assert {"registration_requests", "registration_products"} <= names
    assert not any(n.startswith("sms_") for n in names)
    cols = {c.name for c in RegistrationRequest.__table__.columns}
    assert cols == {"id", "contact_email", "contact_name", "enterprise_name", "submission_key", "request_hash",
                    "access_token_hash", "status", "decision_mode", "decided_at", "decided_by_id",
                    "decided_by_label", "decision_reason", "provisioning_status", "provisioning_attempts",
                    "verified_at", "verification_nonce", "verification_expires_at",
                    "provisioning_error_code", "enterprise_id", "created_at", "updated_at"}  # fmt: skip
    assert not {"password", "token", "access_token", "owner_ref"} & cols


def test_nothing_is_provisioned_and_no_http_routes_exist_yet(world):
    from apps.central.main import create_app

    db, sms, email, admin = world
    row = submit(db, [sms, email]).request
    reg.approve(db, row.id, admin)
    db.commit()
    c = table_counts(db)
    assert (c["enterprises"], c["enterprise_products"], c["sync_outbox"]) == (
        0,
        0,
        0,
    )  # pa provisioning/outbox
    assert all(rp.assignment_id is None for rp in db.scalars(select(RegistrationProduct)))
    assert db.get(RegistrationRequest, row.id).enterprise_id is None
    paths = create_app(db.get_bind()).openapi()["paths"]
    assert (
        "/registration" in paths
    )  # M8-d: API HTTP ekziston; ky test provon vetëm që miratimi s'ka efekte


def test_registration_modules_are_central_only(world):
    for rel in ("models/registration.py", "services/registrations.py"):
        tree = ast.parse((ROOT / "apps/central" / rel).read_text())
        mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not [m for m in mods if m == "app" or m.startswith("app.")], (
            rel
        )  # Central ↛ Enterprise
        assert not [
            m for m in mods if m.split(".")[0] in {"httpx", "requests", "socket", "urllib"}
        ], rel


def test_enterprise_db_is_untouched_by_registration():
    """Shërbimi s'ka asnjë qasje në Enterprise DB: i njëjti kod nuk importon `app.*` (provuar më sipër)
    dhe asnjë tabelë `sms_*` s'ndryshon në metadata-n e Central."""
    from app.core.db import Base as EnterpriseBase

    assert not set(Base.metadata.tables) & set(EnterpriseBase.metadata.tables)


# --- migrimi ----------------------------------------------------------------------------------------------------


def test_migration_0013_up_down_up_and_schema_matches_metadata(make_db):
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    url = make_db("central")
    central_alembic(url, "upgrade", "0012")
    eng = create_engine(url)
    assert "registration_requests" not in set(inspect(eng).get_table_names())
    central_alembic(url, "upgrade", "0013")
    assert {"registration_requests", "registration_products"} <= set(inspect(eng).get_table_names())
    central_alembic(url, "downgrade", "0012")
    assert not {"registration_requests", "registration_products"} & set(
        inspect(eng).get_table_names()
    )
    central_alembic(url, "upgrade", "head")
    with eng.connect() as c:
        ctx = MigrationContext.configure(
            c, opts={"compare_type": True, "version_table": "central_alembic_version"}
        )
        assert compare_metadata(ctx, Base.metadata) == []
        ver = c.execute(text("select version_num from central_alembic_version")).scalar()
    assert ver == "0018"
    fks = {
        fk["referred_table"]: fk for fk in inspect(eng).get_foreign_keys("registration_requests")
    }
    assert set(fks) == {"users", "enterprises"}
    eng.dispose()
