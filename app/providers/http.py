"""Adapter HTTP gjenerik (JSON). Kontrata që pret nga provider-i:

  POST {url}   Authorization: Bearer <key>
  {"reference": ..., "from": ..., "to": ..., "text": ..., "encoding": ..., "segments": ...}
  2xx → {"id": "<provider message id>"}

Statuset HTTP: 2xx pranuar; 408/425/429 → e përkohshme DEFINITIVE (s'u përpunua); 5xx, gabime rrjeti
pas lidhjes dhe përgjigje e palexueshme → e përkohshme AMBIGUE (mund të jetë përpunuar); çdo 4xx
tjetër → e përhershme. `reference` (= `public_id`, e qëndrueshme ndër riprova) dërgohet, por
`idempotent_by_reference = False` (M9-a): s'ka kontratë/dokument/test që provon se provider-i
deduplikon sipas saj ⇒ pas rezultati të paqartë mesazhi bëhet UNKNOWN, JO retry. Borxh prodhimi: kur
vendori real konfirmon dedup me `reference` (dhe ka test kundër tij), vendoset True për atë adapter.
"""

import httpx

from app.providers.base import ProviderError, SendRequest, SendResult

TEMPORARY_STATUS = {408, 425, 429}
PRE_SEND_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


class HttpProvider:
    idempotent_by_reference = False  # shih docstring-un e modulit

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
        raise ProviderError("bad_response", temporary=True, ambiguous=True)  # mund të jetë pranuar

    def send(self, req: SendRequest) -> SendResult:
        try:
            r = self._client.post(self._url, json=self._payload(req), headers=self._headers)
        except PRE_SEND_ERRORS as e:  # lidhja s'u vendos: kërkesa s'u dërgua
            raise ProviderError(f"network:{type(e).__name__}", temporary=True) from e
        except httpx.HTTPError as e:  # pas lidhjes (read timeout, reset, ...): rezultat i paqartë
            raise ProviderError(
                f"network:{type(e).__name__}", temporary=True, ambiguous=True
            ) from e
        if r.status_code in TEMPORARY_STATUS:
            raise ProviderError(f"http_{r.status_code}", temporary=True)  # s'u përpunua
        if r.status_code >= 500:
            raise ProviderError(f"http_{r.status_code}", temporary=True, ambiguous=True)
        if r.status_code >= 400:
            raise ProviderError(f"http_{r.status_code}", temporary=False)
        try:
            data = r.json()
        except ValueError:
            data = None
        return SendResult(self._parse(data))
