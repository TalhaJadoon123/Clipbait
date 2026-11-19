"""SSRF guard for user-supplied URLs.

Clipbait downloads arbitrary URLs, so a naive deployment is a proxy into the
host's private network (cloud metadata endpoints included). We resolve the host
and reject private, loopback, link-local and reserved addresses.

Set ``CLIPBAIT_ALLOW_PRIVATE_URLS=1`` for fully offline/local deployments that
need to ingest from a NAS or a localhost test server.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}


class UnsafeUrl(ValueError):
    pass


def _private_variants() -> tuple[bool, ...]:
    return (True,)


def _is_blocked_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


def ensure_safe_url(url: str, *, allow_private: bool | None = None) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UnsafeUrl("only http(s) URLs are allowed")
    if not parsed.hostname:
        raise UnsafeUrl("URL has no host")
    if parsed.username or parsed.password:
        raise UnsafeUrl("credentials in URLs are not allowed")

    if allow_private is None:
        allow_private = os.getenv("CLIPBAIT_ALLOW_PRIVATE_URLS", "").strip() in {"1", "true", "yes"}
    if allow_private:
        return url

    host = parsed.hostname
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeUrl(f"cannot resolve host: {host}") from exc
    for info in infos:
        if _is_blocked_ip(info[4][0]):
            raise UnsafeUrl("private or reserved network addresses are not allowed")
    return url