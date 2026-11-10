"""On-demand revalidation: tags, revalidate=False, layout scope, webhook."""
from conftest import page_html
import asyncio
import json
import os
import sqlite3
import time

import httpx
import pytest

from pynext import Response, create_app
from pynext.build import write_manifest
from pynext.cache import (
    ISRCache, SQLiteCacheBackend, policy_windows, validate_cache_tag, validate_cache_tags,
)
from pynext.components import _isr_policy, _validate_tags, page
from pynext.server import start_native

POLICY = {"revalidate": 30.0, "stale": 60.0, "tags": ["post:1"]}


async def _render(body):
    async def render():
        return Response(body)
    return render


async def until(condition, timeout=5):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


# --- policy / decorator validation ---------------------------------------


def test_isr_policy_tags_sorted_deduped_and_revalidate_false():
    # The 3-key policy carries no tags: tags are validated/sorted by the
    # tag validators, and _isr_policy only takes revalidate/stale/expire.
    assert _isr_policy(60, 60) == {"revalidate": 60.0, "stale": 60.0, "expire": 120.0}
    assert _isr_policy(False, 60) == {
        "revalidate": 31536000.0, "stale": 60.0, "expire": 31536000.0}
    assert validate_cache_tags(("b", "a", "a")) == ["a", "b"]
    for bad in (True, None, 0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="revalidate"):
            _isr_policy(bad, 60)


def test_isr_policy_rejects_bad_tags():
    for bad in ("", "has space", "x" * 257, 42, None):
        with pytest.raises(ValueError, match="tag"):
            validate_cache_tag(bad)
    with pytest.raises(ValueError, match="tags"):
        _validate_tags("post:1")
    with pytest.raises(ValueError, match="At most"):
        validate_cache_tags([f"t{i}" for i in range(33)])
    with pytest.raises(ValueError, match="tag"):
        validate_cache_tags(("ok", "bad tag"))


def test_page_rejects_tags_for_non_isr():
    with pytest.raises(ValueError, match="tags are only valid for ISR"):
        page(render="ssr", tags=("post:1",))
    with pytest.raises(ValueError, match="tags are only valid for ISR"):
        page(render="ssg", tags=("post:1",))


def test_page_records_tags_and_revalidate_false():
    @page(render="isr", revalidate=False, tags=("post:1", "post:2"))
    def Page():
        return None

    # page() stores the partial policy; tags live in their own attribute
    # and the final policy is resolved at app load.
    assert Page.__pynext_cache_policy__ == {
        "revalidate": False, "stale": None, "expire": None}
    assert Page.__pynext_cache_tags__ == ("post:1", "post:2")


def test_revalidate_false_stores_with_max_fresh():
    # The policy windows for revalidate=False are the longest lifetime
    # with a zero stale window; the process-local L1 caps each window at
    # a week, so the store sees the clamped fresh value.
    assert policy_windows({"revalidate": False, "stale": 60.0}) == (31536000.0, 0.0)

    class FakeStore:
        def __init__(self):
            self.insert_calls = []

        def lookup(self, key):
            return ("miss", None)

        def insert(self, key, body, revalidate, stale):
            self.insert_calls.append((key, body, revalidate, stale))
            return True

    async def run():
        cache = ISRCache()
        cache.store = FakeStore()
        try:
            body = await cache.get_or_render(
                "/page", {"revalidate": False, "stale": 60.0, "tags": []}, await _render(b"x"))
            assert body == b"x"
            assert cache.store.insert_calls == [("/page", b"x", 604800.0, 0.0)]
        finally:
            await cache.close()

    asyncio.run(run())


# --- ISRCache tag invalidation (memory backend) ---------------------------


