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
