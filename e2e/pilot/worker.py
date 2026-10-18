"""Nis `app.worker` me FakeProvider që jep id-të e provider-it unike ndërmjet rinisjeve (vetëm harness-i M11-H).

`FakeProvider` numëron `fake-1, fake-2…` për proces: pas çdo rinisjeje id-të përplasen me mesazhe të vjetra dhe një DLR i nënshkruar
zgjidh mesazhin e gabuar. Provider-ët e vërtetë kthejnë id unike; këtu ruhet e njëjta sjellje (rregullat 0001/0002, idempotencë sipas reference).
Nuk ndryshon asnjë kod produkti. Pa provider të vërtetë.

    python -m e2e.pilot.worker --role sms|sender_control_plane|sender_request_reporter
"""

import runpy
import sys
import uuid

import app.providers as providers
from app.providers.base import ProviderError, SendResult
from app.providers.fake import FakeProvider


class UniqueFake(FakeProvider):
    def send(self, req):
        self.calls.append(req)
        if req.destination.endswith("0001"):
            raise ProviderError("fake_temporary", temporary=True)
        if req.destination.endswith("0002"):
            raise ProviderError("fake_rejected", temporary=False)
        if req.reference not in self.accepted:
            self.accepted[req.reference] = SendResult(f"fake-{uuid.uuid4().hex[:16]}")
        return self.accepted[req.reference]


providers._registry["fake"] = UniqueFake()
sys.argv = ["app.worker", *sys.argv[1:]]
runpy.run_module("app.worker", run_name="__main__")
