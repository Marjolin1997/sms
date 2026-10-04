# ruff: noqa: F811
"""M8-d — API publike + admin e regjistrimit (Central). Pa Enterprise ORM, pa HTTP drejt Enterprise."""

import ast
import logging
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from apps.central.core import errors
from apps.central.core.config import settings
from apps.central.main import create_app
from apps.central.models import (
    AuditLog,
    Enterprise,
    EnterpriseProduct,
    RegistrationRequest,
    SyncOutbox,
)
from apps.central.services import products as prod_svc
from apps.central.services import provisioning as prov
from apps.central.services import registration_policy as pol
from apps.central.services import registrations as reg
from apps.central.services import users
from tests.test_central import IS_PG, ROOT, make_db  # noqa: F401
from tests.test_central_auth import PW, auth_secret, bearer, mk, token_for  # noqa: F401
from tests.test_central_products import cdb  # noqa: F401  (fixtures)
from tests.test_central_sync_outbox import run_threads

T0 = datetime(2030, 1, 1, 12, tzinfo=UTC)


class H:
    pass


@pytest.fixture
def h(cdb, auth_secret, monkeypatch):
    url, eng = cdb
    monkeypatch.setattr(settings, "public_registration_enabled", True)
    x = H()
    x.eng, x.url = eng, url
    x.factory = sessionmaker(bind=eng, expire_on_commit=False)
    x.c = TestClient(create_app(eng))
    x.admin_u = mk(eng, "adm@example.com", role="admin")
    mk(eng, "op@example.com", role="operator")
    x.A = bearer(token_for(x.c, "adm@example.com"))
    x.O = bearer(token_for(x.c, "op@example.com"))
    with x.factory() as s:
        x.sms = prod_svc.create(s, "sms", "SMS", "sms", "Mesazhe SMS").id
        x.email = prod_svc.create(s, "email", "Email", "email").id
        admin = s.get(users.CentralUser, x.admin_u.id)
        for p in (x.sms, x.email):
            pol.set_policy(s, p, admin, self_registration_enabled=True)
        s.commit()
    return x


def body(h, **kw):
    return {"contact_email": "ana@example.com", "enterprise_name": "Acme Ltd",
            "product_ids": [str(h.sms)]} | kw  # fmt: skip


def post(h, b=None, key=None, **kw):
    headers = {"Idempotency-Key": key} if key else {}
    return h.c.post("/registration", json=b if b is not None else body(h), headers=headers, **kw)


def rows(h, model=RegistrationRequest, *where):
    with h.factory() as s:
        return list(s.scalars(select(model).where(*where)))


def count(h, model, *where):
    with h.factory() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where))


def status(h, rid, token):
    return h.c.get(
        f"/registration/{rid}/status", headers={"X-Registration-Token": token} if token else {}
    )


# --- publike: flag, produkte, submit -------------------------------------------------------------------


def test_public_flag_default_is_false_and_disables_every_public_route_but_not_admin(h, monkeypatch):
    from apps.central.core.config import Settings

    assert Settings(_env_file=None).public_registration_enabled is False
    monkeypatch.setattr(settings, "public_registration_enabled", False)
    for r in (h.c.get("/registration/products"), post(h), status(h, uuid.uuid4(), "x")):
        assert r.status_code == 503 and r.json()["detail"]["code"] == "registration_unavailable"
    assert h.c.get("/admin/registrations", headers=h.A).status_code == 200  # admin pa ndikim
    assert settings.allow_unverified_auto_registration is False  # koncept i ndarë


def test_products_lists_only_active_and_enabled_with_minimal_fields(h):
    with h.factory() as s:
        admin = s.get(users.CentralUser, h.admin_u.id)
        pol.set_policy(s, h.email, admin, self_registration_enabled=False)
        extra = prod_svc.create(s, "nopolicy", "NoPolicy", "sms").id  # pa politikë
        old = prod_svc.create(s, "old", "Old", "sms").id
        pol.set_policy(s, old, admin, self_registration_enabled=True)
        prod_svc.update(s, old, status="retired")
        s.commit()
    r = h.c.get("/registration/products")
    assert r.status_code == 200
    assert r.json() == [{"id": str(h.sms), "code": "sms", "name": "SMS", "description": "Mesazhe SMS",
                         "channel": "sms", "approval_mode": "manual"}]  # fmt: skip
    assert extra not in [x["id"] for x in r.json()]


