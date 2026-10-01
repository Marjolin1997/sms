from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Konfigurim i Central: prefiks `CENTRAL_`, skedar `.env.central`; asgjë nga `SMS_*`."""

    model_config = SettingsConfigDict(
        env_prefix="CENTRAL_", env_file=".env.central", extra="ignore"
    )

    env: Literal["development", "production"] = "development"
    # DB logjike e veçantë nga Enterprise (jo skemë e njëjtë, jo tabela të përbashkëta).
    database_url: str = "sqlite:///./central_dev.db"
    db_pool_size: int = 5
    db_statement_timeout_ms: int = 30000


settings = Settings()
