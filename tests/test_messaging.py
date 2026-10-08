import pytest

from app.models.messaging import ApprovalStatus as S
from app.models.messaging import TemplateBodyFrozenError, TemplateVersion
from app.services import sender_ids as sid
from app.services import templates as tpl
from app.services.wallet import Conflict


def approved_sender(db, owner="c1", country="AL", value="ACME"):
    s = sid.request(db, owner, country, value)
    sid.approve(db, s.id, "admin")
    db.commit()
    return s


def test_sender_validation(db):
    for bad in ("AB", "TOOLONGSENDER1", "12345678901234567890", "a_b_c", "123abc!", " ACME "):
        with pytest.raises(sid.InvalidSender):
            sid.request(db, "c1", "AL", bad)
    with pytest.raises(sid.InvalidSender):
        sid.request(db, "c1", "ALB", "ACME")
    assert sid.request(db, "c1", "al", "+355691234567").value == "355691234567"


def test_sender_lifecycle_and_usage(db):
    s = sid.request(db, "c1", "AL", "ACME")
    with pytest.raises(sid.SenderNotAllowed):  # ende pending
        sid.assert_usable(db, "c1", "AL", "ACME")
    sid.approve(db, s.id, "admin")
    assert sid.assert_usable(db, "c1", "al", "ACME").id == s.id
    with pytest.raises(sid.SenderNotAllowed):  # shtet tjetër
        sid.assert_usable(db, "c1", "XK", "ACME")
    with pytest.raises(sid.SenderNotAllowed):  # klient tjetër
        sid.assert_usable(db, "c2", "AL", "ACME")
    with pytest.raises(Conflict):
        sid.approve(db, s.id, "admin")
    with pytest.raises(Conflict):  # revoke kërkon arsye
        sid.revoke(db, s.id, "admin", "")
    sid.revoke(db, s.id, "admin", "abuse")
    with pytest.raises(sid.SenderNotAllowed):
        sid.assert_usable(db, "c1", "AL", "ACME")
    assert sid.request(db, "c1", "AL", "ACME").status == S.PENDING  # resubmit


def test_same_sender_cannot_be_approved_for_two_owners(db):
    approved_sender(db, "c1")
    other = sid.request(db, "c2", "AL", "acme")  # rasa nuk e shmang
    with pytest.raises(Conflict):
        sid.approve(db, other.id, "admin")


def test_reject_needs_reason_and_actor(db):
    s = sid.request(db, "c1", "AL", "ACME")
    with pytest.raises(Conflict):
        sid.reject(db, s.id, "admin", "")
    with pytest.raises(Conflict):
        sid.approve(db, s.id, "")
    sid.reject(db, s.id, "admin", "brand not verified")
    assert s.status == S.REJECTED and s.reviewed_by == "admin"


def test_template_syntax():
    assert tpl.variables("Hi {{name}}, code {{code}} {{name}}") == ["name", "code"]
    for bad in ("", "Hi {{Name}}", "Hi {{ name }}", "Hi {{name", "x }} y", "{{a{{b}}}}"):
        with pytest.raises(tpl.InvalidTemplate):
            tpl.variables(bad)


def test_template_version_flow(db):
    v1 = tpl.create(db, "c1", "otp", "Code {{code}}")
    tid = v1.template_id
    with pytest.raises(tpl.TemplateNotUsable):
        tpl.render(db, "c1", tid, {"code": "1"})  # pending
    tpl.review(db, v1.id, "approve", "admin")
    assert tpl.render(db, "c1", tid, {"code": "123456"}).text == "Code 123456"
    v2 = tpl.new_version(db, tid, "Your code {{code}}")
    assert tpl.render(db, "c1", tid, {"code": "1"}).text == "Code 1"  # v2 pending → v1
    tpl.review(db, v2.id, "approve", "admin")
    assert tpl.render(db, "c1", tid, {"code": "1"}).version_id == v2.id
    tpl.review(db, v2.id, "revoke", "admin", "policy")
    assert tpl.render(db, "c1", tid, {"code": "1"}).version_id == v1.id
    with pytest.raises(tpl.TemplateNotUsable):  # klient tjetër
        tpl.render(db, "c2", tid, {"code": "1"})


def test_render_validation(db):
    v = tpl.create(db, "c1", "t", "Hi {{name}}")
    tpl.review(db, v.id, "approve", "admin")
    t = v.template_id
    for bad in ({}, {"name": "a", "x": "b"}, {"name": "a" * 161}, {"name": "a\x00b"}):
        with pytest.raises(tpl.InvalidTemplate):
            tpl.render(db, "c1", t, bad)
    # vlera nuk rizgjerohet si placeholder
    assert tpl.render(db, "c1", t, {"name": "{{name}}"}).text == "Hi {{name}}"


def test_duplicate_template_name(db):
    tpl.create(db, "c1", "t", "a")
    with pytest.raises(Conflict):
        tpl.create(db, "c1", "t", "b")
    tpl.create(db, "c2", "t", "b")


def test_body_frozen(db):
    v = tpl.create(db, "c1", "t", "a")
    db.commit()
    v.body = "changed"
    with pytest.raises(TemplateBodyFrozenError):
        db.flush()
    db.rollback()
    assert db.get(TemplateVersion, v.id).body == "a"


def test_api_flow(client):
    s = client.post("/v1/sender-ids", json={"owner_ref": "c1", "country": "AL", "value": "ACME"})
    assert s.status_code == 201 and s.json()["status"] == "pending"
    # M10-S0: skemat janë `extra=forbid`; aktori vjen nga principali, jo nga trupi
    url = f"/v1/sender-ids/{s.json()['id']}"
    assert client.post(f"{url}/approve", json={"actor": "root"}).status_code == 422
    r = client.post(f"{url}/approve", json={})
    assert r.json()["status"] == "approved"
    assert client.post(f"{url}/reject", json={"reason": "r"}).status_code == 409
    assert (
        client.post(
            "/v1/sender-ids", json={"owner_ref": "c", "country": "AL", "value": "x"}
        ).status_code
        == 422
    )
    t = client.post(
        "/v1/templates", json={"owner_ref": "c1", "name": "otp", "body": "Code {{code}}"}
    ).json()
    assert t["variables"] == ["code"]
    url = f"/v1/templates/{t['template_id']}/render"
    assert client.post(url, json={"owner_ref": "c1", "values": {"code": "1"}}).status_code == 403
    client.post(f"/v1/template-versions/{t['id']}/approve", json={"actor": "root"})
    out = client.post(url, json={"owner_ref": "c1", "values": {"code": "42"}}).json()
    assert out["text"] == "Code 42" and out["segments"] == 1
