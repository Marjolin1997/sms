"""Hashimi i fjalëkalimeve me Argon2id (argon2-cffi, parametrat e paracaktuar të bibliotekës).

Asnjë algoritëm i vetë-shkruar: formati i hash-it është vetë-përshkrues (parametrat + kripa brenda),
ndaj rritja e kostos më vonë nuk prish hash-et ekzistuese (`needs_rehash`).
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from apps.central.core.errors import Invalid

MIN_LENGTH = 12
MAX_LENGTH = 128  # kufi sipër: mos lejo fjalëkalime gjigante si vektor DoS

_hasher = PasswordHasher()
_dummy: str | None = None


def validate(password: str) -> str:
    if not isinstance(password, str) or not MIN_LENGTH <= len(password) <= MAX_LENGTH:
        raise Invalid(f"password must be {MIN_LENGTH}..{MAX_LENGTH} characters")
    return password


def hash_password(password: str) -> str:
    return _hasher.hash(validate(password))


def verify_password(password_hash: str, password: str) -> bool:
    """True vetëm për përputhje; çdo hash i keq/i prishur → False (kurrë përjashtim)."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError, TypeError, ValueError, AttributeError):
        return False  # AttributeError: hash jo-string


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, TypeError, ValueError, AttributeError):
        return False


def burn_verification(password: str) -> None:
    """Kosto e njëjtë kohore kur përdoruesi s'ekziston (kundër numërimit të email-eve)."""
    global _dummy
    if _dummy is None:
        _dummy = _hasher.hash("not-a-real-password-for-timing")
    verify_password(_dummy, password if isinstance(password, str) else "")
