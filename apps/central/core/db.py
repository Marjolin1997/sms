from collections.abc import Iterator

from sqlalchemy import MetaData, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from apps.central.core.config import settings

# Tabela e versioneve e Central; e ndarë nga `sms_alembic_version` e Enterprise.
VERSION_TABLE = "central_alembic_version"

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Metadata e Central (e pavarur nga `app.core.db.Base` e Enterprise)."""

    metadata = MetaData(naming_convention=NAMING)


def make_engine(url: str | None = None) -> Engine:
    url = url or settings.database_url
    if url.startswith("sqlite"):
        return create_engine(url, pool_pre_ping=True)
    options = f"-c timezone=utc -c statement_timeout={settings.db_statement_timeout_ms}"
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        isolation_level="READ COMMITTED",
        connect_args={"options": options},
    )


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
