from fastapi import FastAPI

from app.api import messages, messaging, rates, wallets, webhooks
from app.providers import register_configured


def create_app() -> FastAPI:
    register_configured()
    app = FastAPI(title="SMS Platform", version="0.1.0")
    app.include_router(wallets.router)
    app.include_router(rates.router)
    app.include_router(messaging.router)
    app.include_router(messages.router)
    app.include_router(webhooks.router)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app


app = create_app()
