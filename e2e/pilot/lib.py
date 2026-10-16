"""Ndihmës të përbashkët për skriptet M11-H (httpx kundër uvicorn REAL; pa TestClient). Pa sekrete në dalje."""

import hashlib
import hmac
import json
import os
import time

import httpx

from e2e.pilot import guard

guard.assert_fake_only()  # dështon shpejt nëse konfigurimi lejon dërgim të vërtetë
PILOT_DIR = os.environ["PILOT_DIR"]
IDS = json.load(open(os.path.join(PILOT_DIR, "ids.json")))
ENT, CEN = "http://127.0.0.1:8100", "http://127.0.0.1:8101"
passed = 0


def check(cond, msg):
    global passed
    if not cond:
        raise AssertionError(msg)
    passed += 1
    print(f"  ok  {msg}")


class Api:
    def __init__(self, base, key=None, token=None):
        self.base = base
        self.h = {"Authorization": f"Bearer {key or token}"} if (key or token) else {}
        self.c = httpx.Client(base_url=base, timeout=60)

    def req(self, method, path, **kw):
        h = {**self.h, **kw.pop("headers", {})}
        r = self.c.request(method, path, headers=h, **kw)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        return r.status_code, body

    def get(self, path, **params):
        return self.req("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path, body=None, **kw):
        return self.req("POST", path, json=body if body is not None else {}, **kw)

    def raw(self, path, **params):
        return self.c.get(path, params=params, headers=self.h)


def tenant(tag):
    t = IDS["tenants"][tag]
    return Api(ENT, t["key"]), t


def staff(role):
    return Api(ENT, IDS["keys"][role])


def central(email_env):
    c = Api(CEN)
    pw = os.environ[email_env]
    email = "admin@pilot.test" if "ADMIN" in email_env else "operator@pilot.test"
    st, body = c.post("/auth/token", {"email": email, "password": pw})
    assert st == 200, (st, body)
    return Api(CEN, token=body["access_token"])


def wait(cond, what, timeout=60, step=0.5):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = cond()
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"timeout waiting for: {what}")


def central_sender(admin, value):
    st, body = admin.get("/admin/senders", limit=200)
    return (
        next((s for s in body["items"] if s["display_value"] == value), None) if st == 200 else None
    )


def dlr(provider_message_id, status="delivered", code=None, secret="pilot-dlr-secret"):
    raw = json.dumps(
        {
            "provider_message_id": provider_message_id,
            "status": status,
            **({"code": code} if code else {}),
        }
    ).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    r = httpx.post(
        f"{ENT}/webhooks/dlr/fake",
        content=raw,
        headers={"X-Signature": f"sha256={sig}", "Content-Type": "application/json"},
        timeout=30,
    )
    return r.status_code, r.text


def send_sms(api, owner, to, sender, text="Pilot test message", key=None):
    return api.post(
        "/v1/messages",
        {"owner_ref": owner, "to": to, "sender": sender, "text": text},
        headers={"Idempotency-Key": key or f"k-{time.time_ns()}"},
    )
