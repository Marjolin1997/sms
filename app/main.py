from fastapi import FastAPI

from app.api import wallets


def create_app() -> FastAPI:
    app = FastAPI(title="SMS Platform", version="0.1.0")
    app.include_router(wallets.router)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    return app


app = create_app()
