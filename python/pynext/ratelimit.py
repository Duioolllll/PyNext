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

IPv6 addresses are grouped by /64 prefix, so one network (not one
address) shares a bucket. A single host rotating addresses inside its
/64 cannot mint fresh buckets, and a /64 with many hosts shares the
limit the way one IPv4 address behind NAT does.

That grouping is a tradeoff, not a cure (external audit, review #4
follow-up). An attacker holding a whole /48 can mint 65,536 distinct
/64s, enough to fill the 10,000-bucket table and the 10,000-per-window
new-key window. Filling the table only evicts buckets now — an evicted
owner gets a fresh full bucket on its next request and is never denied
— but filling the new-key window still denies genuinely brand-new keys
until the window slides. ``ipv6_prefix_len`` lets an operator group
more broadly (48 groups a whole /48 into one bucket, which collapses
that attack) at the cost of collateral blocking: every host sharing
the prefix shares one bucket, so a busy /48 (a carrier, a campus)
throttles its innocent users together. The default stays 64: changing
it is an operator decision for the network being served, not a safe
global default. Lowering ``max_new_keys_per_window`` instead only
slows the attacker down while also admitting fewer legitimate new
users per window, so it does not fix the exhaustion either.

How to narrow the grouping (operator setting, S-P1-26 / M-17):

- CLI: ``pynext start --rate-limit 100 --rate-limit-ipv6-prefix-len 48``
- Config file: ``rate_limit_ipv6_prefix_len = 48`` (``pynext.toml``;
  the same key is ``rate_limit_ipv6_prefix_len`` in ``PyNextConfig``)
- Programmatic: ``RateLimiter(100, 60, ipv6_prefix_len=48)``
- The Redis backend groups the same way: ``RedisRateLimiter`` inherits
  :meth:`RateLimiter.key_for`, so the same prefix applies to the
  shared buckets; the transports call ``key_for`` before ``allow`` on
  either backend.

What 48 buys, quantitatively: one /48 minting distinct /64s collapses
from 65,536 candidate buckets to exactly one bucket and one new-key
admission. Filling the 10,000-per-window new-key window then needs
10,000 distinct /48s instead of a single /48, so the flood that used
to deny brand-new users with 429 can no longer do so from one /48.
Narrow to 48 when new-key-window 429s spike under an IPv6 flood —
``stats()["new_keys_in_window"]`` climbing towards
``max_new_keys_per_window`` while the distinct /64s share few /48s is
the signal — and revert to 64 once the flood subsides, because a /48
shared by many legitimate hosts (carrier-grade NAT, a campus, a cloud
region) throttles those hosts together under one bucket. Narrowing
does not stop an attacker who holds many /48s: each /48 still mints
its own bucket, so a distributed holder can still fill the window;
pair the setting with monitoring of ``new_keys_in_window`` and, if
the abuse persists, with an upstream (WAF / edge) block of the
offending prefixes rather than a still narrower prefix, which only
widens the collateral damage.

Bucket table pressure never blocks an existing key. The table is a
bounded leaderboard of the most recently active keys (at most
``_MAX_BUCKETS``): when it is full, a new key evicts the stalest bucket
— preferably one that has fully refilled, so its owner loses nothing;
otherwise the least-recently-used bucket, whose owner gets a fresh full
bucket on its next request. New-key creation itself is bounded by a
global sliding window (at most ``max_new_keys_per_window`` new keys per
``window_seconds``): beyond that, only brand-new keys are denied with
429 until the window slides, which bounds how fast an attacker can churn
the table to reset buckets. Existing keys are never denied by table
pressure.

Atomicity (S-P1-4): ``RateLimiter.allow`` holds a single
``threading.Lock`` across the whole read-refill-decide-write step, so
two threads can never interleave a check and a consume on the same
bucket. That guarantee is per-process: the bucket table lives in this
process's memory, so with ``--workers N`` each worker enforces its own
copy of the limit and the effective limit is N times the configured
one. A limit shared across workers needs ``RedisRateLimiter``
(``redis_rate.py``), whose single Lua ``EVAL`` is the atomic unit —
see ``tests/test_ratelimit_atomicity.py`` for the multi-worker proof.