def test_tag_invalidation_only_hits_matching_entries():
    async def run():
        cache = ISRCache()
        calls = {"a": 0, "b": 0}

        async def render_a():
            calls["a"] += 1
            return Response(f"a{calls['a']}".encode())

        async def render_b():
            calls["b"] += 1
            return Response(f"b{calls['b']}".encode())

        try:
            assert await cache.get_or_render(
                "/a", {"revalidate": 30.0, "stale": 60.0, "tags": ["post:1"]}, render_a) == b"a1"
            assert await cache.get_or_render(
                "/b", {"revalidate": 30.0, "stale": 60.0, "tags": ["post:2"]}, render_b) == b"b1"
            assert await cache.get_or_render(
                "/a", {"revalidate": 30.0, "stale": 60.0, "tags": ["post:1"]}, render_a) == b"a1"
            assert await cache.invalidate_tag("missing") == 0
            assert await cache.invalidate_tag("post:1") == 1
            assert await cache.get_or_render(
                "/a", {"revalidate": 30.0, "stale": 60.0, "tags": ["post:1"]}, render_a) == b"a2"
            assert await cache.get_or_render(
                "/b", {"revalidate": 30.0, "stale": 60.0, "tags": ["post:2"]}, render_b) == b"b1"
            assert calls == {"a": 2, "b": 1}
        finally:
            await cache.close()

    asyncio.run(run())


def test_tag_invalidation_does_not_apply_to_newer_entries():
    async def run():
        cache = ISRCache()
        calls = 0

        async def render():
            nonlocal calls
            calls += 1
            return Response(f"v{calls}".encode())

        try:
            policy = {"revalidate": 30.0, "stale": 60.0, "tags": ["t"]}
            assert await cache.get_or_render("/p", policy, render) == b"v1"
            assert await cache.invalidate_tag("t") == 1
            assert await cache.get_or_render("/p", policy, render) == b"v2"
            # The v2 entry was generated after the invalidation, so the
            # recorded timestamp must not demote it.
            assert await cache.get_or_render("/p", policy, render) == b"v2"
            assert calls == 2
        finally:
            await cache.close()

    asyncio.run(run())


def test_tag_invalidation_with_expire_serves_stale_then_expires():
    async def run():
        cache = ISRCache()
        calls = 0

        async def render():
            nonlocal calls
            calls += 1
            return Response(f"v{calls}".encode())

        try:
            policy = {"revalidate": 30.0, "stale": 60.0, "tags": ["t"]}
            assert await cache.get_or_render("/p", policy, render) == b"v1"
            # Deferred expiry evicts nothing: the old body is served while
            # exactly one background regeneration runs.
            assert await cache.invalidate_tag("t", expire=60) == 0
            assert await cache.get_or_render("/p", policy, render) == b"v1"
            await until(lambda: calls == 2)
            async with asyncio.timeout(5):
                while await cache.get_or_render("/p", policy, render) != b"v2":
                    await asyncio.sleep(0.01)
            assert calls == 2
            # Once the deferred expiry elapses, the entry is a miss and
            # the next lookup regenerates synchronously.
            assert await cache.invalidate_tag("t", expire=0.05) == 0
            await asyncio.sleep(0.1)
            assert await cache.get_or_render("/p", policy, render) == b"v3"
            assert calls == 3
        finally:
            await cache.close()

    asyncio.run(run())


def test_tag_invalidation_rejects_bad_expire():
    async def run():
        cache = ISRCache()
        try:
            for bad in (-1, float("inf"), float("nan"), True, "60"):
                with pytest.raises(ValueError, match="expire"):
                    await cache.invalidate_tag("t", expire=bad)
            with pytest.raises(ValueError, match="tag"):
                await cache.invalidate_tag("bad tag")
        finally:
            await cache.close()

    asyncio.run(run())


def test_inflight_generation_skipped_after_tag_invalidation():
    async def run():
        cache = ISRCache()
        started, release = asyncio.Event(), asyncio.Event()

        async def old_render():
            started.set()
            await release.wait()
            return Response("old")

        async def new_render():
            return Response("new")

        old = asyncio.create_task(cache.get_or_render("/item", POLICY, old_render))
        await asyncio.wait_for(started.wait(), 3)
        assert await cache.invalidate_tag("post:1") == 1
        assert await cache.get_or_render("/item", POLICY, new_render) == b"new"
        release.set()
        assert await old == b"old"
        assert cache.store.lookup("/item") == ("fresh", b"new")
        await cache.close()

    asyncio.run(run())


