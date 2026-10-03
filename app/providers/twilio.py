"""Adapter Twilio (SMS). Ndërtuar nga API-ja publike e Twilio Programmable Messaging:

  POST https://api.twilio.com/2010-04-01/Accounts/{AccountSid}/Messages.json
  Auth: HTTP Basic (AccountSid : AuthToken); trup form-urlencoded: To, From | MessagingServiceSid,
  Body, StatusCallback. 201 → {"sid": "SM…", "status": "queued", …}
  Gabim: {"code": 21211, "message": "…", "status": 400}

NUK u testua ende kundër Twilio të vërtetë (mjedisi i zhvillimit s'ka qasje në internet drejt tyre):
algoritmi i nënshkrimit u verifikua me shembullin zyrtar të dokumentacionit; dërgimi provohet me
transport të simuluar. Testi i parë real: docs/TWILIO.md.

Siguria e parave: Twilio nuk ka çelës idempotence për Messages. Pas një gabimi rrjeti ku kërkesa
mund të ketë mbërritur (read timeout etj.) NUK riprovojmë, që marrësi të mos marrë dy SMS; mesazhi
dështon (paratë kthehen) me kod `twilio_outcome_unknown` që stafi ta rakordojë. Vetëm gabimet
para lidhjes (connect) konsiderohen të përkohshme.
"""

import base64
import hashlib
import hmac

import httpx

from app.providers.base import ProviderError, SendRequest, SendResult

API = "https://api.twilio.com/2010-04-01"
# Kode gabimi Twilio që s'kanë kuptim të riprovohen (numër i pavlefshëm, i bllokuar, region, etj.)
PERMANENT_HINT = {21211, 21214, 21408, 21610, 21611, 21612, 21614, 21617, 21619}
FINAL_BAD = {"failed", "undelivered", "canceled"}


def twilio_signature(auth_token: str, url: str, params: list[tuple[str, str]]) -> str:
    """X-Twilio-Signature: base64(HMAC-SHA1(token, URL + çiftet key+value të renditura))."""
    data = url + "".join(k + v for k, v in sorted(params))
    mac = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(mac).decode()


def verify_twilio_signature(
    auth_token: str, url: str, params: list[tuple[str, str]], header: str
) -> bool:
    if not auth_token or not header:
        return False
    return hmac.compare_digest(twilio_signature(auth_token, url, params), header)


def _from(sender: str) -> str:
    """Numrat ruhen pa '+'; Twilio kërkon E.164. Sender-at alfanumerikë kalojnë siç janë."""
    return f"+{sender}" if sender.isdigit() else sender


class TwilioProvider:
    name = "twilio"

    def __init__(
        self,
        account_sid: str,
        auth_token: str,
        status_callback_url: str,
        messaging_service_sid: str = "",
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._sid = account_sid
        self._auth = (account_sid, auth_token)
        self._callback = status_callback_url
        self._service = messaging_service_sid
        self._client = client or httpx.Client(timeout=timeout)

    def _form(self, req: SendRequest) -> dict[str, str]:
        form = {
            "To": f"+{req.destination.lstrip('+')}",
            "Body": req.text,
            "StatusCallback": self._callback,
        }
        if self._service:
            form["MessagingServiceSid"] = self._service
        else:
            form["From"] = _from(req.sender)
        return form

    def send(self, req: SendRequest) -> SendResult:
        try:
            r = self._client.post(
                f"{API}/Accounts/{self._sid}/Messages.json", data=self._form(req), auth=self._auth
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            raise ProviderError(f"network:{type(e).__name__}", temporary=True) from e  # s'u dërgua
        except httpx.HTTPError as e:
            raise ProviderError("twilio_outcome_unknown", temporary=False) from e
        if r.status_code in (401, 403):
            raise ProviderError(
                "twilio_auth", temporary=True
            )  # konfigurim: rregullohet dhe rikthehet
        if r.status_code == 429 or r.status_code in (502, 503, 504):
            raise ProviderError(f"http_{r.status_code}", temporary=True)  # s'u pranua
        if r.status_code >= 500:  # 500: e papritur, mund të jetë krijuar mesazhi → pa riprovë
            raise ProviderError("twilio_outcome_unknown", temporary=False)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            code = data.get("code") if isinstance(data, dict) else None
            permanent = r.status_code < 500
            raise ProviderError(
                f"twilio_{code}" if code else f"http_{r.status_code}", temporary=not permanent
            )
        sid = data.get("sid") if isinstance(data, dict) else None
        if not isinstance(sid, str) or not sid:
            raise ProviderError(
                "twilio_outcome_unknown", temporary=False
            )  # 2xx pa sid: mund të jetë pranuar
        if data.get("status") in FINAL_BAD:
            raise ProviderError(
                f"twilio_{data.get('error_code') or data['status']}", temporary=False
            )
        return SendResult(sid)
