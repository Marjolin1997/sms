"""IP e klientit për log-et dhe kontekstin e kërkesave Central (M11-I/F-02).

Përdor të njëjtën logjikë si Enterprise (`packages/netutil`) dhe `CENTRAL_TRUSTED_PROXY_HOPS`:
me `hops=1` (nginx i Central normalizon `X-Forwarded-For` me `real_ip`) merret elementi i fundit;
me `hops=0` ose varg të pamjaftueshëm, IP e lidhjes. Asnjëherë një element i majtë i kontrolluar
nga klienti. Përdoret për LOG, jo për vendime autorizimi (autentikimi mbetet me
fjalëkalim/JWT/nënshkrim shërbimi).
"""

from fastapi import Request

from apps.central.core.config import settings
from packages import netutil


def client_ip(request: Request) -> str | None:
    peer = request.client.host if request.client else None
    forwarded = request.headers.get("x-forwarded-for", "")
    return netutil.client_ip(peer, forwarded, settings.trusted_proxy_hops)


def client_ip_or_unknown(request: Request) -> str:
    return client_ip(request) or "unknown"
