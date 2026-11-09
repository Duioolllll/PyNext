"""Redis ISR storage with atomic leases and fenced writes."""
import hashlib
import json
import math
import uuid

from .cache import (LEASE_TTL, MAX_CACHE_BODY_BYTES, MAX_CACHE_KEY_BYTES, MAX_CACHE_SECONDS,
                    _VARIANT_GENERATION_TTL, SharedCacheUnavailable, SharedEntry, _valid_durations)


_LOOKUP = """
local row = redis.call('HMGET', KEYS[1], 'body', 'fresh_until', 'stale_until', 'version', 'tags', 'generated_at')
if not row[1] then return nil end
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
local fresh = tonumber(row[2])
local stale = tonumber(row[3])
local tags = row[5] or ''
local generated = tonumber(row[6]) or 0
if now >= stale then
  if redis.call('EXISTS', KEYS[2]) == 0 then
    redis.call('DEL', KEYS[1])
    return nil
  end
  return {'', row[4], 'expired', '0', '0', tags, tostring(generated)}
end
if now < fresh then
  return {row[1], row[4], 'fresh', tostring(fresh-now), tostring(stale-fresh), tags, tostring(generated)}
end
return {row[1], row[4], 'stale', '0', tostring(stale-now), tags, tostring(generated)}
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
           'stale_until', now+fresh+stale, 'version', version+1,
           'tags', ARGV[7], 'generated_at', now)
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
            body, version, status, fresh, stale, tags_json, generated_ms = row
            status = status.decode("ascii")
            try:
                tags = tuple(json.loads(tags_json)) if tags_json else ()
            except (ValueError, TypeError):
                tags = ()
            generated_at = int(generated_ms) / 1000 if int(generated_ms) else None
            return SharedEntry(status, None if status == "expired" else body, int(version),
                               int(fresh) / 1000, int(stale) / 1000, generated_at, tags)
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

    def insert_fenced(self, key, body, fresh_seconds, stale_seconds, expected_version, owner=None,
                      tags=()):
        if len(body) > MAX_CACHE_BODY_BYTES or not _valid_durations(fresh_seconds, stale_seconds):
            return False
        fresh_seconds, stale_seconds = float(fresh_seconds), float(stale_seconds)
        tags_json = json.dumps(sorted(set(tags)), ensure_ascii=True, separators=(",", ":"))
        try:
            return bool(self.client.eval(_INSERT, 2, *self._keys(key), bytes(body),
                                         max(1, math.ceil(fresh_seconds * 1000)),
                                         max(0, math.ceil(stale_seconds * 1000)),
                                         -1 if expected_version is None else expected_version,
                                         owner or "", math.ceil(LEASE_TTL * 1000), tags_json))
        except Exception as exc:
            raise SharedCacheUnavailable("Redis cache write failed") from exc

    def insert(self, key, body, fresh_seconds, stale_seconds, tags=()):
        return self.insert_fenced(key, body, fresh_seconds, stale_seconds, None, tags=tags)

    @staticmethod
    def _tag_key(tag):
        # Tag rows only need to outlive the entries they can affect.
        digest = hashlib.sha256(tag.encode("utf-8")).hexdigest()
        return f"pynext:isr-tag:{{{digest}}}"

    def get_tag_times(self, tags):
        try:
            pipe = self.client.pipeline()
            for tag in tags:
                pipe.hmget(self._tag_key(tag), "stale_at", "expired_at")
            out = {}
            for tag, row in zip(tags, pipe.execute()):
                stale, expired = row
                out[tag] = (int(stale) / 1000 if stale is not None else None,
                            int(expired) / 1000 if expired is not None else None)
            return out
        except Exception as exc:
            raise SharedCacheUnavailable("Redis cache tag lookup failed") from exc

    def update_tag(self, tag, stale_at=None, expired_at=None):
        try:
            mapping = {}
            if stale_at is not None:
                mapping["stale_at"] = math.ceil(stale_at * 1000)
            if expired_at is not None:
                mapping["expired_at"] = math.ceil(expired_at * 1000)
            if mapping:
                key = self._tag_key(tag)
                self.client.hset(key, mapping=mapping)
                self.client.pexpire(
                    key, math.ceil((2 * MAX_CACHE_SECONDS + LEASE_TTL) * 1000))
        except Exception as exc:
            raise SharedCacheUnavailable("Redis cache tag update failed") from exc

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
            return token.decode("ascii"), bool(existed)
        except Exception as exc:
            raise SharedCacheUnavailable("Redis cache generation failed") from exc

    def close(self):
        self.client.close()
