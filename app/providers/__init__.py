from app.providers.base import ProviderError, SendRequest, SendResult, SmsProvider
from app.providers.fake import FakeProvider

_registry: dict[str, SmsProvider] = {"fake": FakeProvider()}


def register(provider: SmsProvider) -> None:
    _registry[provider.name] = provider


def get_provider(name: str) -> SmsProvider:
    try:
        return _registry[name]
    except KeyError:
        raise ProviderError("unknown_provider", temporary=False) from None


def register_configured() -> None:
    """Regjistron provider-in HTTP nëse është konfiguruar (thirret në nisje)."""
    from app.core.config import settings
    from app.providers.http import HttpProvider

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
