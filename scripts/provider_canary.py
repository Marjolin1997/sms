"""M11-I/F-07A: canary SINTETIK i pipeline-it submit → provider → DLR → para. Vetëm provider të rremë.

    python -m scripts.provider_canary [--json]

NUK është canary real (ai është F-07B): pa rrjet (soketat bllokohen dhe numërohen), pa kredenciale, pa
provider real, pa trafik real. CLI krijon një DB SQLite të përkohshme, e fshin në fund dhe injoron çdo
`SMS_DATABASE_URL` të jashtëm. Si bibliotekë (`run_canary(...)`, p.sh. nga pytest) refuzon çdo DB që nuk
është e hedhshme (`assert_disposable`): SQLite, ose PostgreSQL në loopback me emër *test*/*canary*/…

Skenarët: submit + kërkesa e provider-it, DLR sukses, DLR dështim, timeout/rezultat i panjohur (pa ridërgim),
DLR i dyfishtë, DLR jashtë rradhe + DLR i vonuar kontradiktor, firmë e gabuar, id e panjohur, dukshmëria
operacionale, idempotenca financiare, adapterët HTTP/Twilio mbi transport të simuluar (httpx.MockTransport).
Kodi: 0 të gjithë skenarët kaluan · 1 ndonjë dështoi · 2 gabim përdorimi/siguria."""

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

PROVIDER = "canary"
PREFIX, COUNTRY = "99999", "ZZ"  # prefiks sintetik: nuk përputhet me asnjë rrugë reale
SENDER = "CANARYZZ"
PRICE = "0.05"
DISPOSABLE_DB = re.compile(r"(test|canary|sandbox|tmp|e2e)", re.I)
LOOPBACK = {None, "", "localhost", "127.0.0.1", "::1"}


class NotDisposable(RuntimeError):
    pass


def assert_disposable(url: str) -> None:
    """Refuzon çdo bazë që s'është e hedhshme: SQLite, ose PostgreSQL loopback me emër test-i."""
    from sqlalchemy.engine import make_url

    u = make_url(url)
    if u.get_backend_name() == "sqlite":
        return
    if u.host in LOOPBACK and DISPOSABLE_DB.search(u.database or ""):
        return
    raise NotDisposable(
        "refusing to run: the database is not disposable (SQLite, or loopback PostgreSQL named "
        "*test*/*canary*/*sandbox*/*tmp*/*e2e* only)"
    )


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    steps: list[Step] = field(default_factory=list)
    network_attempts: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps) and self.network_attempts == 0

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        self.steps.append(Step(name, bool(cond), detail if not cond else detail or "ok"))
        return bool(cond)


class CanaryProvider:
    """Provider sintetik me sjellje të shkruar sipas 4 shifrave të fundit të destinacionit.
    0001 i përkohshëm (s'u pranua) · 0002 refuzim i përhershëm · 0004 timeout i paqartë (s'u pranua)
    · 0005 pranuar por përgjigjja humbi (ambiguous) · 0006 429 (s'u pranua) · ndryshe pranohet.
    Jo-idempotent si adapterët realë (HTTP/Twilio): pas paqartësisë mesazhi bëhet UNKNOWN."""

    name = PROVIDER
    idempotent_by_reference = False

    def __init__(self) -> None:
        self.calls: list = []
        self.accepted: dict[str, str] = {}
        self._n = 0
        self._run = uuid.uuid4().hex[:8]

    def peek_next_id(self) -> str:
        return f"cny-{self._run}-{self._n + 1}"

    def send(self, req):
        from app.providers.base import ProviderError, SendResult

        self.calls.append(req)
        tail = req.destination[-4:]
        if tail == "0001":
            raise ProviderError("canary_temporary", temporary=True)
        if tail == "0002":
            raise ProviderError("canary_rejected", temporary=False)
        if tail == "0006":
            raise ProviderError("http_429", temporary=True)
        self._n += 1
        pid = f"cny-{self._run}-{self._n}"
        if tail == "0004":
            raise ProviderError("network:ReadTimeout", temporary=True, ambiguous=True)
        self.accepted[req.reference] = pid
        if tail == "0005":
            raise ProviderError("network:ConnectionReset", temporary=True, ambiguous=True)
        return SendResult(pid)


