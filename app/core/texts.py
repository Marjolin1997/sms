"""Tekste për përdoruesit fundorë që prodhon serveri (faturë, faqe çregjistrimi, fundi i emailit).

Konsola ka fjalorin e vet (frontend/src/locales); këtu janë vetëm faqet publike/dokumentet.
Gjuha merret nga settings.default_language; çelësi i panjohur kthehet si është (nuk prishet asgjë).
"""

from app.core.config import settings

_SQ = {
    "Invoice": "Faturë",
    "Issued": "Lëshuar",
    "Due": "Afati",
    "Period": "Periudha",
    "to": "deri",
    "Description": "Përshkrimi",
    "Qty": "Sasia",
    "Unit": "Njësia",
    "Amount": "Shuma",
    "Subtotal": "Nëntotali",
    "VAT": "TVSH",
    "Total": "Totali",
    "Unsubscribe": "Çregjistrohu",
    "This link is not valid.": "Kjo lidhje nuk është e vlefshme.",
    "Confirm that you no longer want to receive these emails.": (
        "Konfirmoni që nuk dëshironi më t'i merrni këta emaile."
    ),
    "You are unsubscribed.": "Jeni çregjistruar.",
    "draft": "skicë",
    "open": "e hapur",
    "paid": "e paguar",
    "void": "e anuluar",
}


def tr(key: str, lang: str | None = None) -> str:
    return _SQ.get(key, key) if (lang or settings.default_language) == "sq" else key


def html_lang(lang: str | None = None) -> str:
    return lang or settings.default_language
