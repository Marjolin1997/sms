import app.models  # noqa: F401  (regjistron tabelat)
from alembic import context
from app.core.config import settings
from app.core.db import Base, make_engine

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    # Prek vetëm tabelat sms_*; asnjëherë tabelat e omnichannel.
    if type_ == "table" and not name.startswith("sms_"):
        return False
    return True


def run() -> None:
    url = context.config.get_main_option("sqlalchemy.url") or settings.database_url
    if context.is_offline_mode():
        context.configure(
            url=url,
            target_metadata=target_metadata,
            literal_binds=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = make_engine(url)
    with engine.connect() as conn:
        context.configure(
            connection=conn,
            target_metadata=target_metadata,
            include_object=include_object,
            version_table="sms_alembic_version",
        )
        with context.begin_transaction():
            context.run_migrations()


run()