def test_products_effective_approval_mode_never_promises_blocked_automatic(h, monkeypatch):
    monkeypatch.setattr(settings, "allow_unverified_auto_registration", True)
    with h.factory() as s:
        pol.set_policy(s, h.sms, s.get(users.CentralUser, h.admin_u.id), approval_mode="automatic")
        s.commit()
    mode = lambda: {x["code"]: x["approval_mode"] for x in h.c.get("/registration/products").json()}  # noqa: E731
    assert mode() == {"email": "manual", "sms": "automatic"}
    monkeypatch.setattr(settings, "allow_unverified_auto_registration", False)  # gate mbyllet
    assert mode() == {"email": "manual", "sms": "manual"}


def test_valid_manual_submit_is_202_with_token_once_and_status_in_review(h):
    r = post(h)
    assert r.status_code == 202
    j = r.json()
    assert set(j) == {"id", "status", "access_token", "token_issued"}
    assert (j["status"], j["token_issued"]) == ("in_review", True) and len(j["access_token"]) >= 40
    assert count(h, RegistrationRequest) == 1


def test_idempotent_replay_returns_same_registration_without_token_or_duplicates(h):
    a = post(h, key="key-aaaaaaaa").json()
    r = post(h, key="key-aaaaaaaa")
    b = r.json()
    assert r.status_code == 200
    assert (b["id"], b["access_token"], b["token_issued"]) == (a["id"], None, False)
    assert count(h, RegistrationRequest) == 1


def test_same_key_with_changed_payload_is_a_safe_409(h):
    post(h, key="key-aaaaaaaa")
    r = post(h, body(h, enterprise_name="Other"), key="key-aaaaaaaa")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "idempotency_conflict"
    assert "hash" not in r.text and "constraint" not in r.text.lower()
    assert count(h, RegistrationRequest) == 1


def test_same_key_from_a_different_contact_is_a_separate_request(h):
    a = post(h, key="key-aaaaaaaa").json()
    b = post(h, body(h, contact_email="bob@example.com"), key="key-aaaaaaaa").json()
    assert a["id"] != b["id"] and b["token_issued"] is True


def test_replay_does_not_repeat_the_automatic_approval_audit(h, monkeypatch):
    monkeypatch.setattr(settings, "allow_unverified_auto_registration", True)
    with h.factory() as s:
        pol.set_policy(s, h.sms, s.get(users.CentralUser, h.admin_u.id), approval_mode="automatic")
        s.commit()
    a = post(h, key="key-aaaaaaaa").json()
    assert a["status"] == "activating"  # approved + pending
    post(h, key="key-aaaaaaaa")
    assert count(h, AuditLog, AuditLog.action == "registration.approve") == 1


def test_automatic_policy_with_closed_gate_stays_in_review(h, monkeypatch):
    monkeypatch.setattr(settings, "allow_unverified_auto_registration", True)
    with h.factory() as s:
        pol.set_policy(s, h.sms, s.get(users.CentralUser, h.admin_u.id), approval_mode="automatic")
        s.commit()
    monkeypatch.setattr(settings, "allow_unverified_auto_registration", False)
    assert post(h).json()["status"] == "in_review"


def test_unavailable_products_all_collapse_to_products_unavailable(h):
    with h.factory() as s:
        admin = s.get(users.CentralUser, h.admin_u.id)
        pol.set_policy(s, h.email, admin, self_registration_enabled=False)
        gone = prod_svc.create(s, "gone", "Gone", "sms").id
        s.commit()
    for ids in ([h.email], [gone], [uuid.uuid4()], [h.sms, h.email]):
        r = post(h, body(h, product_ids=[str(i) for i in ids]))
        assert r.status_code == 422 and r.json()["detail"]["code"] == "products_unavailable"
        assert r.json()["detail"]["message"] == "one or more requested products are unavailable"
    assert count(h, RegistrationRequest) == 0


