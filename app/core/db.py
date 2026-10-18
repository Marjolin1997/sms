from collections.abc import Iterator

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

# Emra të qëndrueshëm për constraints, që Alembic të jetë deterministik.
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def make_engine(url: str | None = None):
    url = url or settings.database_url
    if url.startswith("sqlite"):
        return create_engine(url, pool_pre_ping=True)
    # Parametra të sigurisë transaksionale: READ COMMITTED (çdo SELECT ... FOR UPDATE sheh
    # gjendjen e fundit të commit-uar), UTC, dhe afate që mos lëshojnë transaksione të varura.
    options = (
        f"-c timezone=utc -c lock_timeout={settings.db_lock_timeout_ms} "
        f"-c statement_timeout={settings.db_statement_timeout_ms} "
        f"-c idle_in_transaction_session_timeout={settings.db_idle_tx_timeout_ms}"
    )
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
