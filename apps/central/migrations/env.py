import apps.central.models  # noqa: F401  (regjistron tabelat)
from alembic import context
from apps.central.core.config import settings
from apps.central.core.db import VERSION_TABLE, Base, make_engine

target_metadata = Base.metadata


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
    with engine.connect() as conn:
        context.configure(
            connection=conn, target_metadata=target_metadata, version_table=VERSION_TABLE
        )
        with context.begin_transaction():
            context.run_migrations()


run()
