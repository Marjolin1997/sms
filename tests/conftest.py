import os
import tempfile

# SMS_TEST_DATABASE_URL (PostgreSQL) është rruga e vërtetë; SQLite është vetëm fallback lokal.
_db = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["SMS_DATABASE_URL"] = os.environ.get("SMS_TEST_DATABASE_URL") or f"sqlite:///{_db}"
os.environ["SMS_ADMIN_API_KEY"] = "test-key"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.models  # noqa: E402, F401
from app.core.db import Base, SessionLocal, engine  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def schema():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def db():
    with SessionLocal() as s:
        yield s


@pytest.fixture
def client():
    app = create_app()
    return TestClient(app, headers={"X-Admin-Key": "test-key"})
