"""IP e klientit pas proxy-ve të besuara (stdlib-only; e përbashkët për Enterprise dhe Central).

Modeli (M11-I/F-02): nginx, proxy-ja e fundit para aplikacionit, zgjidh IP-në reale me `real_ip`
nga proxy-t e besuara të konfiguruar dhe e përcjell si NJË element `X-Forwarded-For` (vlera e
normalizuar `$remote_addr`). Prandaj `hops=1` është vlera e synuar.

- `hops=0`: header-i injorohet, përdoret IP e lidhjes.
- `hops=N>0`: elementi N-nga-fundi i vargut, vetëm nëse vargu ka të paktën N elemente; ndryshe
  kthehet IP e lidhjes (kurrë një element i majtë i kontrolluar nga klienti).
"""

MAX_IP_LEN = 45  # IPv6 i plotë me IPv4 të ngulitur


def client_ip(peer: str | None, forwarded_for: str, hops: int) -> str | None:
    """`peer` = adresa e lidhjes (`request.client.host`); `forwarded_for` = `X-Forwarded-For`."""
    if hops > 0:
        chain = [x.strip() for x in forwarded_for.split(",") if x.strip()]
        if len(chain) >= hops:
            return chain[-hops][:MAX_IP_LEN]
    return peer[:MAX_IP_LEN] if peer else None
