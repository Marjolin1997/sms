"""M9-g4: autoriteti i faturimit periodik (Enterprise). Vendim vetëm nga konfigurimi lokal — asnjë thirrje rrjeti, asnjë kontakt me DB-në e Central.

- `local`: Enterprise lëshon (sjellja e sotme) · `shadow`: Enterprise lëshon ende (Central vetëm krahason) · `central`: Central lëshon dhe zotëron
  planet/abonimet/shlyerjen; çdo mutacion tregtar legacy refuzohet fail-closed me `BillingAuthorityFrozen`. Leximi i historisë mbetet i lejuar.
Rikthimi central→local është rollback i kontrolluar (shih docs/M9G_BILLING.md): s'është kurrë një ndërrim i heshtur."""

from app.core.config import settings
from app.core.errors import DomainError


class BillingAuthorityFrozen(DomainError):
    code = "billing_authority_frozen"


def mode() -> str:
    return settings.billing_authority


def frozen() -> bool:
    return settings.billing_authority == "central"


def require_issuer(action: str) -> None:
    """Thirret nga çdo mutacion tregtar legacy (lëshim, plan, abonim, profil, pagesë/void fature)."""
    if frozen():
        raise BillingAuthorityFrozen(
            f"legacy billing is frozen: SMS_BILLING_AUTHORITY=central ({action} is owned by Central)"
        )
