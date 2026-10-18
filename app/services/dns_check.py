"""Rezolver DNS TXT i injektueshëm (testet përdorin një të rremë)."""

from typing import Protocol


class DnsError(Exception):
    """Gabim i përkohshëm DNS (timeout, SERVFAIL); NXDOMAIN nuk është gabim: jep listë bosh."""


class Resolver(Protocol):
    def txt(self, name: str) -> list[str]: ...


class SystemResolver:
    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout

    def txt(self, name: str) -> list[str]:
        import dns.exception
        import dns.resolver

        try:
            answer = dns.resolver.resolve(name, "TXT", lifetime=self.timeout)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return []
        except dns.exception.DNSException as e:
            raise DnsError(str(e)) from e
        # një TXT mund të jetë i ndarë në disa vargje: bashkohen pa hapësirë
        return ["".join(part.decode() for part in r.strings) for r in answer]


_resolver: Resolver = SystemResolver()


def get_resolver() -> Resolver:
    return _resolver


def set_resolver(r: Resolver) -> None:
    global _resolver
    _resolver = r
