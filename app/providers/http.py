"""Adapter HTTP gjenerik (JSON). Kontrata që pret nga provider-i:

  POST {url}   Authorization: Bearer <key>
  {"reference": ..., "from": ..., "to": ..., "text": ..., "encoding": ..., "segments": ...}
  2xx → {"id": "<provider message id>"}

Statuset HTTP: 2xx pranuar; 408/429/5xx dhe gabimet e rrjetit → e përkohshme (retry);
çdo 4xx tjetër → e përhershme. `reference` dërgohet që provider-i të deduplikojë.
Kur të kemi dokumentimin e vendorit real, ndryshon vetëm `_payload` / `_parse`.
"""

import httpx

from app.providers.base import ProviderError, SendRequest, SendResult

TEMPORARY_STATUS = {408, 425, 429}


class HttpProvider:
    def __init__(
        self,
        name: str,
        url: str,
        api_key: str,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.name = name
        self._url = url
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = client or httpx.Client(timeout=timeout)

    @staticmethod
    def _payload(req: SendRequest) -> dict:
        return {
            "reference": req.reference,
            "from": req.sender,
            "to": req.destination,
            "text": req.text,
            "encoding": req.encoding,
            "segments": req.segments,
        }

    @staticmethod
    def _parse(data: object) -> str:
        if isinstance(data, dict) and isinstance(data.get("id"), str) and data["id"]:
            return data["id"]
        raise ProviderError("bad_response", temporary=True)  # mund të jetë pranuar; retry i sigurt

    def send(self, req: SendRequest) -> SendResult:
        try:
            r = self._client.post(self._url, json=self._payload(req), headers=self._headers)
        except httpx.HTTPError as e:
            raise ProviderError(f"network:{type(e).__name__}", temporary=True) from e
        if r.status_code >= 500 or r.status_code in TEMPORARY_STATUS:
            raise ProviderError(f"http_{r.status_code}", temporary=True)
        if r.status_code >= 400:
            raise ProviderError(f"http_{r.status_code}", temporary=False)
        try:
            data = r.json()
        except ValueError:
            data = None
        return SendResult(self._parse(data))