# --- shared backends ------------------------------------------------------


def test_sqlite_tag_invalidation_across_instances(tmp_path):
    path = tmp_path / "cache.sqlite3"

    async def run():
        first = ISRCache(cache_backend="sqlite", cache_path=path, namespace="test:")
        second = ISRCache(cache_backend="sqlite", cache_path=path, namespace="test:")
        calls = {"a": 0, "b": 0}

        async def render_a():
            calls["a"] += 1
            return Response(f"a{calls['a']}".encode())

        async def render_b():
            calls["b"] += 1
            return Response(f"b{calls['b']}".encode())

        try:
            policy_a = {"revalidate": 30.0, "stale": 60.0, "tags": ["post:1"]}
            policy_b = {"revalidate": 30.0, "stale": 60.0, "tags": ["post:2"]}
            assert await first.get_or_render("/a", policy_a, render_a) == b"a1"
            assert await first.get_or_render("/b", policy_b, render_b) == b"b1"
            # The second worker never saw the entries locally, so it
            # evicts nothing itself — but the shared manifest must still
            # demote the first worker's entries on next lookup.
            assert await second.invalidate_tag("post:1") == 0
            assert await first.get_or_render("/a", policy_a, render_a) == b"a2"
            assert await first.get_or_render("/b", policy_b, render_b) == b"b1"
            assert calls == {"a": 2, "b": 1}
        finally:
            await first.close()
            await second.close()

    asyncio.run(run())


