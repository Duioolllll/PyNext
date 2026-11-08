"""Trusted-proxy header resolution (A6).

All interpretation of forwarded headers lives in this module. Nothing
else in the codebase parses ``X-Forwarded-For``, ``Forwarded``,
``X-Forwarded-Proto`` or ``X-Forwarded-Host`` directly; the rate limiter,
the ASGI handler and the native bridge all resolve through
:class:`ProxyTrust`.

The one rule that governs everything here: **without a configured
trusted-proxy list, no forwarded header is interpreted at all.** The
direct peer address (the TCP peer for the native server, ``scope["client"]``
for ASGI) is the only client address, the connection scheme is the only
scheme, and the ``Host`` header is the only host. A client behind an
untrusted proxy can put anything in a forwarded header, so trusting it
would let the client pick its own rate-limit bucket, client IP and scheme.

When proxies *are* trusted, the chain is walked from the nearest hop
(rightmost entry) towards the client, stopping at the first address that
is not a trusted proxy. The entries to the left of that hop were written
by something untrusted, so they are never used. ``Forwarded`` (RFC 7239)
takes precedence over ``X-Forwarded-For`` when it carries ``for`` values;
otherwise the de-facto ``X-Forwarded-For`` chain is used.

``proto``/``host`` from a ``Forwarded`` header are only used when the
whole chain was consumed (every hop trusted, so the leftmost element was
written by a trusted proxy). ``X-Forwarded-Proto``/``X-Forwarded-Host``
are single values with no chain, so they are honoured only when the
direct peer is trusted — which means the operator's proxy must overwrite
them (strip any client-supplied value) instead of appending, or a client
can spoof the scheme/host through a trusting proxy. That limitation is
inherent to those headers, not something this module can fix.
"""
import ipaddress
import re
from dataclasses import dataclass

__all__ = ["ProxyTrust", "ResolvedProxy", "parse_trusted_proxies"]

_HOST_RE = re.compile(
    r"^(?:[A-Za-z0-9._~-]+|\[[0-9A-Fa-f:]+\])(?::\d{1,5})?$"
)


