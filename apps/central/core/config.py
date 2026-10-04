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


settings = Settings()
