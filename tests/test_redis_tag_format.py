"""Regression tests for A-4: Redis tag-key format collision.

The handler and the backend share the ``pynext:isr:tag:<sha256>`` key,
so both must use the same encoding. The handler used to write a hash
(``HSET {stale_at, expired_at}``) while the backend wrote a string
(``SET "stale_ms,expired_ms"``); reading a key written by the other
API raised WRONGTYPE. Both now use the backend's string encoding.

No live Redis is needed: the tag APIs only touch ``get``/``set``/
``mget``/``time``, which the in-memory fake below implements with
strict key typing — a string key read as a hash (or vice versa) raises,
exactly like a real server would.
"""

import math
import threading
import time

import pytest

from pynext.cache import RedisCacheHandler
from pynext.redis_cache import RedisCacheBackend


class _WrongTypeError(Exception):
    """Stand-in for ``redis.exceptions.ResponseError`` (WRONGTYPE)."""


class _FakeRedis:
    """Minimal strict-typed fake for the tag-API command subset."""

    def __init__(self, now_ms=None):
        self._strings = {}
        self._hashes = {}
        self._now_ms = now_ms if now_ms is not None else math.ceil(time.time() * 1000)

    def time(self):
        return (self._now_ms // 1000, (self._now_ms % 1000) * 1000)

    def get(self, key):
        if key in self._hashes:
            raise _WrongTypeError("WRONGTYPE Operation against a key holding the wrong kind of value")
        return self._strings.get(key)

    def set(self, key, value, px=None, ex=None):
        if key in self._hashes:
            raise _WrongTypeError("WRONGTYPE Operation against a key holding the wrong kind of value")
        self._strings[key] = value if isinstance(value, bytes) else value.encode("ascii")
        return True

    def mget(self, keys):
        return [self.get(key) for key in keys]

    def hset(self, key, mapping=None, **kwargs):
        if key in self._strings:
            raise _WrongTypeError("WRONGTYPE Operation against a key holding the wrong kind of value")
        fields = dict(mapping or {})
        fields.update(kwargs)
        self._hashes.setdefault(key, {}).update(fields)
        return len(fields)

    def hmget(self, key, *fields):
        if key in self._strings:
            raise _WrongTypeError("WRONGTYPE Operation against a key holding the wrong kind of value")
        row = self._hashes.get(key, {})
        return [row.get(field) for field in fields]

    def hdel(self, key, *fields):
        if key in self._strings:
            raise _WrongTypeError("WRONGTYPE Operation against a key holding the wrong kind of value")
        row = self._hashes.get(key, {})
        removed = sum(1 for field in fields if row.pop(field, None) is not None)
        return removed

    def delete(self, *keys):
        removed = 0
        for key in keys:
            removed += bool(self._strings.pop(key, None) is not None
                            or self._hashes.pop(key, None) is not None)
        return removed

    def pexpire(self, key, ms):
        return key in self._strings or key in self._hashes

    def close(self):
        return None


@pytest.fixture
def pair():
    """A (handler, backend) pair sharing one fake client.

    Built with ``object.__new__`` so the test needs neither the
    ``redis`` package nor a live server — only the tag APIs run.
    """
    fake = _FakeRedis()
    backend = object.__new__(RedisCacheBackend)
    backend.client = fake
    handler = object.__new__(RedisCacheHandler)
    handler.backend = backend
    handler._lock = threading.RLock()
    return handler, backend, fake


def test_handler_update_tags_readable_by_backend(pair):
    """A-4: a tag written by the handler must read back via the backend."""
    handler, backend, fake = pair
    now_ms = fake._now_ms
    handler.update_tags(["t1"])
    stale_at, expired_at = backend.get_tag_times(["t1"])["t1"]
    assert stale_at == pytest.approx(now_ms / 1000, abs=2.0)
    assert expired_at == pytest.approx(now_ms / 1000, abs=2.0)
    # The backend judges an entry generated before the invalidation expired.
    # ``now`` is pushed slightly past the write instant because the handler
    # stamps with the real wall clock while the fake pins its own time.
    assert backend._tag_status(["t1"], now_ms - 60_000, now_ms + 5_000) == "expired"
    assert backend._tag_status(["t1"], now_ms + 60_000, now_ms + 65_000) == "fresh"


def test_backend_revalidate_tag_readable_by_handler(pair):
    """A-4: a tag written by the backend must read back via the handler."""
    handler, backend, fake = pair
    now_ms = fake._now_ms
    assert backend.revalidate_tag("t2")
    stale_at, expired_at = handler._tag_times(["t2"])["t2"]
    assert stale_at == pytest.approx(now_ms / 1000, abs=2.0)
    assert expired_at == pytest.approx(now_ms / 1000, abs=2.0)
    assert handler.get_expiration(["t2"]) == pytest.approx(now_ms / 1000, abs=2.0)


def test_deferred_expire_stays_stale_across_apis(pair):
    """A-4: a deferred invalidation reads as stale from both APIs."""
    handler, backend, fake = pair
    now_ms = fake._now_ms
    handler.update_tags(["t3"], {"expire": 3600})
    stale_at, expired_at = backend.get_tag_times(["t3"])["t3"]
    assert stale_at == pytest.approx(now_ms / 1000, abs=2.0)
    assert expired_at == pytest.approx(now_ms / 1000 + 3600, abs=2.0)
    assert backend._tag_status(["t3"], now_ms - 60_000, now_ms) == "stale"
    handler_stale, handler_expired = handler._tag_times(["t3"])["t3"]
    assert handler_stale == pytest.approx(stale_at, abs=0.001)
    assert handler_expired == pytest.approx(expired_at, abs=0.001)


def test_mixed_tag_writes_do_not_raise_wrongtype(pair):
    """A-4: alternating handler/backend writes to one tag must not collide."""
    handler, backend, _fake = pair
    assert backend.revalidate_tag("t4")
    handler.update_tags(["t4"])
    assert backend.revalidate_tag("t4", expire=60)
    handler.update_tags(["t4"], {"expire": 60})
    # Both APIs read the same key without WRONGTYPE.
    assert backend.get_tag_times(["t4"])["t4"][0] is not None
    assert handler._tag_times(["t4"])["t4"][0] is not None
    assert handler.get_expiration(["t4"]) > 0.0
