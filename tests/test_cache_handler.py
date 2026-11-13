"""CacheHandler plugin contract (REPORT.md §2.1 #21 / tracks/c-caching.md P0-5).

Covers the registry, the five-method contract on all three built-in
handlers, tag revalidation through handlers, and ISRCache/create_app
wiring for named, factory and instance handlers. Redis cases skip
without PYNEXT_TEST_REDIS_URL.
"""
import asyncio
import os
import time
import uuid

import pytest

from pynext import Response, create_app
from pynext.build import write_manifest
from pynext.cache import (
    CacheEntry,
    CacheHandler,
    ISRCache,
    MemoryCacheHandler,
    RedisCacheHandler,
    SQLiteCacheHandler,
    _TagManifest,
    _tag_status,
    get_cache_handler_factory,
    list_cache_handlers,
    register_cache_handler,
)


class _DictHandler(CacheHandler):
    """Minimal in-memory handler used to prove custom handlers work."""

    name = "dict-test"

    def __init__(self):
        self._entries = {}
        self._tags = _TagManifest()
        self.set_calls = []

    def get(self, key, soft_tags=()):
        entry = self._entries.get(key)
        if entry is None or entry.status() == "expired":
            self._entries.pop(key, None)
            return None
        tag_state = _tag_status(entry.tags, self._tags.times(entry.tags) if entry.tags else {},
                                entry.timestamp, time.time(), soft_tags)
        if tag_state == "expired":
            return None
        return CacheEntry(value=entry.value, tags=entry.tags, stale=entry.stale,
                          timestamp=entry.timestamp, expire=entry.expire,
                          revalidate=entry.revalidate, tag_state=tag_state)

    def set(self, key, entry):
        stored = CacheEntry(value=bytes(entry.value), tags=tuple(entry.tags),
                            stale=float(entry.stale), timestamp=time.time(),
                            expire=entry.expire, revalidate=float(entry.revalidate))
        self._entries[key] = stored
        self.set_calls.append(key)
        return True

    def remove(self, key):
        return self._entries.pop(key, None) is not None

    def get_expiration(self, tags):
        return self._tags.expiration(tags)

    def update_tags(self, tags, durations=None):
        return self._tags.update(tags, durations)


def _render(body=b"<p>ok</p>"):
    async def render():
        return Response(body)
    return render


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry_lists_builtins():
    assert {"memory", "sqlite", "redis"} <= set(list_cache_handlers())
    assert isinstance(get_cache_handler_factory("memory")(), MemoryCacheHandler)


def test_registry_duplicate_without_override_raises():
    with pytest.raises(ValueError, match="already registered"):
        register_cache_handler("memory", lambda cache_path=None, cache_url=None: None)


def test_registry_override_then_restore():
    original = get_cache_handler_factory("memory")
    try:
        register_cache_handler("memory", lambda cache_path=None, cache_url=None: _DictHandler(),
                               override=True)
        assert isinstance(get_cache_handler_factory("memory")(), _DictHandler)
    finally:
        register_cache_handler("memory", original, override=True)
    assert isinstance(get_cache_handler_factory("memory")(), MemoryCacheHandler)


def test_registry_rejects_bad_name_and_factory():
    with pytest.raises(ValueError):
        register_cache_handler("", lambda cache_path=None, cache_url=None: None)
    with pytest.raises(TypeError):
        register_cache_handler("nope", "not-callable")


def test_isr_cache_unknown_handler_name_raises():
    with pytest.raises(ValueError, match="Unknown cache handler"):
        ISRCache(cache_handler="does-not-exist")


def test_isr_cache_rejects_handler_missing_methods():
    class _Partial:
        def get(self, key, soft_tags=()):
            return None

    with pytest.raises(TypeError, match="must implement"):
        ISRCache(cache_handler=lambda cache_path=None, cache_url=None: _Partial())


def test_isr_cache_rejects_bad_handler_type():
    with pytest.raises(TypeError):
        ISRCache(cache_handler=42)


# ---------------------------------------------------------------------------
# CacheEntry
# ---------------------------------------------------------------------------

def test_cache_entry_status_and_windows():
    now = time.time()
    entry = CacheEntry(value=b"x", revalidate=60, stale=30, timestamp=now)
    assert entry.status(now) == "fresh"
    assert entry.status(now + 61) == "stale"
    assert entry.status(now + 91) == "expired"
    assert entry.fresh_remaining(now) == pytest.approx(60)
    assert entry.stale_remaining(now + 61) == pytest.approx(29)
    stale_tagged = CacheEntry(value=b"x", revalidate=60, stale=30, timestamp=now,
                              tag_state="stale")
    assert stale_tagged.status(now) == "stale"
    assert stale_tagged.fresh_remaining(now) == 0.0


