from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SMS_", env_file=".env", extra="ignore")

    # "production" aktivizon kontrollet e nisjes (validate_production): pa to aplikacioni
    # refuzon të nisë me konfigurim të pasigurt.
    env: Literal["development", "production"] = "development"

    # M1b: enterprise_id plotësohet automatikisht nga owner_ref (app/core/tenancy.py).
    enterprise_dual_write: bool = True
    enterprise_dual_write_strict: bool = False  # True: anomali owner_ref → gabim (jo NULL)

    # Gjuha e teksteve për përdoruesit fundorë (faturë, faqja e çregjistrimit, fundi i emailit).
    default_language: Literal["sq", "en"] = "sq"

    # Mbrojtje nga provat e përsëritura të çelësave (për IP klienti).
    auth_max_failures: int = 20
    auth_fail_window_s: int = 600
    # Sa proxy të besuar ka para aplikacionit (0 = pa proxy: përdoret adresa e lidhjes).
    # Me N>0, IP e klientit është elementi N-nga-fundi i X-Forwarded-For.
    trusted_proxy_hops: int = 0

    # Hapi i dytë (TOTP) për veprime të ndjeshme të stafit. True = çelësat e stafit pa 2FA
    # të regjistruar refuzohen për to (çelësi bootstrap përjashtohet: është vetëm për nisjen).
    require_staff_2fa: bool = False

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

    # Webhook-et e klientëve
    webhook_allow_http: bool = False  # vetëm dev; prodhim = vetëm https
    webhook_timeout: float = 10.0
    event_retention_days: int = 30

    # Faturim
    invoice_due_days: int = 14
    issuer_name: str = "Your Company Ltd"  # shfaqet në faturë (kompania që shet platformën)
    issuer_address: str = "Street 1, City, Country"
    issuer_tax_id: str = ""
    payment_provider: str = "fake"
    payment_min: str = "1"
    payment_max: str = "10000"

    # Provider HTTP (bosh = i çaktivizuar). Emri duhet të përputhet me sms_routes.provider.
    http_provider_name: str = "http"
    http_provider_url: str = ""
    http_provider_key: str = ""
    http_provider_timeout: float = 10.0
    # Twilio (bosh = i çaktivizuar). Auth token-i përdoret edhe për të verifikuar nënshkrimin
    # X-Twilio-Signature të callback-eve (URL-ja publike = SMS_PUBLIC_BASE_URL).
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_messaging_service_sid: str = ""  # opsionale: në vend të From
    twilio_timeout: float = 10.0
    # Sekret HMAC për webhook-un DLR, për provider: {"http": "sekreti"}. Pa sekret → 401.
    dlr_secrets: dict[str, str] = {}
    # Sa kohë presim DLR pas SENT para se ta konsiderojmë të humbur.
    dlr_timeout_hours: int = 72

    def production_problems(self) -> list[str]:
        """Konfigurime që nuk lejohen në prodhim (lista bosh = në rregull)."""
        bad = []
        if self.database_url.startswith("sqlite"):
            bad.append("SMS_DATABASE_URL must be PostgreSQL")
        if len(self.pii_hmac_key) < 32:
            bad.append("SMS_PII_HMAC_KEY must be set (at least 32 characters)")
        if not self.secrets_key:
            bad.append("SMS_SECRETS_KEY must be set (Fernet key)")
        if self.admin_api_key and (
            len(self.admin_api_key) < 24
            or self.admin_api_key.lower().startswith(("change", "dev", "test", "demo"))
        ):
            bad.append("SMS_ADMIN_API_KEY is weak/default: use 24+ random characters or unset it")
        if not self.public_base_url.startswith("https://"):
            bad.append("SMS_PUBLIC_BASE_URL must be https://")
        if self.webhook_allow_http:
            bad.append("SMS_WEBHOOK_ALLOW_HTTP must be false")
        if self.email_provider == "fake":
            bad.append("SMS_EMAIL_PROVIDER must not be 'fake'")
        if self.payment_provider == "fake":
            bad.append("SMS_PAYMENT_PROVIDER must not be 'fake' (use 'disabled' for now)")
        return bad

    def validate_production(self) -> None:
        if self.env == "production" and (problems := self.production_problems()):
            raise RuntimeError("unsafe production configuration:\n - " + "\n - ".join(problems))


settings = Settings()