When a limiter is configured, static files and fresh ISR pages are also
charged. Axum calls the Python decision callback from the Rust header
stage and can send an allowed static response directly. Both adapters
check admission from headers,
before reading the body or executing a handler. Each admitted request
consumes one token even if the upload is abandoned or later fails a body
or execution limit; it is never charged again by the native bridge.
The development ASGI adapter shares a bounded decision gate between HTTP
and WebSocket (64 unfinished checks, a 2-second deadline for each). Work
that has already started can finish after the response deadline; its
slot is retained until completion. An expired executor-queued check is
discarded before calling the limiter. The host's default executor also
determines how many callbacks can actually run simultaneously.
Paths listed in ``exempt_paths`` skip the check entirely
(for example health-check endpoints that load balancers must always
reach); nothing is exempt by default.
"""
import ipaddress
import math
import threading
import time
from collections import OrderedDict
from contextvars import ContextVar

from .http import Response
from .proxy import ProxyTrust, parse_trusted_proxies

_MAX_BUCKETS = 10_000
_IPV6_PREFIX_LEN = 64
_decoded_exemption: ContextVar[bool] = ContextVar("pynext_decoded_exemption", default=False)


class RateLimitUnavailable(Exception):
    """A shared limiter could not safely decide whether to admit a request."""


def _is_exempt_path(limiter, path):
    """Use decoded-path matching while preserving custom exemption hooks."""
    exempt = getattr(limiter, "is_exempt", None)
    decoded_exempt = getattr(limiter, "is_exempt_path", None)
    if decoded_exempt and (exempt is None or getattr(exempt, "__func__", None) is RateLimiter.is_exempt):
        return bool(decoded_exempt(path))
    if exempt is None:
        return False
    # Custom hooks may delegate to the query-aware public helper. Preserve
    # the caller's decoded-path contract even through str(path) rewrites,
    # without changing public is_exempt() calls outside this callback.
    token = _decoded_exemption.set(True)
    try:
        return bool(exempt(path))
    finally:
        _decoded_exemption.reset(token)


def _parse_trusted_proxies(trusted_proxies):
    """Normalize trusted-proxy entries (kept for backward compatibility).

    See :func:`pynext.proxy.parse_trusted_proxies`.
    """
    return parse_trusted_proxies(trusted_proxies)


def _parse_exempt_paths(exempt_paths):
    """Normalize exempt path prefixes.

    Each entry is a path prefix such as ``"/healthz"`` or ``"/static/"``.
    A request path is exempt when it equals a prefix or lives under it
    (``"/healthz"`` exempts ``"/healthz"`` and ``"/healthz/ready"`` but
    not ``"/healthz2"``). Raises ``ValueError`` for anything that is not
    an absolute path.
    """
    prefixes = []
    for entry in exempt_paths or ():
        if not isinstance(entry, str) or not entry.startswith("/"):
            raise ValueError(f"Invalid exempt path {entry!r}: must be an absolute path starting with '/'")
        prefixes.append(entry)
    return tuple(prefixes)


def _bucket_key(address, prefix_len=_IPV6_PREFIX_LEN):
    """Collapse an address to its rate-limit bucket key.

    IPv6 addresses share the bucket of their ``prefix_len`` prefix;
    IPv4-mapped IPv6 addresses use their IPv4 address; IPv4 addresses
    are unchanged.
    """
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            return str(mapped)
        network = ipaddress.IPv6Network(f"{address}/{prefix_len}", strict=False)
        return f"{network.network_address}/{prefix_len}"
    return str(address)


class RateLimiter:
    """A thread-safe token bucket per key.

    ``limit`` is the burst size and the refill rate is ``limit`` tokens
    per ``window_seconds``. A key that has been idle long enough to refill
    completely is treated as fresh.

    ``allow`` is atomic within this process: one lock covers the whole
    get-refill-decide-set step (S-P1-4), so concurrent threads can never
    over-admit a bucket. The table itself is per-process — see the module
    docstring: a limit shared across workers needs ``RedisRateLimiter``.
    """

    def __init__(self, limit, window_seconds, *, trusted_proxies=(), exempt_paths=(),
                 max_buckets=None, max_new_keys_per_window=None, ipv6_prefix_len=_IPV6_PREFIX_LEN):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("rate limit must be an integer >= 1")
        if isinstance(window_seconds, bool) or not isinstance(window_seconds, (int, float)) \
                or not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("rate limit window must be a number > 0 seconds")
        if (isinstance(ipv6_prefix_len, bool) or not isinstance(ipv6_prefix_len, int)
                or not 0 <= ipv6_prefix_len <= 128):
            raise ValueError("ipv6_prefix_len must be an integer between 0 and 128")
        self.ipv6_prefix_len = ipv6_prefix_len
        if max_buckets is not None:
            if isinstance(max_buckets, bool) or not isinstance(max_buckets, int) or max_buckets < 1:
                raise ValueError("max_buckets must be an integer >= 1")
        if max_new_keys_per_window is not None:
            if isinstance(max_new_keys_per_window, bool) or not isinstance(max_new_keys_per_window, int) \
                    or max_new_keys_per_window < 1:
                raise ValueError("max_new_keys_per_window must be an integer >= 1")
        self.limit = limit
        self.window_seconds = float(window_seconds)
        self._refill_per_second = limit / self.window_seconds
        # One shared trust decision for the limiter and the app: the
        # forwarded-header rules live in pynext.proxy (A6).
        self._trust = ProxyTrust(trusted_proxies)
        self.trusted_proxies = self._trust.networks
        self.exempt_paths = _parse_exempt_paths(exempt_paths)
        # Table size and new-key admission bound; both default to the
        # module-wide cap so a burst of legitimate new clients is not
        # throttled by default.
        self.max_buckets = max_buckets if max_buckets is not None else _MAX_BUCKETS
        self.max_new_keys_per_window = max_new_keys_per_window if max_new_keys_per_window is not None \
            else self.max_buckets
        self._lock = threading.Lock()
        # key -> [tokens, last_seen_monotonic], most-recently-used last.
        self._buckets = OrderedDict()
        # Monotonic timestamps of new-bucket admissions in the current
        # window (the global sliding window for new keys), keyed by the
        # admitted key: the key is what tells an evicted existing key
        # apart from a brand-new one when its bucket is gone.
        self._new_keys = OrderedDict()
        self._evicted = 0

    def _is_trusted(self, client_ip):
        return self._trust.is_trusted(client_ip)

    def is_exempt(self, path):
        """Match a raw path/query, or the decoded path inside an exemption hook.

        Adapter callbacks preserve a literal ``?`` even when an override
        delegates here. Direct public calls retain query-aware matching.
        """
        if not path:
            return False
        value = str(path)
        if not _decoded_exemption.get():
            value = value.split("?", 1)[0]
        return self.is_exempt_path(value)

    def is_exempt_path(self, path):
        """Match an already decoded URL path, preserving literal ``?`` characters."""
        if not self.exempt_paths or not path:
            return False
        path = str(path)
        for prefix in self.exempt_paths:
            base = prefix.rstrip("/") or "/"
            if path == prefix or path == base or path.startswith(base + "/"):
                return True
        return False

    def key_for(self, client_ip, headers=()):
        """Derive the bucket key for a request.

        Delegates to :meth:`pynext.proxy.ProxyTrust.resolve_client_ip`:
        the nearest untrusted address walking the forwarded chain from the
        direct peer, or the peer itself when nothing is trusted. A request
        with no known peer address shares the ``"unknown"`` bucket. IPv6
        addresses are grouped by the ``ipv6_prefix_len`` prefix (/64 by
        default; see :func:`_bucket_key`).
        """
        resolved = self._trust.resolve_client_ip(client_ip, headers)
        if resolved is None:
            return "unknown"
        try:
            return _bucket_key(ipaddress.ip_address(resolved), self.ipv6_prefix_len)
        except ValueError:
            return resolved

    def _prune_new_keys(self, now):
        """Drop new-key admissions older than one window."""
        cutoff = now - self.window_seconds
        while self._new_keys:
            oldest = next(iter(self._new_keys.values()))
            if oldest > cutoff:
                break
            self._new_keys.popitem(last=False)

    def _evict_one(self, now):
        """Evict one bucket to make room for a new key.

        Prefer a bucket that has fully refilled (its owner loses nothing);
        otherwise evict the least-recently-used bucket. The evicted owner
        gets a fresh full bucket on its next request, so eviction can
        never deny an existing key — it only resets its token count.
        """
        for key, (_, last_seen) in self._buckets.items():
            if now - last_seen >= self.window_seconds:
                del self._buckets[key]
                self._evicted += 1
                return
        self._buckets.popitem(last=False)
        self._evicted += 1

    def stats(self):
        """Bounded-table counters: live buckets, evictions, new keys in the window."""
        with self._lock:
            self._prune_new_keys(time.monotonic())
            return {"buckets": len(self._buckets), "max_buckets": self.max_buckets,
                    "evicted": self._evicted, "new_keys_in_window": len(self._new_keys)}

    def allow(self, key, *, now=None):
        """Consume one token for ``key``.

        Returns ``(True, None)`` when the request may proceed, or
        ``(False, retry_after_seconds)`` when the bucket is empty, where
        the retry delay is the whole seconds until the next token is
        available (at least 1). A brand-new key is denied only when the
        global new-key window for this window is full; an existing key
        is never denied because the bucket table is full.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            self._prune_new_keys(now)
            bucket = self._buckets.get(key)
            if bucket is None:
                # Only a brand-new key is denied by the global new-key
                # window. A key admitted within this window whose bucket
                # was evicted under table pressure since is an existing
                # key: it is never denied, it gets a fresh full bucket.
                if key not in self._new_keys and len(self._new_keys) >= self.max_new_keys_per_window:
                    oldest = next(iter(self._new_keys.values()))
                    retry_after = max(1, math.ceil(self.window_seconds - max(0.0, now - oldest)))
                    return False, retry_after
                if len(self._buckets) >= self.max_buckets:
                    self._evict_one(now)
                bucket = [float(self.limit), now]
                self._buckets[key] = bucket
                self._new_keys[key] = now
                self._new_keys.move_to_end(key)
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
            self._new_keys.clear()
            self._evicted = 0


def rate_limited_response(retry_after):
    """The 429 response for a request over its rate limit."""
    return Response("Too Many Requests", status=429,
                    headers=((b"retry-after", str(int(retry_after)).encode("ascii")),))
