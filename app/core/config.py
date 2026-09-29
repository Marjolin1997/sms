from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SMS_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./sms_dev.db"
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
