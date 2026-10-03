from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Konfigurim i Central: prefiks `CENTRAL_`, skedar `.env.central` (pavarur nga Enterprise)."""

    model_config = SettingsConfigDict(
        env_prefix="CENTRAL_", env_file=".env.central", extra="ignore"
    )

    env: Literal["development", "production"] = "development"
    # DB logjike e veçantë nga Enterprise (jo skemë e njëjtë, jo tabela të përbashkëta).
    database_url: str = "sqlite:///./central_dev.db"
    db_pool_size: int = 5
    db_statement_timeout_ms: int = 30000
    # Autentikimi i stafit të Central: sekret i VETËM i Central (pa lidhje me Enterprise).
    # Bosh ose <32 karaktere = autentikimi çaktivizohet (503); në prodhim nisja refuzohet.
    auth_secret: str = ""
    auth_ttl_seconds: int = Field(default=900, ge=60, le=86400)


settings = Settings()
