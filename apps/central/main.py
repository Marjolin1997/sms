from fastapi import FastAPI
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

import apps.central.models  # noqa: F401  (regjistron tabelat në Base.metadata)
from apps.central.api import (
    admin,
    auth,
    enterprise_products,
    errors,
    health,
    internal_sync,
    products,
)
from apps.central.core import tokens
from apps.central.core.config import settings
from apps.central.core.db import engine as default_engine


def create_app(engine: Engine | None = None) -> FastAPI:
    if settings.env == "production" and not tokens.configured():
        raise RuntimeError(
            f"CENTRAL_AUTH_SECRET must be set (>= {tokens.MIN_SECRET_LENGTH} chars) in production"
        )
    app = FastAPI(title="SMS Central", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine or default_engine
    app.state.sessionmaker = sessionmaker(bind=app.state.engine, expire_on_commit=False)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(products.router)
    app.include_router(enterprise_products.router)
    app.include_router(internal_sync.router)
    errors.install(app)
    return app


app = create_app()