def test_body_validation_extra_fields_limits_and_garbage(h):
    cases = [
        body(h, extra=1),
        body(h, product_ids=[]),
        body(h, product_ids=[str(uuid.uuid4()) for _ in range(6)]),
        body(h, product_ids=["nope"]),
        body(h, enterprise_name="x" * 201),
        body(h, contact_email="x" * 300),
        body(h, contact_name="n" * 121),
        body(h, contact_email="not-an-email"),
        body(h, product_ids=[str(h.sms), str(h.sms)]),
        {"contact_email": "a@b.co"},
    ]
    for c in cases:
        r = post(h, c)
        assert r.status_code == 422 and r.json()["detail"]["code"] in (
            "invalid_request",
            "products_unavailable",
        ), c
        assert set(r.json()["detail"]) == {"code", "message"}  # pa detaje pydantic
    for raw in (b"{", b"[]", b"null", b""):
        r = h.c.post("/registration", content=raw)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_request"
    assert post(h, key="x").status_code == 422  # Idempotency-Key shumë i shkurtër
    assert count(h, RegistrationRequest) == 0


def test_body_over_4kb_is_rejected_even_without_content_length(h):
    big = body(h, contact_name="n" * 5000)
    assert post(h, big).status_code == 413

    def gen():
        for _ in range(10):
            yield b" " * 1024

    r = h.c.post("/registration", content=gen())  # chunked, pa Content-Length
    assert r.status_code == 413 and r.json()["detail"]["code"] == "request_too_large"
    assert count(h, RegistrationRequest) == 0


# --- statusi publik + anti-enumerim ---------------------------------------------------------------------


def test_status_with_valid_token_and_public_mapping_for_every_state(h):
    j = post(h).json()
    rid, tok = j["id"], j["access_token"]
    r = status(h, rid, tok)
    assert r.status_code == 200 and r.json() == {"id": rid, "status": "in_review"}  # 9, 12
    with h.factory() as s:
        reg.approve(s, rid, s.get(users.CentralUser, h.admin_u.id))
        s.commit()
    assert status(h, rid, tok).json()["status"] == "activating"  # 13
    with h.factory() as s:  # failed ⇒ prapë activating
        prod_svc.update(s, h.sms, status="retired")
        s.commit()
    assert prov.run(h.factory, uuid.UUID(rid)).status == "failed"
    r = status(h, rid, tok)
    assert r.json()["status"] == "activating"
    assert "error" not in r.text and "product_retired" not in r.text  # 16
    with h.factory() as s:
        prod_svc.update(s, h.sms, status="active")
        s.commit()
    assert prov.run(h.factory, uuid.UUID(rid)).status == "provisioned"
    r = status(h, rid, tok)
    assert r.json() == {"id": rid, "status": "active"}  # 14
    assert set(r.json()) == {"id", "status"}  # pa enterprise_id/assignment ids


def test_status_rejected_mapping_and_no_internal_fields(h):
    j = post(h).json()
    with h.factory() as s:
        reg.reject(s, j["id"], s.get(users.CentralUser, h.admin_u.id), "internal reason text")
        s.commit()
    r = status(h, j["id"], j["access_token"])
    assert r.json() == {"id": j["id"], "status": "rejected"}
    assert "internal reason" not in r.text


def test_wrong_missing_and_unknown_are_indistinguishable_404s(h):
    j = post(h).json()
    cases = [
        status(h, j["id"], "wrong-token"),
        status(h, j["id"], None),
        status(h, uuid.uuid4(), j["access_token"]),
        status(h, "not-a-uuid", j["access_token"]),
        status(h, j["id"], "x" * 5000),
    ]
    shapes = {(r.status_code, r.text) for r in cases}
    assert shapes == {(404, '{"detail":{"code":"not_found","message":"not found"}}')}
    assert status(h, j["id"], j["access_token"]).status_code == 200


