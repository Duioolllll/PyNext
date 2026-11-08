"""Per-process token-bucket rate limiting (spec v0.2 §11, finding #49).

The default buckets are process-local. RedisRateLimiter provides optional
shared buckets across workers or instances.

Keys are client IPs by default. An IP key is only as trustworthy as the
network in front of the server: a client behind an untrusted proxy can
spoof ``X-Forwarded-For``, so forwarded headers are honoured only when
the direct peer is listed in ``trusted_proxies`` (spec §11: only trusted
proxies may have their forwarded headers interpreted). With no trusted
proxies configured, forwarded headers are ignored entirely. All of that
resolution lives in :mod:`pynext.proxy`; this module only owns the
buckets.

When a limiter is configured, static files and fresh ISR pages also pass
through the Python bridge. On the Axum adapter the body has already been read by the time
the bridge runs, so rate limiting happens at dispatch time there, after
the body-intake limits; on the ASGI adapter the check runs before the
body is read.
"""
import math
import threading
import time
from collections import OrderedDict

from .http import Response
from .proxy import ProxyTrust, parse_trusted_proxies

_MAX_BUCKETS = 10_000


class RateLimitUnavailable(Exception):
    """A shared limiter could not safely decide whether to admit a request."""


def _parse_trusted_proxies(trusted_proxies):
    """Normalize trusted-proxy entries (kept for backward compatibility).

    See :func:`pynext.proxy.parse_trusted_proxies`.
    """
    return parse_trusted_proxies(trusted_proxies)


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
        # One shared trust decision for the limiter and the app: the
        # forwarded-header rules live in pynext.proxy (A6).
        self._trust = ProxyTrust(trusted_proxies)
        self.trusted_proxies = self._trust.networks
        self._lock = threading.Lock()
        # key -> [tokens, last_seen_monotonic]
        self._buckets = OrderedDict()

    def _is_trusted(self, client_ip):
        return self._trust.is_trusted(client_ip)

    def key_for(self, client_ip, headers=()):
        """Derive the bucket key for a request.

        Delegates to :meth:`pynext.proxy.ProxyTrust.resolve_client_ip`:
        the nearest untrusted address walking the forwarded chain from the
        direct peer, or the peer itself when nothing is trusted. A request
        with no known peer address shares the ``"unknown"`` bucket.
        """
        resolved = self._trust.resolve_client_ip(client_ip, headers)
        return resolved if resolved is not None else "unknown"

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
