"""Strict WebSocket Origin validation shared by ASGI and Axum.

Allowlist semantics (S-P0-7): the handshake is checked exactly once,
before the 101 response, by :func:`websocket_origin_allowed`, which
both transports call — the ASGI handler in :mod:`pynext.app` and the
native upgrade branch in ``crates/pynext-native`` (via the
``websocket_origin_callback`` in :mod:`pynext.server`). A denied
handshake is answered HTTP 403 and logged on both transports, never
silently closed.

An origin is allowed when it either matches an entry of
``SecurityConfig.websocket_allowed_origins`` exactly — scheme, host
and port all take part, with the default port implied
(``https://client.example`` matches ``:443`` and nothing else) — or
equals the request's effective origin (the ``Host`` header, resolved
through :class:`~pynext.proxy.ProxyTrust` for trusted proxies only).
``*``, ``null``, userinfo, paths, queries and fragments are not valid
allowlist values and are rejected at config time. A request with a
duplicate or unparseable ``Origin`` header, or a duplicate ``Host``
header, is denied. A missing ``Origin`` is denied unless
``websocket_allow_missing_origin`` explicitly opts non-browser clients
in. Origin checking is not authentication: the header is
client-controlled and can be forged or omitted, so handlers must
still establish identity (session / ``@require_auth``).
"""

import ipaddress
import re
from urllib.parse import urlsplit


_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_AUTHORITY = re.compile(r"(?:\[[0-9a-fA-F:.]+\]|[a-zA-Z0-9.-]+)(?::[0-9]+)?\Z")


def canonical_origin(value):
    """Return (scheme, host, port) for a serialized HTTP origin, else None."""
    if (not isinstance(value, str) or not value or len(value) > 2048 or
            not value.isascii() or any(ord(c) < 33 or ord(c) == 127 for c in value) or
            "?" in value or "#" in value or "\\" in value):
        return None
    try:
        parts = urlsplit(value)
        scheme = parts.scheme.lower()
        host = parts.hostname
        port = parts.port
        if (scheme not in {"http", "https"} or not _AUTHORITY.fullmatch(parts.netloc) or not host or parts.path or
                parts.username is not None or parts.password is not None or parts.netloc.endswith(":")):
            return None
        if port is not None and not 1 <= port <= 65535:
            return None
    except ValueError:
        return None
    host = host.lower()
    if "%" in host:
        return None
    if ":" in host:
        try:
            host = str(ipaddress.IPv6Address(host))
        except ValueError:
            return None
    else:
        labels = host[:-1].split(".") if host.endswith(".") else host.split(".")
        if len(host) > 253 or any(not _DNS_LABEL.fullmatch(label) for label in labels):
            return None
    return scheme, host, port or (443 if scheme == "https" else 80)


def websocket_origin_allowed(headers, peer_ip, scheme, proxy_trust, config):
    """Reject invalid/duplicate origins; allow configured or trusted same-origin requests."""
    try:
        raw_headers = tuple(headers)
        if any(not isinstance(name, (bytes, bytearray, memoryview)) or
               not isinstance(value, (bytes, bytearray, memoryview)) for name, value in raw_headers):
            return False
        headers = tuple((bytes(name).lower(), bytes(value)) for name, value in raw_headers)
    except (TypeError, ValueError):
        return False
    hosts = [value for name, value in headers if name == b"host"]
    if len(hosts) > 1:
        return False
    values = [value for name, value in headers if name == b"origin"]
    if not values:
        return config.websocket_allow_missing_origin is True
    if len(values) != 1:
        return False
    try:
        origin = canonical_origin(values[0].decode("ascii"))
    except UnicodeDecodeError:
        return False
    if origin is None:
        return False
    if any(origin == canonical_origin(allowed) for allowed in config.websocket_allowed_origins):
        return True
    if len(hosts) != 1:
        return False
    try:
        host = hosts[0].decode("ascii")
    except UnicodeDecodeError:
        return False
    direct_scheme = {"ws": "http", "wss": "https", "http": "http", "https": "https"}.get(scheme)
    if direct_scheme is None:
        return False
    effective_scheme = proxy_trust.resolve_scheme(peer_ip, headers) or direct_scheme
    effective_host = proxy_trust.resolve_host(peer_ip, headers) or host
    return origin == canonical_origin(f"{effective_scheme}://{effective_host}")