def test_token_is_never_logged_or_returned_again_and_hash_never_leaks(h, caplog):
    caplog.set_level(logging.DEBUG)
    j = post(h).json()
    status(h, j["id"], j["access_token"])
    status(h, j["id"], "bad")
    with h.factory() as s:
        row = s.get(RegistrationRequest, uuid.UUID(j["id"]))
        digest = row.access_token_hash
    assert j["access_token"] not in caplog.text and digest not in caplog.text
    for r in (
        status(h, j["id"], j["access_token"]),
        post(h, key="key-aaaaaaaa"),
        post(h, key="key-aaaaaaaa"),
    ):
        assert digest not in r.text and "access_token_hash" not in r.text
    admin = h.c.get(f"/admin/registrations/{j['id']}", headers=h.A)
    assert (
        digest not in admin.text
        and "access_token" not in admin.text
        and j["access_token"] not in admin.text
    )


# --- abuzim: kuota për email --------------------------------------------------------------------------------


def test_email_quota_counts_real_requests_not_keys_and_replay_is_free(h):
    for i in range(3):
        assert post(h, key=f"key-vary-{i:04d}").status_code == 202  # çelësa të ndryshëm: numërohen
    r = post(h, key="key-vary-9999")
    assert r.status_code == 429 and r.json()["detail"]["code"] == "too_many_requests"
    assert post(h).status_code == 429  # pa çelës: po ashtu
    assert post(h, key="key-vary-0001").status_code == 200  # replay: pa kuotë të konsumuar
    assert post(h, body(h, contact_email="other@example.com")).status_code == 202  # email tjetër
    assert (
        count(h, RegistrationRequest, RegistrationRequest.contact_email == "ana@example.com") == 3
    )


def test_email_quota_is_case_insensitive_via_normalization(h):
    for e in ("ANA@example.com", " ana@example.com", "Ana@Example.com"):
        assert post(h, body(h, contact_email=e)).status_code == 202
    assert post(h, body(h, contact_email="ana@EXAMPLE.com")).status_code == 429


def test_quota_window_is_a_rolling_24h_in_utc_at_the_service_level(h):
    with h.factory() as s:
        for i in range(3):
            reg.submit(s, enterprise_name="A", contact_email="w@example.com", product_ids=[h.sms],
                       max_per_email_24h=3, now=T0 + timedelta(minutes=i))  # fmt: skip
        s.commit()
        with pytest.raises(errors.TooManyRequests):
            reg.submit(s, enterprise_name="A", contact_email="w@example.com", product_ids=[h.sms],
                       max_per_email_24h=3, now=T0 + timedelta(hours=23, minutes=59))  # fmt: skip
        s.rollback()
        reg.submit(s, enterprise_name="A", contact_email="w@example.com", product_ids=[h.sms],
                   max_per_email_24h=3, now=T0 + timedelta(hours=24, minutes=1))  # fmt: skip
        s.commit()


@pytest.mark.skipif(not IS_PG, reason="needs PostgreSQL")
def test_concurrent_submissions_cannot_exceed_the_email_quota_on_pg(h):
    if h.url.startswith("sqlite"):
        pytest.skip("needs PostgreSQL advisory locks")
    n, results = 10, []
    barrier = threading.Barrier(n, timeout=30)
    lock = threading.Lock()

    def worker(i):
        def run():
            with h.factory() as s:
                barrier.wait()
                try:
                    reg.submit(s, enterprise_name="A", contact_email="race@example.com",
                               product_ids=[h.sms], submission_key=f"race-key-{i:04d}",
                               max_per_email_24h=3)  # fmt: skip
                    s.commit()
                    out = "ok"
                except errors.TooManyRequests:
                    s.rollback()
                    out = "denied"
            with lock:
                results.append(out)

        return run

    run_threads([worker(i) for i in range(n)])
    assert sorted(results).count("ok") == 3 and results.count("denied") == n - 3
    assert (
        count(h, RegistrationRequest, RegistrationRequest.contact_email == "race@example.com") == 3
    )


