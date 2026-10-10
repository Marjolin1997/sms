"""Gateway pagesash (adapter). Vendori real (Stripe, Paddle, banka lokale...) zbatohet këtu:
create_checkout kthen një URL ku klienti paguan; rezultati vjen me webhook të nënshkruar."""

import itertools
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.providers.base import ProviderError


@dataclass(frozen=True)
class CheckoutSession:
    external_id: str
    url: str


class PaymentGateway(Protocol):
    name: str

    def create_checkout(
        self, reference: str, amount: Decimal, currency: str, description: str
    ) -> CheckoutSession: ...


class FakePaymentGateway:
    name = "fake"

    def __init__(self) -> None:
        self._n = itertools.count(1)
        self.sessions: list[dict] = []
        self.fail = False

    def create_checkout(self, reference, amount, currency, description) -> CheckoutSession:
        if self.fail:
            raise ProviderError("gateway_down", temporary=True)
        sid = f"cs_fake_{next(self._n)}"
        self.sessions.append({"id": sid, "reference": reference, "amount": amount,
                              "currency": currency, "description": description})  # fmt: skip
        return CheckoutSession(sid, f"https://pay.example.com/checkout/{sid}")


_gateways: dict[str, PaymentGateway] = {"fake": FakePaymentGateway()}


def register_gateway(g: PaymentGateway) -> None:
    _gateways[g.name] = g


def get_gateway(name: str) -> PaymentGateway:
    try:
        return _gateways[name]
    except KeyError:
        raise ProviderError("unknown_gateway", temporary=False) from None