def parse_trusted_proxies(trusted_proxies):
    """Normalize trusted-proxy entries to ``ipaddress`` networks.

    Each entry is an IP address (a single host) or a CIDR network.
    Raises ``ValueError`` for anything unparseable so a typo fails at
    startup instead of silently trusting nobody — or everybody.
    """
    networks = []
    for entry in trusted_proxies or ():
        if isinstance(entry, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            networks.append(entry)
            continue
        try:
            networks.append(ipaddress.ip_network(str(entry), strict=False))
        except ValueError as exc:
            raise ValueError(
                f"Invalid trusted proxy {entry!r}: must be an IP address or CIDR network"
            ) from exc
    return tuple(networks)


def _as_text(value):
    if isinstance(value, bytes):
        return value.decode("latin-1")
    return str(value)


def _header_values(headers, name):
    """All values for ``name`` (case-insensitive) as ``str``."""
    wanted = name.lower()
    values = []
    for key, value in headers or ():
        if _as_text(key).lower() == wanted:
            values.append(_as_text(value))
    return values


def _split_quoted(value, separator):
    """Split on ``separator`` outside double quotes.

    Backslash escapes inside quotes are preserved for the unquoting step.
    """
    parts = []
    current = []
    in_quotes = False
    escaped = False
    for char in value:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\" and in_quotes:
            current.append(char)
            escaped = True
        elif char == '"':
            current.append(char)
            in_quotes = not in_quotes
        elif char == separator and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        body = value[1:-1]
        out = []
        escaped = False
        for char in body:
            if escaped:
                out.append(char)
                escaped = False
            elif char == "\\":
                escaped = True
            else:
                out.append(char)
        return "".join(out)
    return value


def _parse_forwarded_element(element):
    """Parse one ``Forwarded`` element into ``{lowercase-key: value}``."""
    params = {}
    for pair in _split_quoted(element, ";"):
        if "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        key = key.strip().lower()
        if key:
            params[key] = _unquote(value)
    return params


def _forwarded_elements(headers):
    """All ``Forwarded`` elements, left-to-right (client first)."""
    elements = []
    for value in _header_values(headers, "forwarded"):
        for element in _split_quoted(value, ","):
            if element.strip():
                elements.append(_parse_forwarded_element(element))
    return elements


def _normalize_ip(value):
    """Parse ``value`` as an IP address, or return ``None``.

    IPv4-mapped IPv6 addresses (``::ffff:192.0.2.1``) normalize to the
    IPv4 string so the same client is not two different keys.
    """
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return str(mapped if mapped is not None else address)


def _parse_forwarded_for(value):
    """Parse one ``Forwarded: for=`` value to a normalized IP, or ``None``.

    Handles ``192.0.2.1``, ``192.0.2.1:1234``, ``[2001:db8::1]`` and
    ``[2001:db8::1]:1234``. Obfuscated values (``_hidden``, ``_secret``)
    and ``unknown`` deliberately yield ``None``: the proxy hid the address,
    so there is nothing trustworthy to walk to.
    """
    value = value.strip()
    if not value or value.lower() == "unknown" or value.startswith("_"):
        return None
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        return _normalize_ip(value[1:end])
    if value.count(":") > 1:
        # Bare IPv6 address (no brackets, no port).
        return _normalize_ip(value)
    if ":" in value:
        # IPv4 (or hostname) with a port; only an IP is usable.
        return _normalize_ip(value.rpartition(":")[0])
    return _normalize_ip(value)


def _parse_xff_value(value):
    """Parse one ``X-Forwarded-For`` entry to a normalized IP, or ``None``.

    Keeps the historical behaviour: entries are bare IPs, optionally in
    brackets for IPv6. Anything else (including a ``:port`` suffix) is
    malformed and stops the walk instead of being guessed at.
    """
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return _normalize_ip(value)


def _valid_scheme(value):
    scheme = value.strip().lower()
    return scheme if scheme in ("http", "https") else None


def _valid_host(value):
    host = value.strip().lower()
    if not host or not _HOST_RE.fullmatch(host):
        return None
    return host


@dataclass(frozen=True)
class ResolvedProxy:
    """Trust-resolved identity of a request.

    ``client_ip`` is the nearest untrusted address seen from the peer
    (or the peer itself when nothing was forwarded). ``scheme``/``host``
    are the forwarded values when a trusted proxy supplied them, else the
    caller-provided defaults. ``client_ip`` is ``None`` only when the
    peer address itself is unknown.
    """

    client_ip: str | None = None
    scheme: str | None = None
    host: str | None = None


class ProxyTrust:
    """Trusted-proxy list plus the forwarded-header resolution rules.

    Construct once (per app / limiter) from the operator's
    ``trusted_proxies`` and reuse it: parsing the list on every request
    would be wasteful, and a single instance keeps every consumer on the
    same trust decision.
    """

    def __init__(self, trusted_proxies=()):
        self.networks = parse_trusted_proxies(trusted_proxies)

    def is_trusted(self, ip):
        """True when ``ip`` is in the trusted-proxy list."""
        if not self.networks or ip is None:
            return False
        address = _normalize_ip(ip)
        if address is None:
            return False
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(parsed in network for network in self.networks)

    def _walk(self, peer_ip, candidates, parse):
        """Walk ``candidates`` (left-to-right, client first) from the peer.

        Returns ``(resolved_ip, consumed_all)``. ``resolved_ip`` is the
        nearest untrusted address, or the peer itself when the peer is not
        trusted. ``consumed_all`` is True only when every candidate was
        consumed — i.e. the whole chain is trusted and the leftmost entry
        may be relied on for ``proto``/``host``.
        """
        if peer_ip is None:
            return None, False
        current = _normalize_ip(peer_ip)
        if current is None:
            # Unparseable peer: preserve it verbatim, as before.
            return peer_ip, False
        if not self.is_trusted(current):
            return current, False
        consumed_all = True
        for candidate in reversed(candidates):
            if not self.is_trusted(current):
                consumed_all = False
                break
            parsed = parse(candidate)
            if parsed is None:
                consumed_all = False
                break
            current = parsed
        else:
            consumed_all = True
        return current, consumed_all

    def resolve_client_ip(self, peer_ip, headers=()):
        """Resolve the client IP from forwarded headers, or the peer.

        Returns ``None`` only when ``peer_ip`` is ``None``. With no
        trusted proxies configured — or an untrusted peer — the forwarded
        headers are ignored entirely and the peer address is returned.
        """
        if peer_ip is None:
            return None
        elements = _forwarded_elements(headers)
        forwarded_for = [el["for"] for el in elements if "for" in el]
        if forwarded_for:
            resolved, _ = self._walk(peer_ip, forwarded_for, _parse_forwarded_for)
            return resolved
        xff = []
        for value in _header_values(headers, "x-forwarded-for"):
            xff.extend(value.split(","))
        resolved, _ = self._walk(peer_ip, xff, _parse_xff_value)
        return resolved

    def resolve_scheme(self, peer_ip, headers=()):
        """Resolve the client-facing scheme, or ``None``.

        ``Forwarded: proto=`` is used only when the whole forwarded chain
        was consumed (every hop trusted). Otherwise a single valid
        ``X-Forwarded-Proto`` value is honoured when the direct peer is
        trusted. ``None`` means "not resolved from a proxy": the caller
        falls back to the connection scheme.
        """
        if peer_ip is None or not self.is_trusted(peer_ip):
            return None
        elements = _forwarded_elements(headers)
        forwarded_for = [el["for"] for el in elements if "for" in el]
        if forwarded_for:
            _, consumed_all = self._walk(peer_ip, forwarded_for, _parse_forwarded_for)
            if consumed_all and elements and "proto" in elements[0]:
                scheme = _valid_scheme(elements[0]["proto"])
                if scheme is not None:
                    return scheme
        values = _header_values(headers, "x-forwarded-proto")
        if len(values) == 1 and "," not in values[0]:
            return _valid_scheme(values[0])
        return None

    def resolve_host(self, peer_ip, headers=()):
        """Resolve the client-facing host, or ``None``.

        Same trust rules as :meth:`resolve_scheme`: ``Forwarded: host=``
        only when the whole chain was consumed, otherwise a single valid
        ``X-Forwarded-Host`` value when the direct peer is trusted.
        ``None`` means "not resolved from a proxy": the caller falls back
        to the ``Host`` header.
        """
        if peer_ip is None or not self.is_trusted(peer_ip):
            return None
        elements = _forwarded_elements(headers)
        forwarded_for = [el["for"] for el in elements if "for" in el]
        if forwarded_for:
            _, consumed_all = self._walk(peer_ip, forwarded_for, _parse_forwarded_for)
            if consumed_all and elements and "host" in elements[0]:
                host = _valid_host(elements[0]["host"])
                if host is not None:
                    return host
        values = _header_values(headers, "x-forwarded-host")
        if len(values) == 1 and "," not in values[0]:
            return _valid_host(values[0])
        return None

    def resolve(self, peer_ip, headers=(), *, default_scheme=None, default_host=None):
        """Resolve client IP, scheme and host in one pass.

        ``default_scheme``/``default_host`` are used when no forwarded
        value was resolved (connection scheme / ``Host`` header).
        """
        return ResolvedProxy(
            client_ip=self.resolve_client_ip(peer_ip, headers),
            scheme=self.resolve_scheme(peer_ip, headers) or default_scheme,
            host=self.resolve_host(peer_ip, headers) or default_host,
        )
