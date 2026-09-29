from app.providers.base import ProviderError, SendRequest, SendResult, SmsProvider
from app.providers.email import EmailProvider, FakeEmailProvider  # noqa: E402
from app.providers.fake import FakeProvider

_registry: dict[str, SmsProvider] = {"fake": FakeProvider()}
_email_registry: dict[str, EmailProvider] = {"fake": FakeEmailProvider()}


def register(provider: SmsProvider) -> None:
    _registry[provider.name] = provider


def get_provider(name: str) -> SmsProvider:
    try:
        return _registry[name]
    except KeyError:
        raise ProviderError("unknown_provider", temporary=False) from None


def register_email(provider: EmailProvider) -> None:
    _email_registry[provider.name] = provider


def get_email_provider(name: str) -> EmailProvider:
    try:
        return _email_registry[name]
    except KeyError:
        raise ProviderError("unknown_provider", temporary=False) from None


def register_configured() -> None:
    """Regjistron provider-in HTTP nëse është konfiguruar (thirret në nisje)."""
    from app.core.config import settings
    from app.providers.http import HttpProvider

    if settings.smtp_host:
        from app.providers.email import SmtpEmailProvider

        register_email(
            SmtpEmailProvider(
                "smtp", settings.smtp_host, settings.smtp_port, settings.smtp_user,
                settings.smtp_password, settings.smtp_starttls,
            )
        )  # fmt: skip
    if settings.http_provider_url:
        register(
            HttpProvider(
                settings.http_provider_name,
                settings.http_provider_url,
                settings.http_provider_key,
                settings.http_provider_timeout,
            )
        )


__all__ = [
    "FakeProvider",
    "ProviderError",
    "SendRequest",
    "SendResult",
    "get_provider",
    "register",
    "register_configured",
]
