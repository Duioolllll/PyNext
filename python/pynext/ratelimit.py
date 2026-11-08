"""Per-process token-bucket rate limiting (spec v0.2 §11, finding #49).

The default buckets are process-local. RedisRateLimiter provides optional
shared buckets across workers or instances.

Keys are client IPs by default. An IP key is only as trustworthy as the
network in front of the server: a client behind an untrusted proxy can
spoof ``X-Forwarded-For``, so forwarded headers are honoured only when
the direct peer is listed in ``trusted_proxies`` (spec §11: only trusted
proxies may have their forwarded headers interpreted). With no trusted
proxies configured, ``X-Forwarded-For`` is ignored entirely.

When a limiter is configured, static files and fresh ISR pages also pass
through the Python bridge. On the Axum adapter the body has already been read by the time
the bridge runs, so rate limiting happens at dispatch time there, after
the body-intake limits; on the ASGI adapter the check runs before the
body is read.
"""
import ipaddress
import math
import threading
import time
from collections import OrderedDict

from .http import Response

_MAX_BUCKETS = 10_000


class RateLimitUnavailable(Exception):
    """A shared limiter could not safely decide whether to admit a request."""


def _parse_trusted_proxies(trusted_proxies):
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
            raise ValueError(f"Invalid trusted proxy {entry!r}: must be an IP address or CIDR network") from exc
    return tuple(networks)


class RateLimiter:
    """A thread-safe token bucket per key.

    ``limit`` is the burst size and the refill rate is ``limit`` tokens
    per ``window_seconds``. A key that has been idle long enough to refill
    completely is treated as fresh.
    """

    def __init__(self, limit, window_seconds, *, trusted_proxies=()):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("rate limit must be an integer >= 1")
        if isinstance(window_seconds, bool) or not isinstance(window_seconds, (int, float)) \
                or not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("rate limit window must be a number > 0 seconds")
        self.limit = limit
        self.window_seconds = float(window_seconds)
        self._refill_per_second = limit / self.window_seconds
        self.trusted_proxies = _parse_trusted_proxies(trusted_proxies)
        self._lock = threading.Lock()
        # key -> [tokens, last_seen_monotonic]
        self._buckets = OrderedDict()

    def _is_trusted(self, client_ip):
        if not self.trusted_proxies or client_ip is None:
            return False
        try:
            address = ipaddress.ip_address(client_ip)
        except ValueError:
            return False
        return any(address in network for network in self.trusted_proxies)

    def key_for(self, client_ip, headers=()):
        """Derive the bucket key for a request.

        ``headers`` is an iterable of ``(name, value)`` byte pairs. Starting
        at the direct peer, walk ``X-Forwarded-For`` right-to-left only while
        each hop is trusted. The nearest untrusted address is the key.
        A request with no known peer address shares the ``"unknown"``
        bucket.
        """
        if client_ip is None:
            return "unknown"
        try:
            address = ipaddress.ip_address(client_ip)
            current = str(address.ipv4_mapped or address) if isinstance(address, ipaddress.IPv6Address) else str(address)
        except ValueError:
            return client_ip
        if not self._is_trusted(current):
            return current
        forwarded = []
        for name, value in headers:
            if name.lower() == b"x-forwarded-for":
                forwarded.extend(value.decode("latin-1").split(","))
        for candidate in reversed(forwarded):
            if not self._is_trusted(current):
                break
            candidate = candidate.strip()
            if candidate.startswith("[") and candidate.endswith("]"):
                candidate = candidate[1:-1]
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                # Do not trust any hop to the left of malformed input.
                break
            current = str(address.ipv4_mapped or address) if isinstance(address, ipaddress.IPv6Address) else str(address)
        return current

    def allow(self, key, *, now=None):
        """Consume one token for ``key``.

        Returns ``(True, None)`` when the request may proceed, or
        ``(False, retry_after_seconds)`` when the bucket is empty, where
        the retry delay is the whole seconds until the next token is
        available (at least 1).
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                while len(self._buckets) >= _MAX_BUCKETS:
                    _, (_, oldest_seen) = next(iter(self._buckets.items()))
                    remaining = self.window_seconds - max(0.0, now - oldest_seen)
                    if remaining > 0:
                        # Fail closed for new keys instead of evicting an
                        # active bucket or allowing unbounded memory growth.
                        return False, max(1, math.ceil(remaining))
                    self._buckets.popitem(last=False)
                bucket = [float(self.limit), now]
                self._buckets[key] = bucket
            else:
                elapsed = max(0.0, now - bucket[1])
                bucket[0] = min(float(self.limit), bucket[0] + elapsed * self._refill_per_second)
                bucket[1] = now
                self._buckets.move_to_end(key)
            if bucket[0] >= 1.0:
                bucket[0] -= 1.0
                return True, None
            retry_after = max(1, math.ceil((1.0 - bucket[0]) / self._refill_per_second))
            return False, retry_after

    def reset(self):
        """Drop all buckets. Tests only; production buckets refill on their own."""
        with self._lock:
            self._buckets.clear()


def rate_limited_response(retry_after):
    """The 429 response for a request over its rate limit."""
    return Response("Too Many Requests", status=429,
                    headers=((b"retry-after", str(int(retry_after)).encode("ascii")),))
