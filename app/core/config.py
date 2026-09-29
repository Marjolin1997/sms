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

    # Çelës HMAC për adresat në tabelat e consent-it (nuk ruhen në tekst të hapur).
    # Bosh = konsumi i consent-it dështon (fail closed). Mos e ndrysho pasi ka të dhëna:
    # hash-et ekzistuese nuk do të gjenden më.
    pii_hmac_key: str = ""

    # Email. secrets_key = çelës Fernet (32 bajt base64) për DKIM privat në DB.
    # Gjenero me cryptography.fernet.Fernet.generate_key().decode()
    secrets_key: str = ""
    spf_include: str = "spf.sms-platform.example"  # kërkohet në SPF të domenit të klientit
    public_base_url: str = "http://localhost:8000"  # për lidhjet e çregjistrimit
    email_provider: str = "fake"  # "smtp" në prodhim
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True

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