@contextlib.contextmanager
def blocked_network(report: Report):
    """Bllokon çdo lidhje soketi (përveç AF_UNIX) dhe numëron përpjekjet; përpjekje ⇒ canary dështon."""
    real = socket.socket.connect

    def guard(self, address):
        if self.family == getattr(socket, "AF_UNIX", None):
            return real(self, address)
        report.network_attempts += 1
        raise OSError("network access is blocked during the synthetic canary")

    socket.socket.connect = guard
    try:
        yield
    finally:
        socket.socket.connect = real


def _sign(secret: str, raw: bytes) -> dict[str, str]:
    return {"X-Signature": "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()}


class Ctx:
    """Mjedisi i një ekzekutimi: sesioni, klienti HTTP në proces, sekreti DLR, ofruesi, pronari, wallet-i."""

    def __init__(self, db, client, admin_headers, secret, provider, owner, wallet_id):
        self.db, self.client, self.admin = db, client, admin_headers
        self.secret, self.provider, self.owner, self.wallet_id = secret, provider, owner, wallet_id
        self._serial = 0

    def dest(self, suffix: int = 3) -> str:
        self._serial += 1
        return f"+{PREFIX}{self._serial:04d}{suffix:04d}"

    def submit(self, suffix: int = 3, text: str = "canary synthetic message"):
        from app.services import messages as svc

        m = svc.submit(
            self.db,
            self.owner,
            f"cny-{uuid.uuid4().hex[:12]}",
            self.dest(suffix),
            SENDER,
            text=text,
        )
        self.db.commit()
        return m

    def dlr(self, body, *, secret=None, raw: bytes | None = None, sign=True):
        raw = raw if raw is not None else json.dumps(body).encode()
        headers = _sign(secret or self.secret, raw) if sign else {}
        r = self.client.post(f"/webhooks/dlr/{PROVIDER}", content=raw, headers=headers)
        self.db.expire_all()
        return r

    def balances(self):
        from app.services import wallet as wallets

        self.db.expire_all()
        return wallets.balances(self.db, self.wallet_id)

    def receipts(self, outcome: str | None = None) -> int:
        from sqlalchemy import func, select

        from app.models.sending import DlrReceipt

        q = select(func.count()).select_from(DlrReceipt).where(DlrReceipt.provider == PROVIDER)
        if outcome:
            q = q.where(DlrReceipt.outcome == outcome)
        return self.db.scalar(q)

    def trail(self, m) -> list[str]:
        from sqlalchemy import select

        from app.models.sending import MessageEvent

        rows = self.db.scalars(
            select(MessageEvent).where(MessageEvent.message_id == m.id).order_by(MessageEvent.id)
        )
        return [e.to_status for e in rows]


def seed(db, owner: str) -> int:
    """Wallet 10 EUR, tarifë sintetike për prefiksin 99999, plan, rrugë → `canary`, sender i miratuar."""
    from app.models.sending import AccountPlan, Route
    from app.services import rates, sender_ids
    from app.services import wallet as wallets

    past = datetime(2020, 1, 1, tzinfo=UTC)
    w = wallets.create_wallet(db, owner, "EUR")
    wallets.confirm_topup(db, wallets.create_topup(db, w.id, "10", wallets.TopupMethod.CASH).id)
    card = rates.create_card(db, f"canary-{owner}", "EUR")
    v = rates.new_draft(db, card.id)
    rates.set_rate(db, v.id, PREFIX, PRICE)
    rates.publish(db, v.id, past, now=past - timedelta(days=1))
    db.add(AccountPlan(owner_ref=owner, rate_card_id=card.id))
    db.add(Route(prefix=PREFIX, country=COUNTRY, provider=PROVIDER, priority=100))
    s = sender_ids.request(db, owner, COUNTRY, SENDER)
    sender_ids.approve(db, s.id, "canary")
    db.commit()
    return w.id


# --- skenarët ---------------------------------------------------------------------------------


def _count_messages(db) -> int:
    from sqlalchemy import func, select

    from app.models.sending import Message

    return db.scalar(select(func.count()).select_from(Message))


def sc_success(c: Ctx, r: Report) -> None:
    from app.models.sending import MessageStatus
    from app.services import messages as svc

    avail0, held0 = c.balances()
    m = c.submit(3)
    r.check("submit: accepted as queued", m.status == MessageStatus.QUEUED)
    r.check("submit: price held once", c.balances() == (avail0 - D(PRICE), held0 + D(PRICE)))
    svc.process_one(c.db)
    c.db.refresh(m)
    req = c.provider.calls[-1]
    r.check(
        "provider request payload",
        req.reference == m.public_id
        and req.destination == m.destination
        and req.sender == SENDER
        and req.text == "canary synthetic message"
        and req.segments == 1,
    )
    r.check(
        "state SENT with provider message id",
        m.status == MessageStatus.SENT
        and m.provider_message_id == c.provider.accepted[m.public_id],
    )
    resp = c.dlr({"provider_message_id": m.provider_message_id, "status": "delivered"})
    r.check(
        "success DLR applied", resp.status_code == 200 and resp.json() == {"outcome": "applied"}
    )
    c.db.refresh(m)
    r.check("final state DELIVERED", m.status == MessageStatus.DELIVERED)
    r.check("captured once", c.balances() == (avail0 - D(PRICE), held0))
    r.check(
        "event trail", c.trail(m) == ["queued", "sending", "sent", "delivered"], str(c.trail(m))
    )
    c._ok_message = m


def sc_failure(c: Ctx, r: Report) -> None:
    from app.models.sending import MessageStatus
    from app.services import messages as svc

    avail0, held0 = c.balances()
    m = c.submit(3)
    svc.process_one(c.db)
    c.db.refresh(m)
    resp = c.dlr(
        {"provider_message_id": m.provider_message_id, "status": "failed", "code": "absent"}
    )
    c.db.refresh(m)
    r.check("failure DLR applied", resp.status_code == 200)
    r.check(
        "final state FAILED with code",
        m.status == MessageStatus.FAILED and m.error_code == "absent",
    )
    r.check("hold released, no charge", c.balances() == (avail0, held0))


def sc_definite_failures(c: Ctx, r: Report) -> None:
    from app.models.sending import MessageStatus
    from app.services import messages as svc

    avail0, held0 = c.balances()
    perm = c.submit(2)
    svc.process_one(c.db)
    c.db.refresh(perm)
    r.check("permanent rejection ⇒ FAILED + release", perm.status == MessageStatus.FAILED)
    tmp = c.submit(1)
    svc.process_one(c.db)
    c.db.refresh(tmp)
    r.check(
        "definite temporary error ⇒ requeued with backoff",
        tmp.status == MessageStatus.QUEUED
        and tmp.attempts == 1
        and tmp.error_code == "canary_temporary",
    )
    r.check(
        "one provider call per attempt",
        [x.reference for x in c.provider.calls].count(tmp.public_id) == 1,
    )
    svc.cancel_if_queued(c.db, tmp.id)
    c.db.commit()
    r.check("no money left held after cleanup", c.balances() == (avail0, held0))


def sc_unknown_outcome(c: Ctx, r: Report) -> None:
    from app.models.sending import MessageStatus
    from app.services import messages as svc

    avail0, held0 = c.balances()
    for suffix, label in ((4, "timeout before response"), (5, "accepted but response lost")):
        m = c.submit(suffix)
        svc.process_one(c.db)
        c.db.refresh(m)
        n = [x.reference for x in c.provider.calls].count(m.public_id)
        r.check(f"{label}: UNKNOWN, hold retained", m.status == MessageStatus.UNKNOWN and n == 1)
        svc.process_one(c.db)
        svc.recover_stuck(c.db, lease=timedelta(0))
        c.db.commit()
        c.db.refresh(m)
        r.check(
            f"{label}: never re-sent (worker + sweeper)",
            [x.reference for x in c.provider.calls].count(m.public_id) == 1
            and m.status == MessageStatus.UNKNOWN,
        )
        r.check(
            f"{label}: visible to operators",
            any(u["id"] == m.public_id for u in svc.list_unknown(c.db)),
        )
        _, held = c.balances()
        r.check(f"{label}: money untouched", held == held0 + D(PRICE))
        pid = c.provider.accepted.get(m.public_id, f"cny-late-{uuid.uuid4().hex[:8]}")
        resp = c.dlr({"provider_message_id": pid, "status": "delivered", "reference": m.public_id})
        c.db.refresh(m)
        r.check(
            f"{label}: late DLR by reference resolves exactly once",
            resp.status_code == 200
            and m.status == MessageStatus.DELIVERED
            and m.provider_message_id == pid,
        )
        c.dlr({"provider_message_id": pid, "status": "delivered", "reference": m.public_id})
    avail, held = c.balances()
    r.check(
        "two resolved UNKNOWNs captured exactly twice",
        (avail0 - avail, held) == (D(PRICE) * 2, held0),
    )


def sc_duplicate_dlr(c: Ctx, r: Report) -> None:
    from app.models.sending import MessageStatus
    from app.services import messages as svc

    m = c.submit(3)
    svc.process_one(c.db)
    c.db.refresh(m)
    before, receipts = c.balances(), c.receipts("applied")
    body = {"provider_message_id": m.provider_message_id, "status": "delivered"}
    codes = [c.dlr(body).status_code for _ in range(3)]
    after = c.balances()
    c.db.refresh(m)
    r.check(
        "duplicate DLR: all 200, state DELIVERED",
        codes == [200] * 3 and m.status == MessageStatus.DELIVERED,
    )
    r.check(
        "duplicate DLR: money moved once",
        after[1] == before[1] - D(PRICE) and after[0] == before[0],
    )
    r.check("duplicate DLR: every callback audited", c.receipts("applied") - receipts == 3)


def sc_out_of_order(c: Ctx, r: Report) -> None:
    from app.models.sending import MessageStatus
    from app.services import messages as svc

    m = c.submit(3)
    pid = c.provider.peek_next_id()
    early = c.dlr({"provider_message_id": pid, "status": "delivered"})
    c.db.refresh(m)
    r.check(
        "DLR before SENT: 503 so the provider retries; state unchanged",
        early.status_code == 503 and m.status == MessageStatus.QUEUED,
    )
    svc.process_one(c.db)
    c.db.refresh(m)
    retry = c.dlr({"provider_message_id": pid, "status": "delivered"})
    c.db.refresh(m)
    r.check(
        "provider retry after SENT applies",
        retry.status_code == 200
        and m.status == MessageStatus.DELIVERED
        and m.provider_message_id == pid,
    )
    avail, held = c.balances()
    stale = c.dlr({"provider_message_id": pid, "status": "failed", "code": "stale"})
    c.db.refresh(m)
    r.check(
        "stale contradicting DLR: 409, terminal success not regressed",
        stale.status_code == 409 and m.status == MessageStatus.DELIVERED,
    )
    r.check("stale DLR moved no money", c.balances() == (avail, held))
    r.check("stale DLR audited as conflict", c.receipts("conflict") >= 1)


def sc_bad_signature(c: Ctx, r: Report) -> None:
    from app.models.sending import MessageStatus
    from app.services import messages as svc

    m = c.submit(3)
    svc.process_one(c.db)
    c.db.refresh(m)
    body = {"provider_message_id": m.provider_message_id, "status": "delivered"}
    receipts = c.receipts()
    wrong = c.dlr(body, secret="not-the-secret")
    missing = c.dlr(body, sign=False)
    c.db.refresh(m)
    r.check(
        "bad/missing signature: 401, fail closed",
        wrong.status_code == 401 and missing.status_code == 401 and m.status == MessageStatus.SENT,
    )
    r.check("rejected callbacks create no receipt and no state", c.receipts() == receipts)
    garbage = c.dlr(None, raw=b"{not json")
    invalid = c.dlr({"provider_message_id": "x", "status": "bogus"})
    r.check(
        "malformed signed payload: 422 fail closed",
        garbage.status_code == 422 and invalid.status_code == 422,
    )
    c.db.refresh(m)
    r.check("malformed payload changed nothing", m.status == MessageStatus.SENT)
    c.dlr(body)  # mbyll mesazhin: s'lë gjendje të ndërmjetme


def sc_unknown_id(c: Ctx, r: Report) -> None:
    before_msgs, receipts = _count_messages(c.db), c.receipts("unknown_message")
    resp = c.dlr({"provider_message_id": "cny-does-not-exist", "status": "delivered"})
    r.check("unknown provider id: 503 (provider retries), nothing created", resp.status_code == 503)
    after_msgs = _count_messages(c.db)
    r.check(
        "unknown id audited, no message touched",
        c.receipts("unknown_message") == receipts + 1 and after_msgs == before_msgs,
    )


def sc_visibility(c: Ctx, r: Report) -> None:
    stats = c.client.get("/v1/admin/stats", headers=c.admin)
    prov = c.client.get("/v1/admin/providers", headers=c.admin)
    body = stats.json() if stats.status_code == 200 else {}
    r.check(
        "admin stats expose DLR problems",
        stats.status_code == 200
        and body.get("dlr_problems_24h", {}).get("conflict", 0) >= 1
        and body["dlr_problems_24h"].get("unknown_message", 0) >= 1,
    )
    rows = {
        p["provider"]: p
        for p in (prov.json().get("providers", []) if prov.status_code == 200 else [])
    }
    r.check(
        "provider health lists the canary provider",
        PROVIDER in rows and rows[PROVIDER]["total"] >= 1,
    )
    from scripts import queue_readiness

    checks = {x.name: x for x in queue_readiness.evaluate(c.db)}
    r.check("queue readiness has no stuck SENDING", checks["stuck_sending_sms"].level == "PASS")


def sc_financial(c: Ctx, r: Report) -> None:
    from sqlalchemy import func, select

    from app.models.sending import Message, MessageStatus
    from app.services import wallet as wallets

    c.db.expire_all()
    delivered = c.db.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.provider == PROVIDER, Message.status == MessageStatus.DELIVERED)
    )
    avail, held = c.balances()
    r.check("wallet ledger verifies", wallets.verify_wallet(c.db, c.wallet_id))
    r.check(
        "charged exactly once per delivered message",
        D("10") - avail - held == D(PRICE) * delivered and held == 0,
        f"delivered={delivered}",
    )


