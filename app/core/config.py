from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SMS_", env_file=".env", extra="ignore")

    # "production" aktivizon kontrollet e nisjes (validate_production): pa to aplikacioni
    # refuzon të nisë me konfigurim të pasigurt.
    env: Literal["development", "production"] = "development"

    # M1b: enterprise_id plotësohet automatikisht nga owner_ref (app/core/tenancy.py).
    enterprise_dual_write: bool = True
    enterprise_dual_write_strict: bool = False  # True: anomali owner_ref → gabim (jo NULL)
    # M1c: skopimi i tenant-it. "enterprise" (parazgjedhja) = enterprise_id DHE owner_ref;
    # "owner_ref" = RRUGË RIKTHIMI emergjente (vetëm owner_ref, si para M1c).
    # "enterprise" kërkon dual-write aktiv.
    tenant_scoping: Literal["enterprise", "owner_ref"] = "enterprise"

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
    # M9-a: SENDING pa progres mbi këtë afat merret nga sweeper-i (UNKNOWN/riradhitje sipas fazës)
    sending_lease_seconds: int = Field(default=600, ge=60, le=86400)

    # M7-e/g: sinkronizimi me Control Plane (Central). `off` = asgjë nuk nisë/vëzhgohet;
    # `shadow` = poller + krahasim vetëm-vëzhgim (asnjë vendim trafiku nuk ndryshon);
    # `enforce` = entitlement-i CP merr pjesë në submit (deny wins me AccountPlan lokal).
    # Çelësi privat Ed25519 vjen VETËM nga skedari (mount sekret), kurrë nga DB ose log.
    cp_sync_mode: Literal["off", "shadow", "enforce"] = "off"
    # Në prodhim `enforce` kërkon konfirmim eksplicit të portave (M7-f dry-run real, mospërputhje të
    # pashpjeguara = 0, review/conflict të zgjidhura, periudhë shadow): s'ka bypass nga default.
    cp_enforce_readiness_ack: bool = False
    # M8-c: aplikuesi M7 mund të krijojë tenant "shell" lokal nga gjendja Enterprise e Central.
    # Çelës i veçantë (NUK nënkuptohet nga mode=enforce); default false ⇒ kalohet si panjohur.
    cp_tenant_autocreate: bool = False
    # M9-c: autoriteti i parave. local = sjellja e sotme; shadow = validim i cutover-it (grant-et e
    # marra REGJISTROHEN, s'kreditojnë; mint lokal i ngrirë); central = vetëm grant-et e Central
    # krijojnë kredi pozitive operacionale. Rikthimi central→local = ROLLBACK emergjent (config).
    money_authority: Literal["local", "shadow", "central"] = "local"
    # Në prodhim `central` kërkon konfirmim eksplicit pasi `money_authority_readiness` kaloi.
    money_authority_ack: bool = False
    money_poll_interval_seconds: int = Field(30, ge=5, le=3600)
    cp_base_url: str = ""
    cp_client_id: str = ""
    cp_key_id: str = ""
    cp_private_key_path: str = ""
    cp_poll_interval_seconds: int = Field(30, ge=5, le=3600)
    cp_request_timeout_seconds: float = Field(10.0, ge=1, le=120)
    cp_snapshot_interval_seconds: int = Field(
        3600, ge=300
    )  # rakordim i plotë periodik (i detyrueshëm)

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
        if self.cp_sync_mode != "off" and not self.cp_base_url.startswith("https://"):
            bad.append("SMS_CP_BASE_URL must be https:// when SMS_CP_SYNC_MODE is not off")
        if self.cp_sync_mode == "enforce" and not self.cp_enforce_readiness_ack:
            bad.append(
                "SMS_CP_SYNC_MODE=enforce requires SMS_CP_ENFORCE_READINESS_ACK=true "
                "(M7-f dry-run, unexplained mismatches = 0, shadow period reviewed)"
            )
        if self.money_authority != "local" and not self.cp_base_url.startswith("https://"):
            bad.append("SMS_CP_BASE_URL must be https:// when SMS_MONEY_AUTHORITY is not local")
        if self.money_authority == "central" and not self.money_authority_ack:
            bad.append(
                "SMS_MONEY_AUTHORITY=central requires SMS_MONEY_AUTHORITY_ACK=true "
                "(scripts.money_authority_readiness passed on this database)"
            )
        return bad

    def validate_production(self) -> None:
        if self.env == "production" and (problems := self.production_problems()):
            raise RuntimeError("unsafe production configuration:\n - " + "\n - ".join(problems))

    @model_validator(mode="after")
    def _scoping_needs_dual_write(self):
        if self.tenant_scoping == "enterprise" and not self.enterprise_dual_write:
            raise ValueError(
                "SMS_TENANT_SCOPING=enterprise requires SMS_ENTERPRISE_DUAL_WRITE=true "
                "(rreshtat e rinj do të mbeten pa enterprise_id dhe do të fshiheshin)"
            )
        return self


settings = Settings()