def test_ip_rate_limiting_is_not_claimed_in_app():
    src = (ROOT / "apps/central/api/registration_public.py").read_text()
    assert "X-Forwarded-For" not in src and "client.host" not in src  # IP limit = proxy (M8-e)
    doc = (ROOT / "docs/M8_REGISTRATION.md").read_text()
    assert "IP" in doc and "reverse-proxy" in doc


# --- admin: RBAC + rrjedha ------------------------------------------------------------------------------------


def test_admin_routes_require_authentication(h):
    rid = uuid.uuid4()
    calls = [
        ("get", "/admin/registrations"), ("get", f"/admin/registrations/{rid}"),
        ("post", f"/admin/registrations/{rid}/approve"), ("post", f"/admin/registrations/{rid}/reject"),
        ("post", f"/admin/registrations/{rid}/provision"), ("get", "/admin/registration-policies"),
        ("put", f"/admin/products/{h.sms}/registration-policy"),
    ]  # fmt: skip
    for m, path in calls:
        assert getattr(h.c, m)(path).status_code == 401, path


def test_admin_list_detail_filters_and_operator_read_access(h):
    a = post(h).json()
    b = post(h, body(h, contact_email="bob@example.com", product_ids=[str(h.email)])).json()
    for hd in (h.A, h.O):
        r = h.c.get("/admin/registrations", headers=hd)
        assert r.status_code == 200 and {x["id"] for x in r.json()} == {a["id"], b["id"]}
    f = lambda **q: {x["id"] for x in h.c.get("/admin/registrations", params=q, headers=h.A).json()}  # noqa: E731
    assert f(contact_email="BOB@example.com") == {b["id"]}
    assert f(product_id=str(h.email)) == {b["id"]}
    assert f(status="approved") == set() and f(status="submitted") == {a["id"], b["id"]}
    assert f(created_from="2999-01-01T00:00:00Z") == set()
    assert len(h.c.get("/admin/registrations", params={"limit": 1}, headers=h.A).json()) == 1
    assert h.c.get("/admin/registrations", params={"limit": 0}, headers=h.A).status_code == 422
    d = h.c.get(f"/admin/registrations/{a['id']}", headers=h.O).json()
    assert d["status"] == "submitted" and d["provisioning"] == {
        "status": None,
        "attempts": 0,
        "error_code": None,
    }
    assert [p["code"] for p in d["products"]] == ["sms"] and d["enterprise_id"] is None
    assert h.c.get(f"/admin/registrations/{uuid.uuid4()}", headers=h.A).status_code == 404


def test_operator_cannot_mutate_anything(h):
    rid = post(h).json()["id"]
    assert h.c.post(f"/admin/registrations/{rid}/approve", headers=h.O).status_code == 403
    assert (
        h.c.post(
            f"/admin/registrations/{rid}/reject", json={"reason": "x"}, headers=h.O
        ).status_code
        == 403
    )
    assert h.c.post(f"/admin/registrations/{rid}/provision", headers=h.O).status_code == 403
    r = h.c.put(
        f"/admin/products/{h.sms}/registration-policy",
        json={"self_registration_enabled": False},
        headers=h.O,
    )
    assert r.status_code == 403
    assert (
        rows(h)[0].status == "submitted"
        and rows(h, pol.ProductRegistrationPolicy)[0].self_registration_enabled
    )


