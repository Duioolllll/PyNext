"""A bounded shared Redis token bucket using server time."""
import hashlib
import math
import os

from .ratelimit import RateLimiter, RateLimitUnavailable, _MAX_BUCKETS

_ALLOW = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local limit = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
local row = redis.call('HMGET', KEYS[1], 'tokens', 'last')
local tokens
if not row[1] then
  if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[3]) then
    local oldest = redis.call('ZRANGE', KEYS[2], 0, 0, 'WITHSCORES')
    return {0, math.max(1, math.ceil((tonumber(oldest[2])-now)/1000))}
  end
  tokens = limit
else
  tokens = math.min(limit, tonumber(row[1]) + math.max(0, now-tonumber(row[2])) * limit / window)
end
local allowed = 0
local retry = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry = math.max(1, math.ceil((1-tokens)*window/limit/1000))
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'last', now)
redis.call('PEXPIRE', KEYS[1], window)
redis.call('ZADD', KEYS[2], now+window, KEYS[1])
redis.call('PEXPIRE', KEYS[2], window)
return {allowed, retry}
"""


class RedisRateLimiter(RateLimiter):
    """Shared token buckets; a Redis outage fails closed with HTTP 503."""

    blocking = True

    def __init__(self, limit, window_seconds, *, url=None, namespace="default", trusted_proxies=()):
        super().__init__(limit, window_seconds, trusted_proxies=trusted_proxies)
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
            allowed, retry = self.client.eval(_ALLOW, 2, bucket, self._base + ":active", self.limit,
                                               max(1, math.ceil(self.window_seconds * 1000)), _MAX_BUCKETS)
        except Exception as exc:
            raise RateLimitUnavailable("Redis rate limiter unavailable") from exc
        return bool(allowed), None if allowed else int(retry)

    def close(self):
        self.client.close()

    def reset(self):
        raise NotImplementedError("Shared Redis buckets expire automatically; use a new namespace for tests")
