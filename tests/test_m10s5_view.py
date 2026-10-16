# ruff: noqa: F811
"""M10-S5 — modeli i leximit efektiv të sender-ave (klient/admin), lista pa N+1, detail pa rrjedhje ndër-tenant, ridërgimi nën central."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text

from app.models.messaging import ApprovalStatus
from app.models.sender_authority import SenderBootstrapIssue
from app.models.sender_request import SenderRequestOutbox
from app.services import sender_ids as sid
from tests.test_m10s4_authority import (  # noqa: F401
    _reset,
    count_sql,
    eid_of,
    fresh,
    mode,
    policy,
    project,
)

ADMIN_ONLY = {"drift", "cp_revision", "policy_revision", "bootstrap_issues"}


def cust(client, owner="c1"):
    r = client.post(
        "/v1/admin/api-keys", json={"name": owner, "role": "client", "owner_ref": owner}
    ).json()
    return {"Authorization": f"Bearer {r['key']}", "X-Admin-Key": ""}


def mk(db, value, status="pending", owner="c1"):
    s = sid.request(db, owner, "AL", value)
    if status == "approved":
        sid.approve(db, s.id, "staff")
    elif status in ("rejected", "revoked"):
        if status == "revoked":
            sid.approve(db, s.id, "staff")
            sid.revoke(db, s.id, "staff", "x")
        else:
            sid.reject(db, s.id, "staff", "x")
    db.commit()
    return s


def row_for(items, sender):
    return next(i for i in items if i["id"] == sender.id)


def test_local_mode_effective_status_is_local_and_adds_no_central_diagnostics(
    db, client, monkeypatch
):
    s = mk(db, "LOCAL1", "approved")
    r = client.get("/v1/sender-ids", headers=cust(client))
    assert r.status_code == 200
    it = row_for(r.json(), s)
    assert (it["status"], it["effective_status"], it["authority_mode"], it["sync_status"]) == (
        "approved",
        "approved",
        "local",
        "not_applicable",
    )
    assert (
        it["central_status"] is None
        and it["can_review_locally"] is True
        and not ADMIN_ONLY & {k for k, v in it.items() if v is not None}
    )


def test_shadow_customer_sees_local_but_admin_sees_central_and_drift(db, client, monkeypatch):
    s = mk(db, "SHAD01", "approved")
    project(db, "SHAD01", "revoked")
    fresh(db)
    mode(monkeypatch, "shadow")
    c = row_for(client.get("/v1/sender-ids", headers=cust(client)).json(), s)
    assert c["effective_status"] == "approved" and c["central_status"] is None and "drift" not in c
    a = row_for(client.get("/v1/sender-ids").json(), s)  # admin (X-Admin-Key)
    assert (
        a["effective_status"] == "approved"
        and a["central_status"] == "revoked"
        and a["drift"] == "central_revoked"
    )
    assert a["sync_status"] == "synchronized" and a["cp_revision"] == 3


@pytest.mark.parametrize(
    "local,central,eff",
    [
        ("pending", "approved", "approved"),
        ("approved", "revoked", "revoked"),
        ("approved", "rejected", "rejected"),
        ("approved", "pending", "pending"),
    ],
)
def test_central_mode_effective_status_comes_from_the_projection(
    db, client, monkeypatch, local, central, eff
):
    s = mk(db, "CENT01", local)
    project(db, "CENT01", central)
    fresh(db)
    mode(monkeypatch, "central")
    it = row_for(client.get("/v1/sender-ids", headers=cust(client)).json(), s)
    assert (it["status"], it["effective_status"], it["central_status"], it["sync_status"]) == (
        local,
        eff,
        central,
        "synchronized",
    )
    assert it["can_review_locally"] is False and it["authority_mode"] == "central"
    db.expire_all()
    assert s.status.value == local  # SenderId.status NUK ndryshohet kurrë


def test_central_mode_missing_projection_is_never_shown_as_approved(db, client, monkeypatch):
    a, b, c = (
        mk(db, "MISS01", "approved"),
        mk(db, "MISS02", "pending"),
        mk(db, "MISS03", "rejected"),
    )
    fresh(db)
    mode(monkeypatch, "central")
    items = client.get("/v1/sender-ids", headers=cust(client)).json()
    assert (
        row_for(items, a)["effective_status"] == "not_synchronized"
        and row_for(items, a)["sync_status"] == "not_synchronized"
    )
    assert row_for(items, b)["effective_status"] == "not_synchronized"
    assert row_for(items, c)["effective_status"] == "rejected"  # mohim edhe pa Central


def test_central_mode_policy_denial_and_stale_sync_are_visible(db, client, monkeypatch):
    s = mk(db, "POLC01", "approved")
    project(db, "POLC01", "approved")
    policy(db, allowed=False)
    fresh(db, datetime(2020, 1, 1, tzinfo=UTC))
    mode(monkeypatch, "central")
    it = row_for(client.get("/v1/sender-ids", headers=cust(client)).json(), s)
    assert (
        it["effective_status"] == "policy_denied"
        and it["sync_status"] == "stale"
        and it["can_resubmit"] is False
    )


def test_detail_endpoint_customer_vs_admin_and_no_cross_tenant_leak(db, client, monkeypatch):
    s = mk(db, "DET001", "pending")
    project(db, "DET001", "approved")
    db.add(
        SenderBootstrapIssue(
            sender_id=s.id,
            enterprise_id=s.enterprise_id,
            category="policy_denied",
            identity_hash="0" * 16,
        )
    )
    db.commit()
    other = mk(db, "OTHR01", "approved", owner="c2")
    fresh(db)
    mode(monkeypatch, "central")
    h1, h2 = cust(client, "c1"), cust(client, "c2")
    d = client.get(f"/v1/sender-ids/{s.id}", headers=h1)
    assert (
        d.status_code == 200
        and d.json()["effective_status"] == "approved"
        and d.json()["status"] == "pending"
    )
    assert (
        d.json()["drift"] is None and d.json()["bootstrap_issues"] is None
    )  # klienti s'sheh diagnostikën
    assert client.get(f"/v1/sender-ids/{s.id}", headers=h2).status_code == 404  # tenant tjetër
    a = client.get(f"/v1/sender-ids/{s.id}").json()
    assert (
        a["central_status"] == "approved"
        and a["drift"] == "local_deny_central_allow"
        and a["bootstrap_issues"] == ["policy_denied"]
        and a["cp_revision"] == 3
    )
    assert client.get("/v1/sender-ids/999999").status_code == 404
    assert all(i["id"] != other.id for i in client.get("/v1/sender-ids", headers=h1).json())


def test_list_has_no_n_plus_one_and_central_costs_are_constant(db, client, monkeypatch):
    for i in range(5):
        mk(db, f"LST0{i}A", "approved")
    project(db, "LST00A", "approved")
    fresh(db)
    mode(monkeypatch, "central")
    h = cust(client)
    from app.services import sender_authority as sau

    client.get("/v1/sender-ids", headers=h)  # ngroh cache-t (auth, freskia)
    sau.projection_stale(db)
    small = len(count_sql(lambda: client.get("/v1/sender-ids", headers=h)))
    mode(monkeypatch, "local")
    for i in range(25):
        mk(db, f"LSTX{i:02d}", "approved")
    mode(monkeypatch, "central")
    sau.projection_stale(db)
    big = len(count_sql(lambda: client.get("/v1/sender-ids", headers=h)))
    admin = len(count_sql(lambda: client.get("/v1/sender-ids")))
    print(f"\n[sql] sender list 5={small} 30={big} admin30={admin}")
    assert small == big and admin - big <= 3
    mode(monkeypatch, "local")
    local_small = len(count_sql(lambda: client.get("/v1/sender-ids", headers=h)))
    assert big - local_small <= 3  # shtesa e central: projeksion + politika (+ freskia nga cache)


def test_existing_api_shape_is_preserved_and_new_fields_are_additive(db, client):
    r = client.post(
        "/v1/sender-ids",
        json={"owner_ref": "c1", "country": "AL", "value": "COMPAT"},
        headers=cust(client),
    )
    j = r.json()
    assert r.status_code == 201 and {
        "id",
        "owner_ref",
        "country",
        "value",
        "kind",
        "status",
        "reason",
    } <= set(j)
    assert (
        j["status"] == "pending"
        and j["effective_status"] == "pending"
        and j["authority_mode"] == "local"
    )


def test_central_mode_customer_can_resubmit_a_sender_central_revoked_even_if_local_status_is_approved(
    db, client, monkeypatch
):
    s = mk(db, "RESUB1", "approved")
    project(db, "RESUB1", "revoked")
    fresh(db)
    mode(monkeypatch, "central")
    h = cust(client)
    assert row_for(client.get("/v1/sender-ids", headers=h).json(), s)["can_resubmit"] is True
    n0 = db.scalar(select(func.count()).select_from(SenderRequestOutbox))
    r = client.post(
        "/v1/sender-ids", json={"owner_ref": "c1", "country": "AL", "value": "RESUB1"}, headers=h
    )
    assert r.status_code == 201 and r.json()["status"] == "pending"
    db.expire_all()
    assert (
        s.status == ApprovalStatus.PENDING and s.approved_key is None
    )  # pending lokal ≠ autorizim
    assert db.scalar(select(func.count()).select_from(SenderRequestOutbox)) == n0 + 1
    # autoriteti mbetet Central (revoked) derisa të vijë cp.sender.v1
    assert r.json()["effective_status"] == "revoked"
    dec = db.execute(
        text("select decision, reason from sms_sender_decisions order by id desc limit 1")
    ).one()
    assert dec[0] == "resubmitted" and "central_status:revoked" in dec[1]


def test_local_review_still_works_in_local_and_shadow_through_the_api(db, client, monkeypatch):
    s = mk(db, "REVW01", "pending")
    for m in ("local", "shadow"):
        mode(monkeypatch, m)
        r = client.post(
            f"/v1/sender-ids/{s.id}/{'approve' if m == 'local' else 'revoke'}",
            json={} if m == "local" else {"reason": "x"},
        )
        assert r.status_code == 200, r.text
    mode(monkeypatch, "central")
    assert client.post(f"/v1/sender-ids/{s.id}/reject", json={"reason": "x"}).status_code == 409
    assert eid_of(db)
