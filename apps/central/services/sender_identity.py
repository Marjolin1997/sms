"""M10-S1: identiteti kanonik i sender-it në Central — NORMALIZIM identik me Enterprise (S0), pa varësi nga `app`.

Rregullat (golden parity me `app.services.sender_authorization` te `tests/test_m10s1_sender.py`):
- numerik `^\\+?[1-9]\\d{2,14}$` → vlera pa `+` (display = norm);
- alfanumerik `^(?=.*[A-Za-z])[A-Za-z0-9 ]{3,11}$` pa hapësira anash → display i ruajtur, norm = `lower()`;
- `approved_key = "<SHTET>:<norm>"` (UNIQUE global për gjendjen APPROVED)."""

import re
from dataclasses import dataclass

from apps.central.core.errors import Invalid

ALNUM = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9 ]{3,11}$")
NUMERIC = re.compile(r"^\+?[1-9]\d{2,14}$")
COUNTRY = re.compile(r"^[A-Za-z]{2}$")
KIND_ALNUM, KIND_NUMERIC = "alphanumeric", "numeric"
KINDS = (KIND_ALNUM, KIND_NUMERIC)


@dataclass(frozen=True, slots=True)
class Identity:
    country: str
    kind: str
    display: str
    norm: str

    @property
    def approved_key(self) -> str:
        return canonical_key(self.country, self.norm)


def country_code(value) -> str:
    if not isinstance(value, str) or not COUNTRY.match(value):
        raise Invalid("country must be ISO alpha-2")
    return value.upper()


def classify(value) -> tuple[str, str]:
    if isinstance(value, str):
        if NUMERIC.match(value):
            return value.lstrip("+"), KIND_NUMERIC
        if ALNUM.match(value) and value == value.strip():
            return value, KIND_ALNUM
    raise Invalid("sender must be 3-11 alphanumerics (with a letter) or a phone number")


def norm_of(display: str, kind: str) -> str:
    return display if kind == KIND_NUMERIC else display.lower()


def canonical_key(country: str, norm: str) -> str:
    return f"{country.upper()}:{norm}"


def identity(country, value) -> Identity:
    display, kind = classify(value)
    return Identity(country_code(country), kind, display, norm_of(display, kind))


def kind_of(value: str) -> str:
    if value not in KINDS:
        raise Invalid("sender_kind must be 'alphanumeric' or 'numeric'")
    return value
