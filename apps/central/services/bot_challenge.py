"""Kufiri i sfidës anti-bot (M8-e) — i PAVARUR nga vendori (pa Google/hCaptcha/Turnstile ende).

`BotChallengeVerifier.verify(token, remote_ip)` ⇒ bool. Rutat publike e thërrasin vetëm kur
`CENTRAL_PUBLIC_REGISTRATION_REQUIRE_CHALLENGE=true`; adapteri i vendorit vjen më vonë. Fail-closed:
nëse sfida kërkohet por provider-i është `disabled`, çdo kërkesë refuzohet (readiness e raporton).
`fake` (token "pass") është vetëm për test/dev dhe refuzohet në prodhim.
"""

from typing import Protocol

from apps.central.core.config import settings


class BotChallengeVerifier(Protocol):
    def verify(self, token: str | None, remote_ip: str | None) -> bool: ...


class DisabledVerifier:
    def verify(self, token, remote_ip) -> bool:
        return False  # asnjë provider ⇒ s'mund të vërtetohet asgjë (fail-closed)


class FakeVerifier:
    def verify(self, token, remote_ip) -> bool:
        return token == "pass"


def get_verifier() -> BotChallengeVerifier:
    return FakeVerifier() if settings.bot_challenge == "fake" else DisabledVerifier()


def required() -> bool:
    return bool(settings.public_registration_require_challenge)