def test_sqlite_backend_migrates_pre_tag_database(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE entries (key TEXT PRIMARY KEY, body BLOB NOT NULL,"
        " generated_at REAL NOT NULL, fresh_for REAL NOT NULL,"
        " stale_for REAL NOT NULL, version INTEGER NOT NULL)")
    conn.execute(
        "INSERT INTO entries VALUES ('/old', ?, ?, 60, 60, 1)",
        (b"old-body", time.time()))
    conn.commit()
    conn.close()
    backend = SQLiteCacheBackend(path)
    try:
        assert backend.lookup("/old") == ("fresh", b"old-body")
        assert backend.insert("/new", b"new-body", 60, 60, tags=("t",))
        assert backend.lookup("/new") == ("fresh", b"new-body")
        backend.update_tag("ns:t", None, time.time())
        assert backend.get_tag_times(["ns:t", "missing"])["missing"] == (None, None)
    finally:
        backend.close()


def test_redis_tag_invalidation_across_instances():
    url = os.environ.get("PYNEXT_TEST_REDIS_URL")
    if not url:
        pytest.skip("PYNEXT_TEST_REDIS_URL is not configured")

    async def run():
        first = ISRCache(cache_backend="redis", cache_url=url, namespace="test-reval:")
        second = ISRCache(cache_backend="redis", cache_url=url, namespace="test-reval:")
        calls = 0

        async def render():
            nonlocal calls
            calls += 1
            return Response(f"v{calls}".encode())

        try:
            policy = {"revalidate": 30.0, "stale": 60.0, "tags": ["post:9"]}
            assert await first.get_or_render("/r", policy, render) == b"v1"
            assert await second.invalidate_tag("post:9") == 0
            assert await first.get_or_render("/r", policy, render) == b"v2"
            assert calls == 2
        finally:
            await first.close()
            await second.close()

    asyncio.run(run())


# --- App level ------------------------------------------------------------


def make_project(root):
    news = root / "app" / "news"
    blog = root / "app" / "blog"
    api = root / "app" / "api" / "purge"
    for directory in (news, blog, api):
        directory.mkdir(parents=True)
    (root / "app" / "page.py").write_text(
        'from pynext import h, page\n'
        'calls = 0\n'
        '@page(render="isr", revalidate=30)\n'
        'def Page():\n'
        '    global calls\n'
        '    calls += 1\n'
        '    return h("p", {}, f"home:{calls}")\n', encoding="utf-8")
    (root / "app" / "index" / "page.py").parent.mkdir(parents=True, exist_ok=True)
    (root / "app" / "index" / "page.py").write_text(
        'from pynext import h, page\n'
        'calls = 0\n'
        '@page(render="isr", revalidate=30)\n'
        'def Page():\n'
        '    global calls\n'
        '    calls += 1\n'
        '    return h("p", {}, f"index:{calls}")\n', encoding="utf-8")
    (news / "a" / "page.py").parent.mkdir(parents=True, exist_ok=True)
    (news / "a" / "page.py").write_text(
        'from pynext import h, page\n'
        'calls = 0\n'
        '@page(render="isr", revalidate=30, tags=("post:1",))\n'
        'def Page():\n'
        '    global calls\n'
        '    calls += 1\n'
        '    return h("p", {}, f"a:{calls}")\n', encoding="utf-8")
    (news / "b" / "page.py").parent.mkdir(parents=True, exist_ok=True)
    (news / "b" / "page.py").write_text(
        'from pynext import h, page\n'
        'calls = 0\n'
        '@page(render="isr", revalidate=30, tags=("post:2",))\n'
        'def Page():\n'
        '    global calls\n'
        '    calls += 1\n'
        '    return h("p", {}, f"b:{calls}")\n', encoding="utf-8")
    (blog / "layout.py").write_text(
        'from pynext import layout\n'
        '@layout\n'
        'def Layout(children):\n'
        '    return children\n', encoding="utf-8")
    for slug in ("x", "y"):
        (blog / slug / "page.py").parent.mkdir(parents=True, exist_ok=True)
        (blog / slug / "page.py").write_text(
            'from pynext import h, page\n'
            'calls = 0\n'
            '@page(render="isr", revalidate=30)\n'
            'def Page():\n'
            '    global calls\n'
            '    calls += 1\n'
            f'    return h("p", {{}}, f"{slug}:{{calls}}")\n', encoding="utf-8")
    (api / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("POST")\n'
        'async def purge(request):\n'
        '    return JSONResponse({"evicted": await request.revalidate_tag("post:1")})\n',
        encoding="utf-8")
    write_manifest(root)
    return create_app(root, production=True, revalidate_secret="s3cret")


def test_app_revalidate_tag_path_layout_and_alias(tmp_path):
    app = make_project(tmp_path)

    async def run():
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.get("/news/a")).text == page_html("<p>a:1</p>")
                assert (await client.get("/news/b")).text == page_html("<p>b:1</p>")
                assert (await client.get("/blog/x")).text == page_html("<p>x:1</p>")
                assert (await client.get("/blog/y")).text == page_html("<p>y:1</p>")
                assert (await client.get("/")).text == page_html("<p>home:1</p>")
                assert (await client.get("/index")).text == page_html("<p>index:1</p>")
                # Only the tagged page regenerates.
                assert await app.revalidate_tag("post:1") is True
                assert (await client.get("/news/a")).text == page_html("<p>a:2</p>")
                assert (await client.get("/news/b")).text == page_html("<p>b:1</p>")
                # Request-level tag invalidation through an API route.
                assert (await client.post("/api/purge")).json() == {"evicted": True}
                assert (await client.get("/news/a")).text == page_html("<p>a:3</p>")
                # Layout scope invalidates every page under that layout.
                assert await app.revalidate("/blog", type="layout") is True
                assert (await client.get("/blog/x")).text == page_html("<p>x:2</p>")
                assert (await client.get("/blog/y")).text == page_html("<p>y:2</p>")
                assert (await client.get("/news/a")).text == page_html("<p>a:3</p>")
                with pytest.raises(ValueError, match="No layout at /news"):
                    await app.revalidate("/news", type="layout")
                with pytest.raises(ValueError, match="revalidate type must be 'page' or 'layout'"):
                    await app.revalidate("/news/a", type="all")
                # "/" and "/index" are aliases: invalidating one hits both.
                assert await app.revalidate("/") is True
                assert (await client.get("/")).text == page_html("<p>home:2</p>")
                assert (await client.get("/index")).text == page_html("<p>index:2</p>")
                with pytest.raises(ValueError, match="does not match an ISR page"):
                    await app.revalidate("/missing")
                with pytest.raises(ValueError, match="tag"):
                    await app.revalidate_tag("bad tag")
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_revalidate_false_page_serves_until_invalidated(tmp_path):
    directory = tmp_path / "app"
    directory.mkdir(parents=True)
    (directory / "page.py").write_text(
        'from pynext import h, page\n'
        'calls = 0\n'
        '@page(render="isr", revalidate=False)\n'
        'def Page():\n'
        '    global calls\n'
        '    calls += 1\n'
        '    return h("p", {}, f"v{calls}")\n', encoding="utf-8")
    write_manifest(tmp_path)
    app = create_app(tmp_path, production=True)

    async def run():
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.get("/")).text == page_html("<p>v1</p>")
                assert (await client.get("/")).text == page_html("<p>v1</p>")
                assert await app.revalidate("/") is True
                assert (await client.get("/")).text == page_html("<p>v2</p>")
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_webhook_requires_secret_and_validates_before_applying(tmp_path):
    app = make_project(tmp_path)

    async def run():
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                assert (await client.get("/news/a")).text == page_html("<p>a:1</p>")
                assert (await client.get("/news/b")).text == page_html("<p>b:1</p>")
                # Wrong or missing secret: nothing is invalidated.
                assert (await client.post("/_pynext/revalidate",
                                          json={"tags": ["post:1"]})).status_code == 401
                assert (await client.post("/_pynext/revalidate", json={"tags": ["post:1"]},
                                          headers={"authorization": "Bearer nope"})).status_code == 401
                assert (await client.get("/news/a")).text == page_html("<p>a:1</p>")
                assert (await client.get("/_pynext/revalidate")).status_code == 405
                assert (await client.post("/_pynext/revalidate", content=b"{bad",
                                          headers={"authorization": "Bearer s3cret"})).status_code == 400
                # Unknown path: validated before applying, so /news/b stays.
                response = await client.post(
                    "/_pynext/revalidate", json={"paths": ["/news/b", "/missing"]},
                    headers={"authorization": "Bearer s3cret"})
                assert response.status_code == 400
                assert (await client.get("/news/b")).text == page_html("<p>b:1</p>")
                # Tag invalidation through the webhook.
                response = await client.post(
                    "/_pynext/revalidate", json={"tags": ["post:1", "post:2"]},
                    headers={"authorization": "Bearer s3cret"})
                assert response.status_code == 200
                assert response.json() == {
                    "paths": {}, "tags": {"post:1": 1, "post:2": 1}, "evicted_entries": 2}
                assert (await client.get("/news/a")).text == page_html("<p>a:2</p>")
                assert (await client.get("/news/b")).text == page_html("<p>b:2</p>")
                # Path invalidation through the webhook.
                response = await client.post(
                    "/_pynext/revalidate", json={"paths": ["/news/a"]},
                    headers={"authorization": "Bearer s3cret"})
                assert response.status_code == 200
                assert response.json()["paths"] == {"/news/a": True}
                assert (await client.get("/news/a")).text == page_html("<p>a:3</p>")
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_webhook_disabled_without_secret(tmp_path):
    app = make_project(tmp_path)
    app.revalidate_secret = None

    async def run():
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post(
                    "/_pynext/revalidate", json={"tags": ["post:1"]},
                    headers={"authorization": "Bearer s3cret"})
                assert response.status_code == 404
                assert (await client.get("/news/a")).text == page_html("<p>a:1</p>")
        finally:
            await app.isr.close()

    asyncio.run(run())


def test_tag_invalidation_reaches_native_fresh_hits(tmp_path):
    app = make_project(tmp_path)

    async def run():
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
                assert (await client.get("/news/a")).text == page_html("<p>a:1</p>")
                # A fresh native hit bypasses Python; tag invalidation
                # must still evict the local store the native server reads.
                assert await app.revalidate_tag("post:1") is True
                assert (await client.get("/news/a")).text == page_html("<p>a:2</p>")
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    asyncio.run(run())