# ---------------------------------------------------------------------------
# Memory handler
# ---------------------------------------------------------------------------

def test_memory_handler_round_trip_remove_and_tags():
    handler = MemoryCacheHandler()
    entry = CacheEntry(value=b"hello", tags=("t1", "t2"), revalidate=60, stale=10)
    assert handler.set("/a", entry)
    got = handler.get("/a")
    assert got.value == b"hello" and got.tags == ("t1", "t2") and got.status() == "fresh"
    assert handler.get_expiration(["t1", "t2"]) == 0.0
    # Immediate expiry invalidates; a soft tag only makes it stale.
    handler.update_tags(["t1"])
    assert handler.get("/a") is None
    assert handler.get_expiration(["t1"]) > 0.0
    assert handler.set("/a", entry)
    handler.update_tags(["t2"], {"expire": 3600})
    got = handler.get("/a")
    assert got is not None and got.tag_state == "stale"
    # Immediate expiry of t2: a hard tag is a miss, a soft tag is stale.
    handler.update_tags(["t2"])
    assert handler.get("/a") is None
    got = handler.get("/a", soft_tags=("t2",))
    assert got is not None and got.tag_state == "stale"
    assert handler.remove("/a")
    assert handler.get("/a") is None
    assert not handler.remove("/a")


def test_memory_handler_rejects_oversized_body_and_bad_tags():
    handler = MemoryCacheHandler()
    assert not handler.set("/big", CacheEntry(value=b"x" * (2_097_152 + 1), revalidate=60))
    assert not handler.set("/bad", CacheEntry(value=b"x", tags=("ok", ""), revalidate=60))
    with pytest.raises(ValueError):
        handler.update_tags(["t"], {"expire": -1})
    with pytest.raises(ValueError):
        handler.get_expiration("not-a-list")


# ---------------------------------------------------------------------------
# SQLite handler
# ---------------------------------------------------------------------------

def test_sqlite_handler_round_trip_and_shared_tags(tmp_path):
    path = str(tmp_path / "cache.sqlite3")
    first = SQLiteCacheHandler(cache_path=path)
    second = SQLiteCacheHandler(cache_path=path)
    try:
        assert first.set("/b", CacheEntry(value=b"world", tags=("x",), revalidate=60, stale=5))
        assert second.get("/b").value == b"world"
        # Tag revalidation written by one instance is visible to the other.
        second.update_tags(["x"])
        assert first.get("/b") is None
        assert first.get_expiration(["x"]) > 0.0
        assert first.set("/b", CacheEntry(value=b"world", tags=("x",), revalidate=60, stale=5))
        second.update_tags(["x"], {"expire": 3600})
        got = first.get("/b")
        assert got is not None and got.tag_state == "stale"
        assert first.remove("/b")
        assert second.get("/b") is None
    finally:
        first.close()
        second.close()


def test_sqlite_handler_reopen(tmp_path):
    path = str(tmp_path / "cache.sqlite3")
    handler = SQLiteCacheHandler(cache_path=path)
    handler.set("/c", CacheEntry(value=b"again", revalidate=60))
    handler.close()
    handler.open()
    try:
        assert handler.get("/c").value == b"again"
    finally:
        handler.close()


def test_sqlite_handler_requires_path():
    with pytest.raises(ValueError, match="cache_path is required"):
        SQLiteCacheHandler()


# ---------------------------------------------------------------------------
# Redis handler
# ---------------------------------------------------------------------------

URL = os.environ.get("PYNEXT_TEST_REDIS_URL")


@pytest.mark.skipif(not URL, reason="PYNEXT_TEST_REDIS_URL is not configured")
def test_redis_handler_round_trip_and_shared_tags():
    first = RedisCacheHandler(cache_url=URL)
    second = RedisCacheHandler(cache_url=URL)
    key = "/pynext-test-" + uuid.uuid4().hex
    try:
        assert first.set(key, CacheEntry(value=b"red", tags=("r",), revalidate=60, stale=5))
        assert second.get(key).value == b"red"
        second.update_tags(["r"])
        assert first.get(key) is None
        assert first.get_expiration(["r"]) > 0.0
        assert first.set(key, CacheEntry(value=b"red", tags=("r",), revalidate=60, stale=5))
        second.update_tags(["r"], {"expire": 3600})
        got = first.get(key)
        assert got is not None and got.tag_state == "stale"
        assert first.remove(key)
        assert second.get(key) is None
    finally:
        first.close()
        second.close()


