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


__all__ = ["FakeProvider", "ProviderError", "SendRequest", "SendResult", "get_provider", "register"]
