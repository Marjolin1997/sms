from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from app.models.rates import Rate, RateFrozenError
from app.services import rates as svc
from app.services.sms_text import count_segments
from app.services.wallet import Conflict

T0 = datetime(2030, 1, 1, tzinfo=UTC)
NOW = datetime(2029, 12, 1, tzinfo=UTC)


def card_v1(db, rates=(("355", "0.05", ""), ("35569", "0.04", ""), ("35569", "0.03", "27601"))):
    c = svc.create_card(db, "std", "eur")
    v = svc.new_draft(db, c.id)
    for prefix, price, op in rates:
        svc.set_rate(db, v.id, prefix, price, op)
    svc.publish(db, v.id, T0, now=NOW)
    db.commit()
    return c, v


def test_longest_prefix_and_operator(db):
    c, v = card_v1(db)
    at = T0 + timedelta(days=1)
    assert svc.quote(db, c.id, "+35568123456", "hi", at).unit_price == D("0.05")
    assert svc.quote(db, c.id, "+35569123456", "hi", at).unit_price == D("0.04")
    assert svc.quote(db, c.id, "+35569123456", "hi", at, operator="27601").unit_price == D("0.03")
    with pytest.raises(svc.NoRate):
        svc.quote(db, c.id, "+4915112345678", "hi", at)


def test_versions_do_not_touch_past(db):
    c, v1 = card_v1(db)
    v2 = svc.new_draft(db, c.id)
    svc.set_rate(db, v2.id, "355", "0.09")
    svc.publish(db, v2.id, T0 + timedelta(days=10), now=NOW)
    db.commit()
    n = "+35568123456"
    before = svc.quote(db, c.id, n, "hi", T0 + timedelta(days=5))
    after = svc.quote(db, c.id, n, "hi", T0 + timedelta(days=10))
    assert (before.unit_price, before.version_id) == (D("0.05"), v1.id)
    assert (after.unit_price, after.version_id) == (D("0.09"), v2.id)
    # v2 e ka kopjuar tarifën e panjohur nga v1
    assert svc.quote(db, c.id, "+35569123456", "hi", T0 + timedelta(days=11)).unit_price == D(
        "0.04"
    )


def test_no_version_before_effective(db):
    c, _ = card_v1(db)
    with pytest.raises(svc.NoRate):
        svc.quote(db, c.id, "+35568123456", "hi", T0 - timedelta(seconds=1))


def test_published_is_immutable(db):
    c, v = card_v1(db)
    with pytest.raises(Conflict):
        svc.set_rate(db, v.id, "355", "1")
    r = db.query(Rate).first()
    r.price_per_segment = D("9")
    with pytest.raises(RateFrozenError):
        db.flush()
    db.rollback()


def test_publish_rules(db):
    c, v = card_v1(db)
    v2 = svc.new_draft(db, c.id)
    with pytest.raises(Conflict):  # draft i dytë
        svc.new_draft(db, c.id)
    with pytest.raises(Conflict):  # në të kaluarën
        svc.publish(db, v2.id, NOW - timedelta(days=1), now=NOW)
    with pytest.raises(Conflict):  # jo pas versionit të mëparshëm
        svc.publish(db, v2.id, T0, now=NOW)
    svc.publish(db, v2.id, T0 + timedelta(days=1), now=NOW)


def test_empty_version_and_bad_input(db):
    c = svc.create_card(db, "x", "EUR")
    v = svc.new_draft(db, c.id)
    with pytest.raises(Conflict):
        svc.publish(db, v.id, T0, now=NOW)
    with pytest.raises(svc.InvalidNumber):
        svc.set_rate(db, v.id, "+355", "1")
    with pytest.raises(svc.InvalidAmount):
        svc.set_rate(db, v.id, "355", 0.1)
    with pytest.raises(svc.InvalidAmount):
        svc.set_rate(db, v.id, "355", "-1")


def test_invalid_number(db):
    c, _ = card_v1(db)
    with pytest.raises(svc.InvalidNumber):
        svc.quote(db, c.id, "0691234567", "hi", T0)


def test_total_is_segments_times_price(db):
    c, _ = card_v1(db)
    q = svc.quote(db, c.id, "+35569123456", "a" * 161, T0)
    assert (q.segments, q.total, q.currency) == (2, D("0.080000"), "EUR")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a" * 160, ("gsm7", 1)),
        ("a" * 161, ("gsm7", 2)),
        ("a" * 306, ("gsm7", 2)),
        ("a" * 307, ("gsm7", 3)),
        ("€" * 80, ("gsm7", 1)),  # 160 njësi
        ("€" * 81, ("gsm7", 2)),
        ("ç", ("ucs2", 1)),
        ("ç" * 70, ("ucs2", 1)),
        ("ç" * 71, ("ucs2", 2)),
        ("ç" * 135, ("ucs2", 3)),
        ("😀" * 35, ("ucs2", 1)),
        ("😀" * 36, ("ucs2", 2)),
    ],
)
def test_segments(text, expected):
    assert count_segments(text) == expected


def test_api_flow(client):
    c = client.post("/v1/rate-cards", json={"name": "api", "currency": "EUR"}).json()
    v = client.post(f"/v1/rate-cards/{c['id']}/versions").json()
    r = client.put(
        f"/v1/rate-card-versions/{v['id']}/rates",
        json={"prefix": "355", "price_per_segment": "0.05"},
    )
    assert r.status_code == 200
    eff = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert (
        client.post(
            f"/v1/rate-card-versions/{v['id']}/publish", json={"effective_from": eff}
        ).status_code
        == 200
    )
    q = {
        "number": "+35569123456",
        "text": "hi",
        "at": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
    }
    out = client.post(f"/v1/rate-cards/{c['id']}/quote", json=q).json()
    assert out["total"] == "0.050000"
    assert (
        client.put(
            f"/v1/rate-card-versions/{v['id']}/rates",
            json={"prefix": "355", "price_per_segment": "1"},
        ).status_code
        == 409
    )
