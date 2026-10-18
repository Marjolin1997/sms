"""Enkriptim simetrik (Fernet) për sekrete që duhen rikuperuar: çelësa DKIM, sekrete webhook."""

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


def _fernet() -> Fernet:
    if not settings.secrets_key:
        raise RuntimeError("SMS_SECRETS_KEY is not configured")  # fail closed
    return Fernet(settings.secrets_key.encode())


def encrypt(data: bytes) -> str:
    return _fernet().encrypt(data).decode()


def decrypt(token: str) -> bytes:
    try:
        return _fernet().decrypt(token.encode())
    except InvalidToken as e:
        raise RuntimeError("cannot decrypt secret (wrong SMS_SECRETS_KEY?)") from e