def test_admin_approve_reject_provision_flow_and_retry_from_failed(h):
    rid = post(h, body(h, product_ids=[str(h.sms), str(h.email)])).json()["id"]
    r = h.c.post(f"/admin/registrations/{rid}/approve", headers=h.A)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert (
        r.json()["provisioning"]["status"] == "pending"
        and r.json()["decision"]["decided_by_user_id"]
    )
    assert h.c.post(f"/admin/registrations/{rid}/approve", headers=h.A).status_code == 200  # no-op
    assert count(h, AuditLog, AuditLog.action == "registration.approve") == 1
    assert (
        h.c.post(
            f"/admin/registrations/{rid}/reject", json={"reason": "late"}, headers=h.A
        ).status_code
        == 409
    )
    with h.factory() as s:  # provisioning dështon
        prod_svc.update(s, h.email, status="retired")
        s.commit()
    r = h.c.post(f"/admin/registrations/{rid}/provision", headers=h.A)
    assert r.status_code == 200 and (r.json()["result"], r.json()["error_code"]) == (
        "failed",
        "product_retired",
    )
    d = h.c.get(f"/admin/registrations/{rid}", headers=h.A).json()  # stafi sheh kodin e brendshëm
    assert d["provisioning"] == {"status": "failed", "attempts": 1, "error_code": "product_retired"}
    with h.factory() as s:
        prod_svc.update(s, h.email, status="active")
        s.commit()
    r = h.c.post(f"/admin/registrations/{rid}/provision", headers=h.A)
    j = r.json()
    assert (r.status_code, j["result"], j["attempts"], j["new_enterprise"]) == (
        200,
        "provisioned",
        2,
        True,
    )
    assert len(j["assignments"]) == 2 and j["enterprise_id"]
    again = h.c.post(f"/admin/registrations/{rid}/provision", headers=h.A).json()
    assert again["already_provisioned"] is True and again["attempts"] == 2
    d = h.c.get(f"/admin/registrations/{rid}", headers=h.A).json()
    assert d["provisioning"]["status"] == "provisioned" and d["enterprise_id"] == j["enterprise_id"]
    assert {p["assignment_status"] for p in d["products"]} == {"active"}
    assert count(h, Enterprise) == 1 and count(h, EnterpriseProduct) == 2


def test_provision_with_explicit_existing_enterprise_and_error_mapping(h):
    with h.factory() as s:
        from apps.central.services import enterprises

        eid = enterprises.create(s, "Existing").id
        s.commit()
    rid = post(h).json()["id"]
    assert (
        h.c.post(f"/admin/registrations/{rid}/provision", headers=h.A).status_code == 409
    )  # submitted
    h.c.post(f"/admin/registrations/{rid}/approve", headers=h.A)
    r = h.c.post(
        f"/admin/registrations/{rid}/provision",
        json={"enterprise_id": str(uuid.uuid4())},
        headers=h.A,
    )
    assert r.status_code == 404  # lidhje e pavlefshme = gabim hyrjeje, pa gjurmë
    assert (
        h.c.post(
            f"/admin/registrations/{rid}/provision", json={"enterprise_id": "x"}, headers=h.A
        ).status_code
        == 422
    )
    assert (
        h.c.post(
            f"/admin/registrations/{rid}/provision", json={"bogus": 1}, headers=h.A
        ).status_code
        == 422
    )
    r = h.c.post(
        f"/admin/registrations/{rid}/provision", json={"enterprise_id": str(eid)}, headers=h.A
    )
    assert (r.json()["result"], r.json()["enterprise_id"], r.json()["new_enterprise"]) == (
        "provisioned",
        str(eid),
        False,
    )
    assert count(h, Enterprise) == 1
    assert (
        h.c.post(f"/admin/registrations/{uuid.uuid4()}/provision", headers=h.A).status_code == 404
    )


def test_approve_does_not_accept_enterprise_id_linking_is_provision_only(h):
    rid = post(h).json()["id"]
    r = h.c.post(
        f"/admin/registrations/{rid}/approve",
        json={"enterprise_id": str(uuid.uuid4())},
        headers=h.A,
    )
    assert r.status_code in (200, 422)  # trupi i panjohur nuk ndryshon asgjë
    assert rows(h)[0].enterprise_id is None


def test_admin_reject_flow_and_validation(h):
    rid = post(h).json()["id"]
    assert h.c.post(f"/admin/registrations/{rid}/reject", json={}, headers=h.A).status_code == 422
    assert (
        h.c.post(
            f"/admin/registrations/{rid}/reject", json={"reason": "r", "x": 1}, headers=h.A
        ).status_code
        == 422
    )
    r = h.c.post(f"/admin/registrations/{rid}/reject", json={"reason": "not eligible"}, headers=h.A)
    assert (
        r.status_code == 200
        and r.json()["status"] == "rejected"
        and r.json()["decision"]["reason"] == "not eligible"
    )
    assert h.c.post(f"/admin/registrations/{rid}/approve", headers=h.A).status_code == 409
    assert h.c.post(f"/admin/registrations/{rid}/provision", headers=h.A).status_code == 409


