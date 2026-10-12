"""Identiteti i lëshuesit të faturës Enterprise (M11-E2.1): burim i vetëm për API-n, HTML-në dhe PDF-në.

- Faturë e re: lexohet VETËM snapshot-i i ruajtur te `sms_invoices.issuer` (konfigurimi aktual nuk lexohet).
- Faturë "legacy" (issuer NULL, para M11-E2.1): fallback EKSPLICIT te konfigurimi aktual `SMS_ISSUER_*` — nuk ka histori të rindërtueshme,
  ndaj nuk shpikim dhe nuk plotësojmë prapa. Provenienca (`SNAPSHOT`/`LEGACY_FALLBACK`) mbetet e brendshme, jo te API i klientit.
"""

import json

from app.core.config import settings
from app.models.billing import Invoice

SNAPSHOT, LEGACY_FALLBACK = "snapshot", "legacy_fallback"
FIELDS = ("name", "address", "tax_id")


def current_issuer() -> dict:
    """Identiteti aktual nga konfigurimi — vetëm për krijimin e faturës së re dhe fallback-un legacy."""
    return {
        "name": settings.issuer_name,
        "address": settings.issuer_address,
        "tax_id": settings.issuer_tax_id,
    }


def snapshot_json() -> str:
    return json.dumps(current_issuer(), ensure_ascii=False, sort_keys=True)


def issuer_of(inv: Invoice) -> tuple[dict, str]:
    if inv.issuer is not None:
        raw = json.loads(inv.issuer)
        return {k: raw.get(k) or "" for k in FIELDS}, SNAPSHOT
    return current_issuer(), LEGACY_FALLBACK
