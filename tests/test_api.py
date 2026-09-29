def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_auth_required(client):
    assert client.post("/v1/wallets", json={}, headers={"X-Admin-Key": "bad"}).status_code == 401


def test_topup_flow(client):
    w = client.post("/v1/wallets", json={"owner_ref": "c1", "currency": "eur"}).json()
    t = client.post(
        f"/v1/wallets/{w['id']}/topups",
        json={"amount": "12.34", "method": "cash", "external_ref": "inv-1"},
    )
    assert t.status_code == 201
    for _ in range(2):
        assert client.post(f"/v1/topups/{t.json()['id']}/confirm").json()["status"] == "confirmed"
    got = client.get(f"/v1/wallets/{w['id']}").json()
    assert got["available"] == "12.340000" and got["held"] == "0.000000"
    ledger = client.get(f"/v1/wallets/{w['id']}/ledger").json()
    assert len(ledger) == 1 and ledger[0]["entry_type"] == "topup"


def test_bad_amount_rejected(client):
    w = client.post("/v1/wallets", json={"owner_ref": "c", "currency": "EUR"}).json()
    r = client.post(f"/v1/wallets/{w['id']}/topups", json={"amount": "-5", "method": "cash"})
    assert r.status_code == 422
    assert client.post("/v1/topups/999/confirm").status_code == 404