def sc_adapters(c: Ctx, r: Report) -> None:
    """Adapterët REALË HTTP/Twilio mbi transport të simuluar: s'del asnjë paketë në rrjet."""
    import httpx

    from app.providers.base import ProviderError, SendRequest
    from app.providers.http import HttpProvider
    from app.providers.twilio import TwilioProvider, twilio_signature, verify_twilio_signature

    seen: list[httpx.Request] = []
    script = {"status": 200, "json": {"id": "http-canary-1"}}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(script["status"], json=script["json"])

    http = HttpProvider(
        "canary-http",
        "https://provider.invalid/send",
        "canary-key-not-real",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    sr = SendRequest("ref-1", "ACME", "99999000103", "hello", "GSM-7", 1)
    out = http.send(sr)
    body = json.loads(seen[-1].content)
    r.check(
        "http adapter payload",
        out.provider_message_id == "http-canary-1"
        and body
        == {
            "reference": "ref-1",
            "from": "ACME",
            "to": "99999000103",
            "text": "hello",
            "encoding": "GSM-7",
            "segments": 1,
        },
    )
    r.check(
        "http adapter sends bearer auth, never in the body",
        seen[-1].headers["authorization"] == "Bearer canary-key-not-real"
        and b"canary-key" not in seen[-1].content,
    )
    script.update(status=503, json={"error": "secret-looking provider body canary-key-not-real"})
    try:
        http.send(sr)
    except ProviderError as e:
        r.check(
            "http 5xx: ambiguous, error code carries no body",
            e.ambiguous and e.code == "http_503" and "secret" not in str(e),
        )
    tw_seen: list[httpx.Request] = []

    def tw_handler(req: httpx.Request) -> httpx.Response:
        tw_seen.append(req)
        return httpx.Response(201, json={"sid": "SMcanary0001", "status": "queued"})

    tw = TwilioProvider(
        "ACcanary",
        "canary-token-not-real",
        "https://canary.invalid/webhooks/twilio/status",
        client=httpx.Client(transport=httpx.MockTransport(tw_handler)),
    )
    out = tw.send(SendRequest("ref-2", "355690000001", "99999000103", "hello", "GSM-7", 1))
    form = dict(x.split("=", 1) for x in tw_seen[-1].content.decode().split("&"))
    r.check(
        "twilio adapter request",
        out.provider_message_id == "SMcanary0001"
        and form["Body"] == "hello"
        and "StatusCallback" in form
        and tw_seen[-1].headers["authorization"].startswith("Basic "),
    )
    params = [("MessageSid", "SMcanary0001"), ("MessageStatus", "delivered")]
    sig = twilio_signature(
        "canary-token-not-real", "https://canary.invalid/webhooks/twilio/status", params
    )
    r.check(
        "twilio callback signature verifies and rejects tampering",
        verify_twilio_signature(
            "canary-token-not-real", "https://canary.invalid/webhooks/twilio/status", params, sig
        )
        and not verify_twilio_signature(
            "canary-token-not-real",
            "https://canary.invalid/webhooks/twilio/status",
            [*params, ("X", "1")],
            sig,
        ),
    )


SCENARIOS = (
    sc_success, sc_failure, sc_definite_failures, sc_unknown_outcome, sc_duplicate_dlr,
    sc_out_of_order, sc_bad_signature, sc_unknown_id, sc_visibility, sc_financial, sc_adapters,
)  # fmt: skip


def run_canary(db, client, admin_headers: dict[str, str]) -> Report:
    """Ekzekuton canary-n mbi një DB të hedhshme. Regjistron/ç’regjistron provider-in `canary` dhe vendos
    përkohësisht sekretin DLR; pas ekzekutimit rikthen regjistrin dhe konfigurimin."""
    from app import providers
    from app.core.config import settings
    from app.core.db import engine

    assert_disposable(str(engine.url.render_as_string(hide_password=False)))
    report = Report()
    secret = uuid.uuid4().hex + uuid.uuid4().hex
    owner = f"canary-{uuid.uuid4().hex[:8]}"
    provider = CanaryProvider()
    old_registry = providers._registry.get(PROVIDER)
    old_secrets = dict(settings.dlr_secrets)
    providers.register(provider)
    settings.dlr_secrets = {**old_secrets, PROVIDER: secret}
    try:
        with blocked_network(report):
            ctx = Ctx(db, client, admin_headers, secret, provider, owner, seed(db, owner))
            for sc in SCENARIOS:
                try:
                    sc(ctx, report)
                except Exception as e:  # skenari dështoi: raportohet, vazhdon me të tjerët
                    db.rollback()
                    report.check(f"{sc.__name__}: no exception", False, f"{type(e).__name__}: {e}")
        report.check("no network access attempted", report.network_attempts == 0)
    finally:
        settings.dlr_secrets = old_secrets
        if old_registry is None:
            providers._registry.pop(PROVIDER, None)
        else:
            providers._registry[PROVIDER] = old_registry
    report.check(
        "cleanup: provider registry and DLR secret restored",
        PROVIDER not in providers._registry or old_registry is not None,
    )
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Synthetic provider/DLR canary (fake providers only).")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    tmp = Path(tempfile.mkdtemp(prefix="sms-canary-"))
    os.environ.update(
        SMS_DATABASE_URL=f"sqlite:///{tmp / 'canary.db'}",
        SMS_ADMIN_API_KEY="canary-admin-key-" + uuid.uuid4().hex,
        SMS_PII_HMAC_KEY="canary-pii-" + uuid.uuid4().hex,
        SMS_SECRETS_KEY="wV0dVQ1nH7xk2m3bYw0m8y7QbKpZ0o1o9mGQ0mF0dJQ=",
        SMS_DEFAULT_LANGUAGE="en",
    )
    try:
        from fastapi.testclient import TestClient

        import app.models  # noqa: F401
        from app.core.config import settings
        from app.core.db import Base, SessionLocal, engine
        from app.main import create_app

        Base.metadata.create_all(engine)
        with SessionLocal() as db, TestClient(create_app()) as client:
            report = run_canary(db, client, {"X-Admin-Key": settings.admin_api_key})
        engine.dispose()
    except NotDisposable as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    gone = not tmp.exists()
    if args.json:
        print(
            json.dumps(
                {
                    "ok": report.ok and gone,
                    "network_attempts": report.network_attempts,
                    "cleaned_up": gone,
                    "steps": [asdict(s) for s in report.steps],
                },
                indent=2,
            )
        )
    else:
        for s in report.steps:
            print(f"{'PASS' if s.ok else 'FAIL'} {s.name}" + ("" if s.ok else f": {s.detail}"))
        print(
            f"synthetic canary: {'OK' if report.ok and gone else 'FAILED'} ({len(report.steps)} checks; temp database removed: {gone})"
        )
    return 0 if report.ok and gone else 1


if __name__ == "__main__":
    sys.exit(main())
