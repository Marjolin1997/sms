from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SMS_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./sms_dev.db"
    admin_api_key: str = ""  # bosh = të gjitha endpoint-et admin refuzohen


settings = Settings()