# ---------------------------------------------------------------------------
# ISRCache wiring
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def test_isr_cache_with_named_sqlite_handler(tmp_path):
    path = str(tmp_path / "isr.sqlite3")
    calls = 0

    async def render():
        nonlocal calls
        calls += 1
        return Response(b"<p>once</p>")

    async def run():
        cache = ISRCache(cache_handler="sqlite", cache_path=path)
        try:
            assert isinstance(cache.handler, SQLiteCacheHandler)
            assert cache.backend_name == "sqlite"
            policy = {"revalidate": 60.0, "stale": 60.0}
            assert await cache.get_or_render("/page", policy, render) == b"<p>once</p>"
            assert await cache.get_or_render("/page", policy, render) == b"<p>once</p>"
            assert calls == 1
            assert await cache.invalidate("/page")
            assert await cache.get_or_render("/page", policy, render) == b"<p>once</p>"
            assert calls == 2
            await cache.close()
            assert cache.shared is None
            cache.open()
            assert cache.shared is not None
            assert await cache.get_or_render("/page", policy, render) == b"<p>once</p>"
            assert calls == 2  # reopened handler still sees the shared entry
        finally:
            await cache.close()

    _run(run())


# ---------------------------------------------------------------------------
# A-1: SharedCacheHandler tag invalidation (revalidate_tag / get_tag_times)
# ---------------------------------------------------------------------------

def test_sqlite_handler_revalidate_tag_and_get_tag_times(tmp_path):
    """A-1: the sqlite handler records tag invalidations where both its
    read paths consult them — the shared manifest (lookup_entry) and
    the handler tag table (get, for entries written via set)."""
    path = str(tmp_path / "cache.sqlite3")
    first = SQLiteCacheHandler(cache_path=path)
    second = SQLiteCacheHandler(cache_path=path)
    try:
        assert first.set("/b", CacheEntry(value=b"world", tags=("x",), revalidate=60, stale=5))
        assert second.get("/b").value == b"world"
        assert first.revalidate_tag("x") is True
        stale_at, expired_at = second.get_tag_times(["x"])["x"]
        assert stale_at is not None and expired_at is not None
        assert second.get("/b") is None  # mirror: set-written entry retired
        assert second.get_tag_times(["never"])["never"] == (None, None)
        # Deferred invalidation demotes to stale instead of expiring.
        assert first.set("/c", CacheEntry(value=b"later", tags=("y",), revalidate=3600, stale=60))
        assert first.revalidate_tag("y", expire=3600) is True
        stale_at, expired_at = second.get_tag_times(["y"])["y"]
        assert expired_at > stale_at
        got = second.get("/c")
        assert got is not None and got.tag_state == "stale"
    finally:
        first.close()
        second.close()


def test_isr_cache_sqlite_handler_cross_worker_tag_invalidation(tmp_path):
    """A-1: revalidate_tag on the sqlite handler path must reach the
    other worker — before the fix the call returned False (the handler
    had no revalidate_tag) and worker 2 kept serving the entry fresh."""
    path = str(tmp_path / "isr.sqlite3")
    calls = 0

    async def render():
        nonlocal calls
        calls += 1
        return Response(b"<p>tagged</p>")

    async def run():
        c1 = ISRCache(cache_handler="sqlite", cache_path=path)
        c2 = ISRCache(cache_handler="sqlite", cache_path=path)
        try:
            policy = {"revalidate": 3600.0, "stale": 3600.0}
            assert await c1.get_or_render("/page", policy, render, tags=("t1",)) == b"<p>tagged</p>"
            assert calls == 1
            entry = await c2._shared_entry("/page")
            assert entry is not None and entry.status == "fresh"
            assert await c1.revalidate_tag("t1") is True
            entry = await c2._shared_entry("/page")
            assert entry is not None and entry.status == "expired"
            assert await c2.get_or_render("/page", policy, render, tags=("t1",)) == b"<p>tagged</p>"
            assert calls == 2
        finally:
            await c1.close()
            await c2.close()

    _run(run())


