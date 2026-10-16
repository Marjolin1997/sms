# ruff: noqa: F811
"""M11-F — API vetëm-lexim për UX-in e mesazheve: campaign list/detail/recipients/activity, filtra mesazhesh, numërues listash."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from app.models.sending import Message, MessageStatus
from app.services import contacts as contacts_svc
from tests.test_billing import _key, fresh_gateway, raw_client  # noqa: F401
from tests.test_campaigns import audience, campaign, drive, start  # noqa: F401
from tests.test_pipeline import fake, world  # noqa: F401

NOW = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def env(db, world, raw_client):  # noqa: F811
    c = raw_client
    h1, h2 = _key(c, "client", "c1"), _key(c, "client", "c2")
    return SimpleNamespace(c=c, h1=h1, h2=h2, db=db)


def run_campaign(db, name, n=2, base=355691231010, **kw):
    lst, ids = audience(db, n, base=base)
    camp = campaign(db, lst, name=name, **kw)
    start(db, camp)
    drive(db)
    db.refresh(camp)
    return camp, lst, ids


def selects(db, fn):
    n = []
    listener = lambda conn, cur, stmt, *a, n=n: n.append(stmt)  # noqa: E731
    bind = db.get_bind()
    event.listen(bind, "before_cursor_execute", listener)
    try:
        fn()
    finally:
        event.remove(bind, "before_cursor_execute", listener)
    return len(n)


def test_campaign_list_is_backward_compatible_with_additive_counts(env):
    camp, *_ = run_campaign(env.db, "promo", n=3)
    r = env.c.get("/v1/campaigns", headers=env.h1)
    assert r.status_code == 200 and isinstance(r.json(), list)
    item = r.json()[0]
    base = {"id", "name", "channel", "status", "list_id", "sender", "category", "template_id", "max_cost", "rate_per_minute",
            "scheduled_at", "started_at", "completed_at", "pause_reason"}  # fmt: skip
    assert base <= set(item) and set(item) - base == {
        "recipients_total",
        "recipient_counts",
        "message_counts",
    }
    assert (
        item["recipients_total"] == 3
        and item["recipient_counts"] == {"queued": 3}
        and item["message_counts"] == {"queued": 3}
    )


def test_campaign_list_filters_and_keyset_pagination(env):
    names = ["alpha sale", "beta sale", "gamma club"]
    made = [run_campaign(env.db, n, base=355691231010 + 100 * i)[0] for i, n in enumerate(names)]
    g = lambda **p: env.c.get("/v1/campaigns", params=p, headers=env.h1).json()  # noqa: E731
    assert [c["name"] for c in g()] == ["gamma club", "beta sale", "alpha sale"]  # id DESC
    assert [c["name"] for c in g(q="sale")] == ["beta sale", "alpha sale"]
    assert [c["name"] for c in g(q="SALE")] == ["beta sale", "alpha sale"]
    assert g(q="%") == [] and g(q="_") == []  # pa wildcard
    assert len(g(status="completed")) == 3 and g(status="draft") == []
    assert env.c.get("/v1/campaigns", params={"status": "nope"}, headers=env.h1).status_code == 422
    page1 = g(limit=2)
    assert [c["name"] for c in page1] == ["gamma club", "beta sale"]
    page2 = g(limit=2, before_id=page1[-1]["id"])
    assert [c["name"] for c in page2] == ["alpha sale"]
    assert made[0].id == page2[0]["id"]


def test_campaign_list_query_count_does_not_grow_with_rows(env):
    for i in range(4):
        run_campaign(env.db, f"c{i}", base=355691240000 + 100 * i)
    env.c.get("/v1/campaigns", headers=env.h1)  # ngrohje (auth/cache)
    one = selects(env.db, lambda: env.c.get("/v1/campaigns", params={"limit": 1}, headers=env.h1))
    four = selects(env.db, lambda: env.c.get("/v1/campaigns", params={"limit": 4}, headers=env.h1))
    assert one == four  # agregate të grupuara për faqen, jo N+1


def test_detail_has_the_extra_read_only_fields(env):
    camp, lst, _ = run_campaign(env.db, "promo")
    d = env.c.get(f"/v1/campaigns/{camp.id}", headers=env.h1).json()
    assert (
        d["text"] == "Big sale!"
        and d["list_name"] == lst.name
        and d["created_at"]
        and d["utc_offset_minutes"] == 0
    )
    assert d["stats"]["recipients"] == {"queued": 2}


def test_recipients_are_enriched_without_per_row_queries(env):
    camp, _, ids = run_campaign(env.db, "promo", n=3)
    rows = env.c.get(f"/v1/campaigns/{camp.id}/recipients", headers=env.h1).json()
    assert len(rows) == 3
    r = rows[0]
    assert {
        "id",
        "contact_id",
        "status",
        "reason",
        "message_id",
        "address",
        "name",
        "message_public_id",
        "message_status",
        "error_code",
    } <= set(r)
    assert r["name"] == "N0" and r["message_status"] == "queued" and r["message_public_id"]
    env.c.get(f"/v1/campaigns/{camp.id}/recipients", headers=env.h1)
    few = selects(
        env.db,
        lambda: env.c.get(
            f"/v1/campaigns/{camp.id}/recipients", params={"limit": 1}, headers=env.h1
        ),
    )
    many = selects(
        env.db,
        lambda: env.c.get(
            f"/v1/campaigns/{camp.id}/recipients", params={"limit": 3}, headers=env.h1
        ),
    )
    assert few == many
    nxt = env.c.get(
        f"/v1/campaigns/{camp.id}/recipients",
        params={"after_id": rows[0]["id"], "limit": 1},
        headers=env.h1,
    ).json()
    assert nxt[0]["id"] == rows[1]["id"]
    assert (
        env.c.get(
            f"/v1/campaigns/{camp.id}/recipients", params={"status": "skipped"}, headers=env.h1
        ).json()
        == []
    )


def test_unknown_provider_state_is_never_shown_as_failed(env):
    camp, *_ = run_campaign(env.db, "promo", n=1)
    m = env.db.query(Message).one()
    m.status = MessageStatus.UNKNOWN
    env.db.commit()
    r = env.c.get(f"/v1/campaigns/{camp.id}/recipients", headers=env.h1).json()[0]
    assert r["message_status"] == "sending"
    assert env.c.get("/v1/campaigns", headers=env.h1).json()[0]["message_counts"] == {"sending": 1}
    assert env.c.get("/v1/messages", headers=env.h1).json()["items"][0]["status"] == "sending"


def test_activity_lists_only_real_audited_events_and_hides_the_actor(env):
    camp, *_ = run_campaign(env.db, "promo")
    c = env.c
    camp2 = c.post(
        "/v1/campaigns",
        json={"name": "api made", "list_id": camp.list_id, "sender": "ACME", "text": "hi"},
        headers=env.h1,
    ).json()
    assert (
        c.post(f"/v1/campaigns/{camp2['id']}/schedule", json={}, headers=env.h1).status_code == 200
    )
    ev = c.get(f"/v1/campaigns/{camp2['id']}/activity", headers=env.h1).json()
    assert [e["action"] for e in ev] == ["campaign.schedule", "campaign.create"]
    assert all(set(e) == {"action", "at", "by", "detail"} and e["by"] == "you" for e in ev)
    assert "key:" not in str(ev)
    assert (
        c.get(f"/v1/campaigns/{camp.id}/activity", headers=env.h1).json() == []
    )  # krijuar jashtë API: s'shpikim histori


def test_tenant_scoping_for_every_new_view(env):
    camp, *_ = run_campaign(env.db, "promo")
    for path in ("", "/recipients", "/activity", "/estimate"):
        assert env.c.get(f"/v1/campaigns/{camp.id}{path}", headers=env.h2).status_code == 404, path
    assert env.c.get("/v1/campaigns", headers=env.h2).json() == []
    assert (
        env.c.get("/v1/messages", params={"campaign_id": camp.id}, headers=env.h2).json()["items"]
        == []
    )
    assert env.c.get("/v1/lists", headers=env.h2).json() == []


def test_message_filters_compose_and_validate(env):
    camp, *_ = run_campaign(env.db, "promo", n=2)
    other, *_ = run_campaign(env.db, "other", n=1, base=355691250000)
    db = env.db
    msgs = db.query(Message).order_by(Message.id).all()
    msgs[0].created_at = datetime(2030, 5, 31, 23, 59, tzinfo=UTC)
    msgs[1].created_at = datetime(2030, 6, 1, 0, 0, tzinfo=UTC)
    msgs[2].created_at = datetime(2030, 6, 2, 8, 0, tzinfo=UTC)
    db.commit()
    g = lambda **p: [
        m["to"] for m in env.c.get("/v1/messages", params=p, headers=env.h1).json()["items"]
    ]  # noqa: E731
    assert len(g()) == 3
    assert len(g(campaign_id=camp.id)) == 2 and len(g(campaign_id=other.id)) == 1
    assert g(campaign_id=999999) == []
    assert len(g(sender="ACME")) == 3 and g(sender="NOPE") == []
    assert len(g(created_from="2030-06-01")) == 2  # 00:00:00Z përfshihet
    assert len(g(created_to="2030-05-31")) == 1  # 23:59Z e ditës 31 përfshihet, 00:00Z e 1 jo
    assert len(g(created_from="2030-06-01", created_to="2030-06-01")) == 1
    assert len(g(campaign_id=camp.id, created_from="2030-06-01")) == 1
    r = env.c.get(
        "/v1/messages",
        params={"created_from": "2030-06-03", "created_to": "2030-06-01"},
        headers=env.h1,
    )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid"
    assert (
        env.c.get("/v1/messages", params={"created_from": "garbage"}, headers=env.h1).status_code
        == 422
    )
    first = env.c.get("/v1/messages", params={"limit": 2}, headers=env.h1).json()
    assert len(first["items"]) == 2 and first["next_before_id"]


def test_lists_have_server_side_member_counts(env):
    db = env.db
    a = contacts_svc.create_list(db, "c1", "VIP")
    contacts_svc.create_list(db, "c1", "Empty")
    ids = [contacts_svc.upsert(db, "c1", phone=f"3556912399{i:02d}")[0].id for i in range(3)]
    contacts_svc.add_members(db, "c1", a.id, ids)
    db.commit()
    got = {x["name"]: x for x in env.c.get("/v1/lists", headers=env.h1).json()}
    assert (
        got["VIP"]["member_count"] == 3
        and got["Empty"]["member_count"] == 0
        and got["VIP"]["created_at"]
    )
    assert {"id", "name"} <= set(got["VIP"])
    env.c.get("/v1/lists", headers=env.h1)
    one = selects(db, lambda: env.c.get("/v1/lists", headers=env.h1))
    contacts_svc.create_list(db, "c1", "Third")
    db.commit()
    assert selects(db, lambda: env.c.get("/v1/lists", headers=env.h1)) == one


def test_no_new_mutation_routes(env):
    spec = env.c.app.openapi()["paths"]
    assert set(spec["/v1/campaigns/{campaign_id}/activity"]) == {"get"}
    assert set(spec["/v1/campaigns/{campaign_id}/recipients"]) == {"get"}


def test_sms_hot_path_modules_do_not_import_the_new_views():
    import pathlib

    for f in ("messages", "sender_authority", "wallet", "campaigns", "dispatch_queue"):
        p = pathlib.Path(f"app/services/{f}.py")
        if p.exists():
            assert "campaign_views" not in p.read_text(), f
