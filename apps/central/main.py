from fastapi import FastAPI
from sqlalchemy.engine import Engine

import apps.central.models  # noqa: F401  (regjistron tabelat në Base.metadata)
from apps.central.api import health
from apps.central.core.db import engine as default_engine


def create_app(engine: Engine | None = None) -> FastAPI:
    app = FastAPI(title="SMS Central", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine or default_engine
    app.include_router(health.router)
    return app


app = create_app()
