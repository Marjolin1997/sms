from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SMS_", env_file=".env", extra="ignore")

    # PostgreSQL në prodhim; SQLite vetëm për zhvillim të shpejtë lokal.
    database_url: str = "sqlite:///./sms_dev.db"
    db_pool_size: int = 10
    db_lock_timeout_ms: int = 5000  # mos prit pafundësisht kyçjen e një wallet-i
    db_statement_timeout_ms: int = 30000
    db_idle_tx_timeout_ms: int = 60000
    admin_api_key: str = ""  # bosh = të gjitha endpoint-et admin refuzohen

    # Provider HTTP (bosh = i çaktivizuar). Emri duhet të përputhet me sms_routes.provider.
    http_provider_name: str = "http"
    http_provider_url: str = ""
    http_provider_key: str = ""
    http_provider_timeout: float = 10.0
    # Sekret HMAC për webhook-un DLR, për provider: {"http": "sekreti"}. Pa sekret → 401.
    dlr_secrets: dict[str, str] = {}
    # Sa kohë presim DLR pas SENT para se ta konsiderojmë të humbur.
    dlr_timeout_hours: int = 72


settings = Settings()
