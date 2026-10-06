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
    # M8-b: politikat `automatic` janë të papërdorshme sa kohë ky gate është false (regjistrimi
    # publik s'ka verifikim kontakti): `automatic` nuk vendoset dhe submit-i s'auto-miraton.
    # Vetëm dev/test (ose rrjedhë e ardhshme e verifikuar); në prodhim nisja refuzohet nëse true.
    allow_unverified_auto_registration: bool = False
    # M8-d: endpoint-et PUBLIKE të regjistrimit (/registration*) janë të mbyllura (503) si default.
    # Koncept i ndarë nga gate-i i auto-miratimit; admin API vazhdon pavarësisht. Pa verifikim
    # kontakti, nëse hapet në prodhim qëndron VETËM me miratim manual.
    public_registration_enabled: bool = False
    # Kuota për email (kërkesa reale të krijuara, dritare rrëshqitëse 24h UTC). IP limit = proxy.
    public_registration_max_per_email_24h: int = Field(default=3, ge=1, le=100)
    # --- M8-e: verifikimi i kontaktit, dërgimi i email-it, kufijtë në proxy, sfida anti-bot ---
    # Çelësi HMAC nga i cili DERIVOHET tokeni i verifikimit (s'ruhet asnjë token/hash në DB).
    # Bosh ose <32 karaktere = verifikimi i padisponueshëm (regjistrimi mbetet vetëm manual).
    registration_verify_key: str = ""
    registration_verify_ttl_minutes: int = Field(default=60, ge=10, le=1440)
    # Baza e lidhjes në email (frontend-i i portalit), p.sh. https://portal.example/verify
    registration_verify_url_base: str = ""
    # Dërgimi i email-it (Central ka mailer të vetin, i pavarur nga Enterprise).
    mailer: Literal["disabled", "fake", "smtp"] = "disabled"
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    smtp_from: str = ""
    # Operatori vërteton që proxy-ja ka `client_max_body_size 4k` + limit_req për /registration*
    # (nginx i Central s'menaxhohet në repo): pa këtë, readiness dështon në prodhim.
    public_registration_proxy_ack: bool = False
    # Sfida anti-bot (kufi i pavarur nga vendori): `disabled` | `fake` (vetëm test/dev).
    bot_challenge: Literal["disabled", "fake"] = "disabled"
    public_registration_require_challenge: bool = False
    # Proxy të besuar për IP-në e klientit (vetëm për LOG; kufijtë IP jetojnë te proxy).
    trusted_proxy_hops: int = Field(default=0, ge=0, le=5)
    # --- M9-d: pragje të rakordimit financiar (vetëm vëzhgim; konfigurueshme) ---
    # Mosha e raportit (nga marrja): ≤ fresh = OK · ≤ stale = WARN · më shumë = FAIL.
    money_report_fresh_seconds: int = Field(default=600, ge=60, le=86400)
    money_report_stale_seconds: int = Field(default=1800, ge=60, le=604800)
    # Grant/reversal i dhënë por jo ende i konsumuar (kursori pas): WARN brenda grace, FAIL pas saj.
    money_cursor_lag_grace_seconds: int = Field(default=900, ge=60, le=86400)
    # Kursori i parave i Enterprise pa sukses të ri (në çastin e raportit): WARN / FAIL.
    money_cursor_stale_warn_seconds: int = Field(default=900, ge=60, le=86400)
    money_cursor_stale_fail_seconds: int = Field(default=3600, ge=60, le=604800)
    # Reversal i pazbatuar operacionalisht: WARN deri këtu, pastaj FAIL.
    money_unresolved_reversal_fail_seconds: int = Field(default=3600, ge=60, le=2592000)
    # Llogari me grant por pa asnjë raport: WARN brenda grace, pastaj FAIL.
    money_report_missing_grace_seconds: int = Field(default=1800, ge=60, le=604800)
    # --- M9-f: operacione financiare (pragje alarmi + retention i kufizuar) ---
    # Pagesë `pending` më e vjetër se kjo = operacion i ngecur (WARN).
    payment_pending_stale_seconds: int = Field(default=172800, ge=3600, le=2592000)
    # Retention i `usage_reports`: 0 = pa fshirje (parazgjedhje konservatore). Raporti aktual dhe
    # `keep_last` të fundit per çelës NUK fshihen kurrë; mes `full_days` dhe `retention_days`
    # mbahet një raport per ditë UTC.
    usage_report_retention_days: int = Field(default=0, ge=0, le=3650)
    usage_report_full_days: int = Field(default=30, ge=1, le=3650)
    usage_report_keep_last: int = Field(default=20, ge=1, le=10000)
    # --- M9-g1: faturimi periodik (Central lëshon). Issuer-i ngrihet në çdo faturë në lëshim. ---
    invoice_due_days: int = Field(default=14, ge=0, le=365)
    issuer_name: str = Field(default="Your Company Ltd", max_length=120)
    issuer_address: str = Field(default="Street 1, City, Country", max_length=300)
    issuer_tax_id: str = Field(default="", max_length=40)


settings = Settings()
