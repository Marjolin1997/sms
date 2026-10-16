"""Mbrojtje SSRF për URL-të që na jep klienti (webhooks): vetëm https, vetëm IP publike.

Kufizim i njohur: rezolvojmë para çdo dërgimi por nuk e fiksojmë lidhjen te IP e rezolvuar,
prandaj një sulm DNS-rebinding brenda një dritareje të vogël mbetet teorikisht i mundur. Për
mbrojtje të plotë ekzekuto worker-in webhook në një rrjet pa akses te burimet e brendshme
(egress filtering)."""

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

from app.core.config import settings


class UnsafeUrl(ValueError):
    pass


def _system_resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UnsafeUrl(f"cannot resolve host: {e}") from e
    return sorted({i[4][0] for i in infos})


_resolve: Callable[[str], list[str]] = _system_resolve


def set_resolver(fn: Callable[[str], list[str]]) -> None:
    global _resolve
    _resolve = fn


def get_resolver() -> Callable[[str], list[str]]:
    return _resolve


def _check_ip(ip_text: str) -> None:
    ip = ipaddress.ip_address(ip_text.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        raise UnsafeUrl("destination resolves to a non-public address")


def validate_url(url: str) -> str:
    """Kthen URL-në e normalizuar ose ngre UnsafeUrl."""
    if len(url) > 2000:
        raise UnsafeUrl("url too long")
    parts = urlsplit(url)
    allowed = {"https"} | ({"http"} if settings.webhook_allow_http else set())
    if parts.scheme not in allowed:
        raise UnsafeUrl("url must use https")
    if parts.username or parts.password:
        raise UnsafeUrl("credentials in url are not allowed")
    host = parts.hostname
    if not host or host.lower() in {"localhost"} or host.endswith((".local", ".internal")):
        raise UnsafeUrl("host is not allowed")
    try:
        port = parts.port
    except ValueError as e:
        raise UnsafeUrl("invalid port") from e
    if port is not None and not 1 <= port <= 65535:
        raise UnsafeUrl("invalid port")
    try:
        ipaddress.ip_address(host)
        is_literal = True
    except ValueError:
        is_literal = False
    if is_literal:
        _check_ip(host)  # ngre UnsafeUrl nëse s'është publike
        return url
    ips = _resolve(host)
    if not ips:
        raise UnsafeUrl("host does not resolve")
    for ip in ips:
        _check_ip(ip)
    return url
