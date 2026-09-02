"""Network URL checks shared by untrusted remote-material downloads."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def _normalized_ip_address(value: object) -> IPAddress | None:
    try:
        address = ipaddress.ip_address(str(value))
    except ValueError:
        return None

    mapped_address = getattr(address, "ipv4_mapped", None)
    return mapped_address or address


def is_public_ip_address(value: object) -> bool:
    """Return whether an address is globally routable and safe for remote I/O."""
    address = _normalized_ip_address(value)
    if address is None:
        return False
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_unspecified
        and not address.is_reserved
    )


def public_http_url_addresses(value: object) -> frozenset[IPAddress] | None:
    """Resolve a public HTTP(S) URL, failing closed for unsafe DNS answers."""
    if not isinstance(value, str) or not value:
        return None
    if "\\" in value or any(character.isspace() or ord(character) < 32 for character in value):
        return None

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return None

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None

    direct_address = _normalized_ip_address(hostname)
    if direct_address is not None:
        return (
            frozenset({direct_address})
            if is_public_ip_address(direct_address)
            else None
        )

    request_port = port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        answers = socket.getaddrinfo(
            hostname,
            request_port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except (OSError, UnicodeError):
        return None

    resolved_addresses: set[IPAddress] = set()
    for answer in answers:
        try:
            raw_address = answer[4][0]
        except (IndexError, TypeError):
            return None
        address = _normalized_ip_address(raw_address)
        if address is None or not is_public_ip_address(address):
            return None
        resolved_addresses.add(address)

    return frozenset(resolved_addresses) or None