def test_policy_api_list_update_and_closed_automatic_gate(h):
    r = h.c.get("/admin/registration-policies", headers=h.O)
    assert r.status_code == 200 and {p["product_code"] for p in r.json()} == {"sms", "email"}
    assert set(r.json()[0]) == {"product_id", "product_code", "product_name", "product_channel", "product_status",
                                "configured", "self_registration_enabled", "approval_mode", "created_at", "updated_at"}  # fmt: skip
    r = h.c.put(
        f"/admin/products/{h.sms}/registration-policy",
        json={"self_registration_enabled": False},
        headers=h.A,
    )
    assert r.status_code == 200 and r.json()["self_registration_enabled"] is False
    assert h.c.get("/registration/products").json()[0]["code"] == "email"
    for bad in ({}, {"approval_mode": None}, {"approval_mode": "x"}, {"other": 1}):
        assert (
            h.c.put(
                f"/admin/products/{h.sms}/registration-policy", json=bad, headers=h.A
            ).status_code
            == 422
        )
    r = h.c.put(
        f"/admin/products/{h.sms}/registration-policy",
        json={"approval_mode": "automatic"},
        headers=h.A,
    )
    assert r.status_code == 409  # gate i mbyllur
    assert (
        h.c.put(
            f"/admin/products/{uuid.uuid4()}/registration-policy",
            json={"approval_mode": "manual"},
            headers=h.A,
        ).status_code
        == 404
    )
    assert count(h, AuditLog, AuditLog.action.like("registration_policy.%")) >= 3


def test_no_delete_or_extra_methods_and_openapi_tags_are_separated(h):
    spec = create_app(h.eng).openapi()["paths"]
    reg_paths = {p: v for p, v in spec.items() if "registr" in p}
    assert reg_paths
    assert not [(p, m) for p, v in reg_paths.items() for m in v if m == "delete"]
    for p, v in reg_paths.items():
        tags = {t for op in v.values() for t in op["tags"]}
        assert tags == (
            {"registration-admin"} if p.startswith("/admin") else {"registration-public"}
        ), p
    assert h.c.delete(f"/admin/registrations/{uuid.uuid4()}", headers=h.A).status_code == 405
    schemas = create_app(h.eng).openapi()["components"]["schemas"]
    for name in ("SubmitOut", "StatusOut", "PublicProductOut"):
        assert not {
            "access_token_hash",
            "provisioning_error_code",
            "enterprise_id",
            "revision",
        } & set(schemas[name]["properties"])


# --- kufijtë ----------------------------------------------------------------------------------------------------


def test_api_layer_has_no_business_state_logic_or_enterprise_coupling():
    forbidden = {"httpx", "requests", "urllib", "app"}
    for f in ("registration_public.py", "registration_admin.py"):
        tree = ast.parse((ROOT / "apps/central/api" / f).read_text())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                mods.add(n.module.split(".")[0])
        assert not mods & forbidden, (f, mods & forbidden)
        src = (ROOT / "apps/central/api" / f).read_text()
        import re

        assert not re.search(r"\.(provisioning_)?status\s*=[^=]", src)  # asnjë kalim gjendjeje
        assert not any(w in src.lower() for w in ("price", "pricing", "sender_id", "country"))


def test_enterprise_never_called_outbox_is_the_only_channel(h):
    rid = post(h).json()["id"]
    h.c.post(f"/admin/registrations/{rid}/approve", headers=h.A)
    before = count(h, SyncOutbox)
    h.c.post(f"/admin/registrations/{rid}/provision", headers=h.A)
    assert count(h, SyncOutbox) - before == 2  # enterprise + një assignment (normal outbox)
