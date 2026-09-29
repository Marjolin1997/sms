from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SendRequest:
    reference: str  # public_id i mesazhit; provider-i e përdor për deduplikim
    sender: str
    destination: str
    text: str
    encoding: str
    segments: int


@dataclass(frozen=True)
class SendResult:
    provider_message_id: str


class ProviderError(Exception):
    """temporary=True → retry me backoff; False → dështim përfundimtar."""

    def __init__(self, code: str, temporary: bool):
        super().__init__(code)
        self.code = code
        self.temporary = temporary


class SmsProvider(Protocol):
    name: str

    def send(self, req: SendRequest) -> SendResult: ...
