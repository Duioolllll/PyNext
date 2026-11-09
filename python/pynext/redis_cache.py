"""Redis ISR storage with atomic leases and fenced writes."""
import hashlib
import logging
import math
import uuid

from .cache import (LEASE_TTL, MAX_CACHE_BODY_BYTES, MAX_CACHE_KEY_BYTES, _VARIANT_GENERATION_TTL,
                    SharedCacheUnavailable, SharedEntry, _valid_durations)

log = logging.getLogger("pynext")


_LOOKUP = """
local row = redis.call('HMGET', KEYS[1], 'body', 'fresh_until', 'stale_until', 'version')
if not row[1] then return nil end
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local fresh = tonumber(row[2])
local stale = tonumber(row[3])
if now >= stale then
  if redis.call('EXISTS', KEYS[2]) == 0 then
    redis.call('DEL', KEYS[1])
    return nil
  end
  return {'', row[4], 'expired', '0', '0'}
end
if now < fresh then
  return {row[1], row[4], 'fresh', tostring(fresh-now), tostring(stale-fresh)}
end
return {row[1], row[4], 'stale', '0', tostring(stale-now)}
"""

_ACQUIRE = """
if redis.call('EXISTS', KEYS[2]) ~= 0 then return -1 end
redis.call('SET', KEYS[2], ARGV[1], 'PX', ARGV[2])
return tonumber(redis.call('HGET', KEYS[1], 'version') or '0')
"""

_INSERT = """
if ARGV[5] ~= '' and redis.call('GET', KEYS[2]) ~= ARGV[5] then return 0 end
local version = tonumber(redis.call('HGET', KEYS[1], 'version') or '0')
if tonumber(ARGV[4]) >= 0 and version ~= tonumber(ARGV[4]) then return 0 end
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local fresh = tonumber(ARGV[2])
local stale = tonumber(ARGV[3])
redis.call('HSET', KEYS[1], 'body', ARGV[1], 'fresh_until', now+fresh,
           'stale_until', now+fresh+stale, 'version', version+1, 'path', ARGV[7])
redis.call('PEXPIRE', KEYS[1], fresh+stale+tonumber(ARGV[6]))
return 1
"""

_RELEASE = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""

_GENERATION = """
local old = redis.call('GET', KEYS[1])
if not old or ARGV[3] == '1' then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
  return {ARGV[1], old and 1 or 0}
end
return {old, 1}
"""


class RedisCacheBackend:
    """Redis-backed shared cache. Redis failure is reported to ISR's fallback."""

    def __init__(self, url):
        if not isinstance(url, str) or not url.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError("Redis cache requires a redis://, rediss:// or unix:// URL")
        try:
            import redis
        except ImportError as exc:
            raise ValueError("Install pynext-framework[redis] to use Redis") from exc
        self.client = redis.Redis.from_url(url, socket_timeout=1, socket_connect_timeout=1,
                                           decode_responses=False)

    @staticmethod
    def _keys(key):
        if not isinstance(key, str) or len(key.encode("utf-8")) > MAX_CACHE_KEY_BYTES:
            raise ValueError("invalid cache key")
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        base = f"pynext:isr:{{{digest}}}"
        return base + ":entry", base + ":lease"

    def lookup_entry(self, key):
        try:
            row = self.client.eval(_LOOKUP, 2, *self._keys(key))
            if row is None:
                return None
            body, version, status, fresh, stale = row
            status = status.decode("ascii")
            return SharedEntry(status, None if status == "expired" else body, int(version),
                               int(fresh) / 1000, int(stale) / 1000)
        except Exception as exc:
            raise SharedCacheUnavailable("Redis cache lookup failed") from exc

    def lookup(self, key):
        entry = self.lookup_entry(key)
        return (entry.status, entry.body) if entry is not None else ("miss", None)

    def acquire_lease(self, key, owner, ttl=LEASE_TTL):
        try:
            result = self.client.eval(_ACQUIRE, 2, *self._keys(key), owner, max(1, math.ceil(ttl * 1000)))
            return None if result == -1 else int(result)
        except Exception as exc:
            raise SharedCacheUnavailable("Redis cache lease failed") from exc

    def owns_lease(self, key, owner):
        try:
            return self.client.get(self._keys(key)[1]) == owner.encode("utf-8")
        except Exception as exc:
            raise SharedCacheUnavailable("Redis cache lease lookup failed") from exc

    def release_lease(self, key, owner):
        try:
            return bool(self.client.eval(_RELEASE, 1, self._keys(key)[1], owner))
        except Exception as exc:
            raise SharedCacheUnavailable("Redis cache lease release failed") from exc

    def insert_fenced(self, key, body, fresh_seconds, stale_seconds, expected_version, owner=None):
        if len(body) > MAX_CACHE_BODY_BYTES or not _valid_durations(fresh_seconds, stale_seconds):
            return False
        fresh_seconds, stale_seconds = float(fresh_seconds), float(stale_seconds)
        try:
            return bool(self.client.eval(_INSERT, 2, *self._keys(key), bytes(body),
                                         max(1, math.ceil(fresh_seconds * 1000)),
                                         max(0, math.ceil(stale_seconds * 1000)),
                                         -1 if expected_version is None else expected_version,
                                         owner or "", math.ceil(LEASE_TTL * 1000), key))
        except Exception as exc:
            raise SharedCacheUnavailable("Redis cache write failed") from exc

    def insert(self, key, body, fresh_seconds, stale_seconds):
        return self.insert_fenced(key, body, fresh_seconds, stale_seconds, None)

    def remove(self, key):
        try:
            return bool(self.client.delete(*self._keys(key)))
        except Exception as exc:
            raise SharedCacheUnavailable("Redis cache invalidation failed") from exc

    def variant_generation(self, key, *, invalidate=False):
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        try:
            token, existed = self.client.eval(_GENERATION, 1, f"pynext:isr-generation:{{{digest}}}",
                                               uuid.uuid4().hex, _VARIANT_GENERATION_TTL,
                                               "1" if invalidate else "0")
            if invalidate or not existed:
                # The generation key is a hash of the path, so record the
                # path itself alongside it: scoped revalidation finds
                # query-variant pages through this companion key.
                try:
                    self.client.set(f"pynext:isr-generation-path:{{{digest}}}", key,
                                    ex=_VARIANT_GENERATION_TTL)
                except Exception:
                    log.warning("Redis cache generation-path record failed for %s", key,
                                exc_info=True)
            return token.decode("ascii"), bool(existed)
        except Exception as exc:
            raise SharedCacheUnavailable("Redis cache generation failed") from exc

    def keys(self):
        """Return the raw keys this backend currently stores.

        Entry keys are recovered from the ``path`` field each entry
        carries (entries written before that field existed are skipped;
        they expire on their own), and query-variant pages contribute
        their concrete paths through the generation-path companion keys.
        """
        try:
            entry_keys = list(self.client.scan_iter(match="pynext:isr:*:entry"))
            paths = []
            if entry_keys:
                pipe = self.client.pipeline()
                for entry_key in entry_keys:
                    pipe.hget(entry_key, "path")
                for stored in pipe.execute():
                    if stored:
                        paths.append(stored.decode("utf-8"))
            for generation_key in self.client.scan_iter(match="pynext:isr-generation-path:*"):
                stored = self.client.get(generation_key)
                if stored:
                    paths.append(stored.decode("utf-8"))
            return paths
        except Exception as exc:
            raise SharedCacheUnavailable("Redis cache key listing failed") from exc

    def close(self):
        self.client.close()
