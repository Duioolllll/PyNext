"""A bounded shared Redis token bucket using server time."""
import hashlib
import logging
import math
import os

from .failure_policy import FAIL_CLOSED, FAIL_OPEN, normalize_failure_policy
from .ratelimit import RateLimiter, RateLimitUnavailable, _MAX_BUCKETS

log = logging.getLogger("pynext")

_ALLOW = """
local t = redis.call('TIME')
-- Microseconds, not milliseconds: the active set orders buckets by
-- last-touch time, and two buckets created in the same millisecond tie.
-- Redis breaks sorted-set ties lexicographically by member, so a tie
-- evicted the wrong bucket (its key hash sorts first) instead of the
-- stalest one, and the evicted owner was then denied instead of getting
-- a fresh bucket. Two scripts can never run in the same microsecond,
-- so microsecond scores order strictly by last touch. PEXPIRE still
-- takes milliseconds, so the window is kept in ms separately.
local now = t[1] * 1000000 + t[2]
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local window = window_ms * 1000
-- Prune expired active buckets and new-key admissions older than a window.
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now - window)
local row = redis.call('HMGET', KEYS[1], 'tokens', 'last')
local tokens
if not row[1] then
  -- New key: bound new-key churn with a global sliding window. Only
  -- brand-new keys are denied, and only until the window slides. A key
  -- that already has a :newkeys entry was admitted within this window
  -- and lost its bucket to table-pressure eviction since: it is an
  -- existing key, not a brand-new one, so the window must never deny it.
  local seen = redis.call('ZSCORE', KEYS[3], KEYS[1])
  if not seen and redis.call('ZCARD', KEYS[3]) >= tonumber(ARGV[4]) then
    local oldest = redis.call('ZRANGE', KEYS[3], 0, 0, 'WITHSCORES')
    return {0, math.max(1, math.ceil((tonumber(oldest[2]) + window - now) / 1000000))}
  end
  -- Table full: evict the stalest active bucket instead of denying the
  -- new key. Its owner gets a fresh full bucket on its next request,
  -- so table pressure can never deny an existing key. The evicted key
  -- keeps its :newkeys entry: that entry is what marks it as an
  -- existing key when it returns within this window.
  if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[3]) then
    local stalest = redis.call('ZRANGE', KEYS[2], 0, 0)
    if stalest[1] then
      redis.call('DEL', stalest[1])
      redis.call('ZREM', KEYS[2], stalest[1])
    end
  end
  tokens = limit
  redis.call('ZADD', KEYS[3], now, KEYS[1])
  redis.call('PEXPIRE', KEYS[3], window_ms)
else
  tokens = math.min(limit, tonumber(row[1]) + math.max(0, now-tonumber(row[2])) * limit / window)
end
local allowed = 0
local retry = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry = math.max(1, math.ceil((1-tokens)*window/limit/1000000))
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'last', now)
redis.call('PEXPIRE', KEYS[1], window_ms)
redis.call('ZADD', KEYS[2], now+window, KEYS[1])
redis.call('PEXPIRE', KEYS[2], window_ms)
return {allowed, retry}
"""


class RedisRateLimiter(RateLimiter):
    """Shared token buckets; a Redis outage fails closed with HTTP 503.

    Bucket keys are the ``/64``-grouped keys from :meth:`RateLimiter.key_for`
    (the app and native bridge call ``key_for`` before ``allow``). Table
    pressure follows the same policy as the in-process limiter: a new key
    evicts the stalest active bucket instead of being denied, and only
    the global new-key sliding window can deny a brand-new key.

    ``failure_policy`` (roadmap item B4) decides what happens when
    Redis is unreachable: the default ``"fail_closed"`` raises
    :class:`RateLimitUnavailable` (the caller answers 503), while
    ``"fail_open"`` admits the request. Failing open removes abuse
    protection for the duration of the outage, so only use it where
    availability matters more than limiting. The process-local
    :class:`RateLimiter` never raises, so the policy only applies to
    this Redis-backed limiter. An evicted key keeps its new-key-window
    entry, so its return within the window is treated as an existing
    key, never a brand-new one.
    """

    blocking = True

    def __init__(self, limit, window_seconds, *, url=None, namespace="default", trusted_proxies=(),
                 exempt_paths=(), max_buckets=None, max_new_keys_per_window=None,
                 failure_policy=None):
        super().__init__(limit, window_seconds, trusted_proxies=trusted_proxies,
                         exempt_paths=exempt_paths, max_buckets=max_buckets,
                         max_new_keys_per_window=max_new_keys_per_window)
        self.failure_policy = normalize_failure_policy(failure_policy, default=FAIL_CLOSED)
        if limit > 1_000_000 or not 0.001 <= self.window_seconds <= 31_536_000:
            raise ValueError("Redis rate limits require at most 1000000 tokens and a window from 0.001 to 31536000 seconds")
        url = url or os.environ.get("PYNEXT_REDIS_URL")
        if not isinstance(url, str) or not url.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError("Redis rate limiting requires a Redis URL")
        if not isinstance(namespace, str) or not namespace or len(namespace.encode("utf-8")) > 256:
            raise ValueError("rate limit namespace must be a nonempty string up to 256 bytes")
        try:
            import redis
        except ImportError as exc:
            raise ValueError("Install pynext-framework[redis] to use Redis") from exc
        self.client = redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1)
        digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()
        self._base = f"pynext:rate:{{{digest}}}"

    def allow(self, key, *, now=None):
        if now is not None:
            raise ValueError("Redis rate limiting uses Redis server time")
        digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
        bucket = self._base + ":bucket:" + digest
        try:
            allowed, retry = self.client.eval(_ALLOW, 3, bucket, self._base + ":active",
                                              self._base + ":newkeys", self.limit,
                                              max(1, math.ceil(self.window_seconds * 1000)),
                                              self.max_buckets, self.max_new_keys_per_window)
        except Exception as exc:
            if self.failure_policy == FAIL_OPEN:
                log.warning("Redis rate limiter unavailable; admitting request (fail_open)",
                            exc_info=True)
                return True, None
            raise RateLimitUnavailable("Redis rate limiter unavailable") from exc
        return bool(allowed), None if allowed else int(retry)

    def close(self):
        self.client.close()

    def reset(self):
        raise NotImplementedError("Shared Redis buckets expire automatically; use a new namespace for tests")
