import uuid

from app.providers.base import ProviderError, SendRequest, SendResult


class FakeProvider:
    """Provider për teste/zhvillim. Sjellja sipas 4 shifrave të fundit të numrit:
    0001 → gabim i përkohshëm, 0002 → gabim i përhershëm; ndryshe pranohet.
    Idempotent sipas `reference`, si një provider i mirë real."""

    name = "fake"
    # Provuar nga kodi dhe testet: e njëjta `reference` ⇒ i njëjti SendResult (`self.accepted`).
    idempotent_by_reference = True

    def __init__(self, unique_ids: bool = False) -> None:
        self.accepted: dict[str, SendResult] = {}
        self.calls: list[SendRequest] = []
        # M11-J/G-01: instanca default ka prefiks të rastësishëm, që pas rinisjes
        # `fake-1` të mos përplaset me mesazhe historike. Testet: `FakeProvider()`.
        self._prefix = f"fake-{uuid.uuid4().hex[:8]}-" if unique_ids else "fake-"

    def send(self, req: SendRequest) -> SendResult:
        self.calls.append(req)
        if req.destination.endswith("0001"):
            raise ProviderError("fake_temporary", temporary=True)
        if req.destination.endswith("0002"):
            raise ProviderError("fake_rejected", temporary=False)
        if req.reference not in self.accepted:
            self.accepted[req.reference] = SendResult(f"{self._prefix}{len(self.accepted) + 1}")
        return self.accepted[req.reference]