def test_isr_cache_sqlite_handler_retained_body_voided_cross_worker(tmp_path):
    """A-1 (related): a cross-worker tag invalidation must void the
    other worker's retained previous body — before the fix
    _retained_body swallowed the missing get_tag_times and the stale
    body could resurrect."""
    path = str(tmp_path / "isr.sqlite3")

    async def render():
        return Response(b"<p>kept</p>")

    async def run():
        c1 = ISRCache(cache_handler="sqlite", cache_path=path)
        c2 = ISRCache(cache_handler="sqlite", cache_path=path)
        try:
            policy = {"revalidate": 3600.0, "stale": 3600.0}
            assert await c2.get_or_render("/page", policy, render, tags=("t1",)) == b"<p>kept</p>"
            assert await c2._retained_body("/page", ("t1",)) == b"<p>kept</p>"
            assert await c1.revalidate_tag("t1") is True
            assert await c2._retained_body("/page", ("t1",)) is None
        finally:
            await c1.close()
            await c2.close()

    _run(run())


def test_isr_cache_memory_handler_revalidate_tag_get_tag_times():
    """A-1 (related): the plain-handler adapter must expose
    get_tag_times — before the fix _retained_body swallowed the
    missing method and skipped the retention check entirely."""
    async def run():
        cache = ISRCache(cache_handler="memory")
        try:
            assert await cache.revalidate_tag("t1") is True
            stale_at, expired_at = cache.shared.get_tag_times(["t1"])["t1"]
            assert stale_at is None and expired_at is not None
            assert cache.shared.get_tag_times(["never"])["never"] == (None, None)
        finally:
            await cache.close()

    _run(run())


def test_isr_cache_with_custom_plain_handler_instance():
    handler = _DictHandler()
    calls = 0

    async def render():
        nonlocal calls
        calls += 1
        return Response(b"<p>custom</p>")

    async def run():
        cache = ISRCache(cache_handler=handler)
        try:
            assert cache.handler is handler
            assert cache.backend_name == "dict-test"
            policy = {"revalidate": 60.0, "stale": 60.0}
            assert await cache.get_or_render("/page", policy, render) == b"<p>custom</p>"
            assert await cache.get_or_render("/page", policy, render) == b"<p>custom</p>"
            assert calls == 1
            assert "/page" in handler.set_calls
            assert await cache.invalidate("/page")
            assert await cache.get_or_render("/page", policy, render) == b"<p>custom</p>"
            assert calls == 2
        finally:
            await cache.close()

    _run(run())


def test_isr_cache_with_registered_custom_factory():
    register_cache_handler("dict-test-factory",
                           lambda cache_path=None, cache_url=None: _DictHandler())
    calls = 0

    async def render():
        nonlocal calls
        calls += 1
        return Response(b"<p>factory</p>")

    async def run():
        cache = ISRCache(cache_handler="dict-test-factory")
        try:
            assert isinstance(cache.handler, _DictHandler)
            policy = {"revalidate": 60.0, "stale": 60.0}
            assert await cache.get_or_render("/page", policy, render) == b"<p>factory</p>"
            assert await cache.get_or_render("/page", policy, render) == b"<p>factory</p>"
            assert calls == 1
        finally:
            await cache.close()

    _run(run())


def test_isr_cache_with_named_memory_handler():
    calls = 0

    async def render():
        nonlocal calls
        calls += 1
        return Response(b"<p>mem</p>")

    async def run():
        cache = ISRCache(cache_handler="memory")
        try:
            policy = {"revalidate": 60.0, "stale": 60.0}
            assert await cache.get_or_render("/page", policy, render) == b"<p>mem</p>"
            assert await cache.get_or_render("/page", policy, render) == b"<p>mem</p>"
            assert calls == 1
        finally:
            await cache.close()

    _run(run())


# ---------------------------------------------------------------------------
# create_app forwarding
# ---------------------------------------------------------------------------

def _make_project(root):
    directory = root / "app" / "blog" / "[slug]"
    directory.mkdir(parents=True)
    (directory / "page.py").write_text(
        'from pynext import h, page\n'
        'calls = 0\n'
        '@page(render="isr", revalidate=30, stale=1)\n'
        'async def Page(params):\n'
        '    global calls\n'
        '    calls += 1\n'
        '    return h("p", {}, f"{params[\'slug\']}:{calls}")\n'
    )


def test_create_app_forwards_cache_handler(tmp_path):
    import httpx

    _make_project(tmp_path)
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True, cache_handler="sqlite",
                         cache_path=str(tmp_path / "isr.sqlite3"))
        try:
            assert isinstance(app.isr.handler, SQLiteCacheHandler)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                first = await client.get("/blog/alpha")
                second = await client.get("/blog/alpha")
                assert first.status_code == 200 and second.status_code == 200
                assert first.text == second.text
                module = app._module("app/blog/[slug]/page.py")
                assert module.calls == 1
        finally:
            await app.shutdown()

    _run(run())
