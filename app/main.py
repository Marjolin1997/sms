import uuid

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api import (
    admin,
    billing,
    campaigns,
    console,
    contacts,
    email,
    messages,
    messaging,
    portal,
    public,
    rates,
    reports,
    wallets,
    webhooks,
)
from app.core.config import settings
from app.providers import register_configured

MAX_BODY_BYTES = 256 * 1024
_RID_CHARS = set(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


class SecurityMiddleware:
    """Kufi për madhësinë e trupit (përpara parsimit) dhe header-a sigurie standardë."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        length = headers.get(b"content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return await self._reply(send, 413, b'{"detail":"payload too large"}')
        received = 0
        incoming = headers.get(b"x-request-id", b"")
        # ID e klientit pranohet vetëm nëse është e sigurt për log-e (pa hapësira/kontroll)
        ok = 0 < len(incoming) <= 64 and all(c in _RID_CHARS for c in incoming)
        request_id = incoming if ok else uuid.uuid4().hex.encode()

        async def limited_receive():
            nonlocal received
            msg = await receive()
            if msg["type"] == "http.request":
                received += len(msg.get("body", b""))
                if received > MAX_BODY_BYTES:
                    raise RuntimeError("payload too large")
            return msg

        async def send_hardened(msg):
            if msg["type"] == "http.response.start":
                have = {k.lower() for k, _ in msg.get("headers", [])}
                extra = [
                    (b"x-request-id", request_id),
                    (b"x-content-type-options", b"nosniff"),
                    (b"cache-control", b"no-store"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"strict-transport-security", b"max-age=63072000; includeSubDomains"),
                    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
                ]
                extra = [(k, v) for k, v in extra if k not in have]  # endpoint-i mund ta ketë vetë
                msg = {**msg, "headers": [*msg.get("headers", []), *extra]}
            await send(msg)

        return await self.app(scope, limited_receive, send_hardened)

    @staticmethod
    async def _reply(send, status, body):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json")]})  # fmt: skip
        await send({"type": "http.response.body", "body": body})


def create_app() -> FastAPI:
    settings.validate_production()
    register_configured()
    app = FastAPI(title="SMS Platform", version="0.1.0", docs_url=None, redoc_url=None)
    app.add_middleware(SecurityMiddleware)
    app.include_router(wallets.router)
    app.include_router(rates.router)
    app.include_router(messaging.router)
    app.include_router(messages.router)
    app.include_router(webhooks.router)
    app.include_router(admin.router)
    app.include_router(contacts.router)
    app.include_router(campaigns.router)
    app.include_router(email.router)
    app.include_router(public.router)
    app.include_router(portal.router)
    app.include_router(billing.router)
    app.include_router(console.router)
    app.include_router(reports.router)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        from app.core.readiness import check

        problem = check()
        if problem:
            return JSONResponse({"status": "not_ready", "reason": problem}, status_code=503)
        return {"status": "ready"}

    return app


app = create_app()
