from conftest import page_html
import asyncio
import os
import time
import uuid

import pytest

pytest.importorskip("redis")

import httpx

from pynext import RedisRateLimiter, Response, create_app
from pynext.build import write_manifest
from pynext.cache import ISRCache
from pynext.redis_cache import RedisCacheBackend
from pynext.server import start_native

URL = os.environ.get("PYNEXT_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(not URL, reason="PYNEXT_TEST_REDIS_URL is not configured")


@pytest.fixture
def backend():
    cache = RedisCacheBackend(URL)
    key = "/pynext-test-" + uuid.uuid4().hex
    try:
        yield cache, key
    finally:
        cache.remove(key)
        cache.close()


def test_redis_lease_fencing_expiry_and_invalidation(backend):
    cache, key = backend
    assert cache.lookup(key) == ("miss", None)
    assert cache.acquire_lease(key, "first", ttl=1) == 0
    assert cache.acquire_lease(key, "second", ttl=1) is None
    assert not cache.insert_fenced(key, b"wrong", 1, 1, 0, "second")
    assert cache.insert_fenced(key, b"one", 0.1, 0.5, 0, "first")
    assert cache.lookup(key) == ("fresh", b"one")
    assert cache.release_lease(key, "first")
    time.sleep(0.15)
    assert cache.lookup(key) == ("stale", b"one")
    assert cache.acquire_lease(key, "second", ttl=1) == 1
    assert cache.remove(key)
    assert not cache.insert_fenced(key, b"old", 1, 1, 1, "second")
    assert cache.lookup(key) == ("miss", None)


def test_redis_insert_fenced_accepts_generated_mono(backend):
    """Regression (PR #22 P1): ISRCache._shared_store passes both
    render-start stamps (generated_at, generated_mono) positionally;
    the Redis backend must accept generated_mono (it only uses the
    wall-clock stamp) or every Redis ISR save raises TypeError and
    nothing is ever cached."""
    cache, key = backend
    assert cache.acquire_lease(key, "owner", ttl=60) == 0
    assert cache.insert_fenced(key, b"body", 60, 60, 0, "owner",
                               ["post:42"], time.time(), time.monotonic())
    assert cache.lookup(key) == ("fresh", b"body")


def test_redis_tag_judgment_uses_render_start_stamp(backend):
    """The tag check judges an entry by when its content was produced:
    an entry generated before an invalidation but stored after it is
    expired by that invalidation."""
    cache, key = backend
    assert cache.acquire_lease(key, "owner", ttl=60) == 0
    assert cache.insert_fenced(key, b"body", 60, 60, 0, "owner",
                               ["post:42"], time.time() - 10, time.monotonic() - 10)
    assert cache.lookup(key) == ("fresh", b"body")
    assert cache.revalidate_tag("post:42", expire=0)
    assert cache.lookup(key) == ("expired", None)


def test_reopening_restores_redis_shared_backend(backend):
    observer, key = backend

    async def run():
        cache = ISRCache(cache_backend="redis", cache_url=URL)
        try:
            assert cache.shared.insert(key, b"cached", 60, 0)
            await cache.close()
            assert cache.shared is None
            cache.open()
            assert cache.shared is not None
            assert cache.shared.lookup(key) == ("fresh", b"cached")
            assert observer.lookup(key) == ("fresh", b"cached")
        finally:
            await cache.close()
    asyncio.run(run())


def test_redis_workers_share_and_invalidate_without_serving_stale(backend):
    cache, key = backend

    async def run():
        first = ISRCache(cache_backend="redis", cache_url=URL)
        second = ISRCache(cache_backend="redis", cache_url=URL)
        count = 0

        async def render():
            nonlocal count
            count += 1
            return Response(str(count))

        try:
            assert await first.get_or_render(key, {"revalidate": 30, "stale": 30}, render) == b"1"
            assert await second.get_or_render(key, {"revalidate": 30, "stale": 30}, render) == b"1"
            assert await second.invalidate(key)
            assert await first.get_or_render(key, {"revalidate": 30, "stale": 30}, render) == b"2"
        finally:
            await first.close()
            await second.close()
    asyncio.run(run())


def test_redis_unavailable_uses_local_fallback():
    async def run():
        cache = ISRCache(cache_backend="redis", cache_url="redis://127.0.0.1:1/0")
        calls = 0

        async def render():
            nonlocal calls
            calls += 1
            return Response("fallback")

        try:
            assert await cache.get_or_render("/page", {"revalidate": 30, "stale": 30}, render) == b"fallback"
            assert await cache.get_or_render("/page", {"revalidate": 30, "stale": 30}, render) == b"fallback"
            assert calls == 1
        finally:
            await cache.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_redis_cache_is_used_by_http_and_cross_worker_invalidation(tmp_path, mode):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "page.py").write_text(
        'from pynext import h, page\n'
        f'# unique build: {uuid.uuid4()}\n'
        'count = 0\n@page(render="isr", revalidate=60)\n'
        'def Page():\n    global count\n    count += 1\n    return h("p", {}, str(count))\n'
    )
    write_manifest(tmp_path)
    first = create_app(tmp_path, production=True, cache_backend="redis", cache_url=URL)
    second = create_app(tmp_path, production=True, cache_backend="redis", cache_url=URL)

    async def run():
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=first), base_url="http://test")
        else:
            server = start_native(first, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                assert (await client.get("/")).text == page_html("<p>1</p>")
                assert (await client.get("/")).text == page_html("<p>1</p>")
                assert await second.revalidate("/")
                assert (await client.get("/")).text == page_html("<p>2</p>")
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await first.revalidate("/")
            await first.isr.close()
            await second.isr.close()
    asyncio.run(run())


def test_shared_redis_rate_bucket_is_atomic():
    namespace = "test-" + uuid.uuid4().hex
    first = RedisRateLimiter(10, 60, url=URL, namespace=namespace)
    second = RedisRateLimiter(10, 60, url=URL, namespace=namespace)

    async def run():
        decisions = await asyncio.gather(*[asyncio.to_thread((first if i % 2 else second).allow, "127.0.0.1")
                                           for i in range(30)])
        assert sum(allowed for allowed, _ in decisions) == 10
        assert all(retry >= 1 for allowed, retry in decisions if not allowed)
    try:
        asyncio.run(run())
    finally:
        first.close()
        second.close()


def test_redis_rate_limiter_admits_new_keys_by_evicting_stalest_bucket():
    namespace = "test-" + uuid.uuid4().hex
    limiter = RedisRateLimiter(1, 60, url=URL, namespace=namespace,
                               max_buckets=2, max_new_keys_per_window=100)
    try:
        assert limiter.allow("a")[0]
        assert limiter.allow("b")[0]
        # Table full, but a new key is admitted by evicting the stalest
        # bucket instead of getting 429.
        assert limiter.allow("c")[0]
        # The evicted owner gets a fresh full bucket on its next request.
        assert limiter.allow("a")[0]
    finally:
        limiter.close()


def test_redis_rate_limiter_new_key_window_denies_only_new_keys():
    namespace = "test-" + uuid.uuid4().hex
    limiter = RedisRateLimiter(10, 2, url=URL, namespace=namespace, max_new_keys_per_window=2)
    try:
        assert limiter.allow("a")[0]
        assert limiter.allow("b")[0]
        allowed, retry = limiter.allow("c")
        assert allowed is False
        assert retry >= 1
        # An existing key is not blocked by the new-key window.
        assert limiter.allow("a")[0]
        # Once the window slides, the new key is admitted.
        time.sleep(2.2)
        assert limiter.allow("c")[0]
    finally:
        limiter.close()


def test_redis_rate_limiter_evicted_key_is_not_denied_by_new_key_window():
    # Review #4 #3 (Redis side): at most 2 buckets and 3 new keys per
    # 60s window. "c" is admitted by evicting "a"; when "a" returns
    # within the window it is an existing key, not a brand-new one, so
    # the full new-key window must not deny it with 429. A genuinely
    # brand-new key is still denied while the window is full.
    namespace = "test-" + uuid.uuid4().hex
    limiter = RedisRateLimiter(10, 60, url=URL, namespace=namespace,
                               max_buckets=2, max_new_keys_per_window=3)
    try:
        assert limiter.allow("a")[0]
        assert limiter.allow("b")[0]
        assert limiter.allow("c")[0]
        assert limiter.allow("a")[0]
        allowed, retry = limiter.allow("d")
        assert allowed is False
        assert retry >= 1
    finally:
        limiter.close()


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_shared_rate_limit_covers_static_and_fails_closed(tmp_path, mode):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("GET")\ndef get(): return JSONResponse({"ok": True})\n'
    )
    public = tmp_path / "public"
    public.mkdir()
    (public / "file.txt").write_text("asset")
    namespace = "test-" + uuid.uuid4().hex
    limiters = [RedisRateLimiter(2, 60, url=URL, namespace=namespace) for _ in range(2)]
    apps = [create_app(tmp_path, rate_limit=limiter) for limiter in limiters]

    async def run():
        servers = []
        if mode == "asgi":
            clients = [httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
                       for app in apps]
        else:
            servers = [start_native(app, asyncio.get_running_loop(), port=0) for app in apps]
            clients = [httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False) for server in servers]
        try:
            assert (await clients[0].get("/")).status_code == 200
            assert (await clients[1].get("/file.txt")).status_code == 200
            assert (await clients[0].get("/file.txt")).status_code == 429
        finally:
            for client in clients:
                await client.aclose()
            for server in servers:
                await asyncio.to_thread(server.stop)
            for app in apps:
                await app.isr.close()
    try:
        asyncio.run(run())
    finally:
        for limiter in limiters:
            limiter.close()

    unavailable = RedisRateLimiter(1, 60, url="redis://127.0.0.1:1/0", namespace=namespace)
    app = create_app(tmp_path, rate_limit=unavailable)

    async def failure():
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                assert (await client.get("/file.txt")).status_code == 503
        finally:
            if server:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    try:
        asyncio.run(failure())
    finally:
        unavailable.close()
