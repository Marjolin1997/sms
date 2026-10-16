from sqlalchemy import inspect

import apps.central.models  # noqa: F401  (regjistron tabelat)
from alembic import context
from apps.central.core.config import settings
from apps.central.core.db import VERSION_TABLE, Base, make_engine

target_metadata = Base.metadata
ENTERPRISE_VERSION_TABLE = "sms_alembic_version"


def run() -> None:
    url = context.config.get_main_option("sqlalchemy.url") or settings.database_url
    if context.is_offline_mode():
        context.configure(
            url=url,
            target_metadata=target_metadata,
            literal_binds=True,
            version_table=VERSION_TABLE,
        )
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = make_engine(url)
    if settings.env == "production" and engine.dialect.name != "postgresql":
        raise RuntimeError("CENTRAL_DATABASE_URL must be PostgreSQL in production")
    # Central s'migron kurrë mbi një DB Enterprise (tabela e versioneve e Enterprise).
    # Provë në lidhje të veçantë: `inspect()` hap transaksion (autobegin), jo në lidhjen e migrimit.
    with engine.connect() as probe:
        if inspect(probe).has_table(ENTERPRISE_VERSION_TABLE):
            raise RuntimeError(
                "refusing to migrate Central: the target looks like an Enterprise database "
                f"('{ENTERPRISE_VERSION_TABLE}' exists); check CENTRAL_DATABASE_URL"
            )
    with engine.connect() as conn:
        context.configure(
            connection=conn, target_metadata=target_metadata, version_table=VERSION_TABLE
        )
        with context.begin_transaction():
            context.run_migrations()


run()
