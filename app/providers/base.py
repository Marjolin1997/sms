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
    """temporary=True → retry me backoff; False → dështim përfundimtar.

    `ambiguous=True` (M9-a): kërkesa MUND të ketë mbërritur/përpunuar te provider-i (timeout pas
    dërgimit, lidhje e ndërprerë, 5xx, përgjigje e palexueshme). Rezultati është i panjohur: pa
    idempotencë të provuar NUK riprovohet dhe NUK dështohet (mesazhi bëhet UNKNOWN, hold-i mbahet).
    `ambiguous=False` = provider-i definitivisht s'e pranoi (refuzim, 429, lidhje e pavendosur)."""

    def __init__(self, code: str, temporary: bool, *, ambiguous: bool = False):
        super().__init__(code)
        self.code = code
        self.temporary = temporary
        self.ambiguous = ambiguous


def is_idempotent(provider: object) -> bool:
    """Aftësia `idempotent_by_reference` (default FALSE). Vetëm adapterët që e shpallin eksplicit
    `True` — me provë (kod+test) që e njëjta `reference` dedupe — lejojnë retry pas rezultati të
    paqartë. Kontroll në memorie (pa kërkim te provider-i në rrugën e nxehtë)."""
    return getattr(provider, "idempotent_by_reference", False) is True


class SmsProvider(Protocol):
    name: str
    idempotent_by_reference: bool  # default False; shih `is_idempotent`

    def send(self, req: SendRequest) -> SendResult: ...
