"""Scoped ISR revalidation: ``revalidate(path, type=...)`` (REPORT §2.2 P1-7).

Next.js ``revalidatePath(path, type?)``: ``type="page"`` invalidates every
cached entry for a dynamic route pattern, ``type="layout"`` invalidates
every page using the layout at ``path``, and a dynamic pattern without a
``type`` only warns. ``"/"`` and ``"/index"`` are aliases.
"""
import asyncio

import httpx
import pytest

from pynext import Response, create_app
from pynext.build import write_manifest
from pynext.cache import ISRCache
from pynext.redis_cache import RedisCacheBackend


def _counter_page():
    return (
        'from pynext import h, page\n'
        'calls = 0\n'
        '@page(render="isr", revalidate=30)\n'
        'async def Page(params):\n'
        '    global calls\n'
        '    calls += 1\n'
        '    return h("p", {}, str(calls))\n'
    )


def _query_page():
    return (
        'from pynext import h, page\n'
        'calls = 0\n'
        '@page(render="isr", revalidate=30, query_keys=("q",))\n'
        'async def Page(params):\n'
        '    global calls\n'
        '    calls += 1\n'
        '    return h("p", {}, str(calls))\n'
    )


def _layout(marker):
    return (
        'from pynext import h, layout\n'
        '@layout\n'
        f'def Layout(children): return h("section", {{"data-layout": "{marker}"}}, children)\n'
    )


def build_project(root):
    (root / "app").mkdir(parents=True)
    (root / "app" / "layout.py").write_text(_layout("root"), encoding="utf-8")
    (root / "app" / "page.py").write_text(_counter_page(), encoding="utf-8")
    news = root / "app" / "news"
    (news / "a").mkdir(parents=True)
    (news / "[slug]").mkdir(parents=True)
    (news / "layout.py").write_text(_layout("news"), encoding="utf-8")
    (news / "page.py").write_text(_counter_page(), encoding="utf-8")
    (news / "a" / "page.py").write_text(_counter_page(), encoding="utf-8")
    (news / "[slug]" / "page.py").write_text(_counter_page(), encoding="utf-8")
    blog = root / "app" / "blog"
    blog.mkdir(parents=True)
    (blog / "page.py").write_text(_counter_page(), encoding="utf-8")
    search = root / "app" / "search" / "[term]"
    search.mkdir(parents=True)
    (search / "page.py").write_text(_query_page(), encoding="utf-8")
    api = root / "app" / "api" / "purge"
    api.mkdir(parents=True)
    (api / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("POST")\n'
        'async def purge(request): return JSONResponse('
        '{"removed": await request.revalidate("/news/[slug]", "page")})\n',
        encoding="utf-8",
    )
    write_manifest(root)


def make_project(root):
    build_project(root)
    return create_app(root, production=True)


async def _text(client, path):
    """Response body for ``path`` (layouts wrap the page, so callers match
    on the ``<p>N</p>`` counter fragment, not the whole document)."""
    return (await client.get(path)).text


def _shows(text, count):
    return f"<p>{count}</p>" in text


def test_revalidate_index_alias_invalidates_home(tmp_path):
    app = make_project(tmp_path)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                assert _shows(await _text(client, "/"), 1)
                assert _shows(await _text(client, "/"), 1)
                assert await app.revalidate("/index") is True
                assert _shows(await _text(client, "/"), 2)
                assert await app.revalidate("/index") is True
                assert _shows(await _text(client, "/"), 3)
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_dynamic_pattern_without_type_warns_and_keeps_cache(tmp_path):
    app = make_project(tmp_path)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                # The [slug] module's counter is shared across slugs.
                assert _shows(await _text(client, "/news/b"), 1)
                assert _shows(await _text(client, "/news/c"), 2)
                with pytest.warns(UserWarning, match="dynamic route pattern"):
                    assert await app.revalidate("/news/[slug]") is False
                assert _shows(await _text(client, "/news/b"), 1)
                assert _shows(await _text(client, "/news/c"), 2)
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_revalidate_pattern_page_invalidates_all_matching_entries(tmp_path):
    app = make_project(tmp_path)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                # /news/a is a static page, not part of the [slug] pattern.
                assert _shows(await _text(client, "/news/a"), 1)
                assert _shows(await _text(client, "/news/b"), 1)
                assert _shows(await _text(client, "/news/c"), 2)
                assert _shows(await _text(client, "/blog"), 1)
                assert await app.revalidate("/news/[slug]", type="page") is True
                # Nothing cached any more: a second call finds nothing.
                assert await app.revalidate("/news/[slug]", type="page") is False
                assert _shows(await _text(client, "/news/a"), 1)
                assert _shows(await _text(client, "/news/b"), 3)
                assert _shows(await _text(client, "/news/c"), 4)
                assert _shows(await _text(client, "/blog"), 1)
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_revalidate_pattern_page_rejects_non_isr_and_bad_type(tmp_path):
    app = make_project(tmp_path)

    async def run():
        try:
            with pytest.raises(ValueError, match="does not match an ISR page"):
                await app.revalidate("/api/purge", type="page")
            with pytest.raises(ValueError, match="does not match an ISR page"):
                await app.revalidate("/nope/[slug]", type="page")
            with pytest.raises(ValueError, match="revalidate type must be"):
                await app.revalidate("/news/a", type="section")
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_revalidate_layout_invalidates_only_that_layouts_pages(tmp_path):
    app = make_project(tmp_path)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                home = await _text(client, "/")
                assert _shows(home, 1) and 'data-layout="root"' in home
                news = await _text(client, "/news/a")
                assert _shows(news, 1) and 'data-layout="news"' in news
                assert _shows(await _text(client, "/news"), 1)
                assert _shows(await _text(client, "/news/b"), 1)
                assert _shows(await _text(client, "/blog"), 1)
                assert await app.revalidate("/news", type="layout") is True
                # Nothing cached any more: a second call finds nothing.
                assert await app.revalidate("/news", type="layout") is False
                assert _shows(await _text(client, "/news"), 2)
                assert _shows(await _text(client, "/news/a"), 2)
                assert _shows(await _text(client, "/news/b"), 2)
                assert _shows(await _text(client, "/"), 1)
                assert _shows(await _text(client, "/blog"), 1)
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_revalidate_root_layout_invalidates_everything(tmp_path):
    app = make_project(tmp_path)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                assert _shows(await _text(client, "/news/a"), 1)
                assert _shows(await _text(client, "/blog"), 1)
                assert await app.revalidate("/", type="layout") is True
                assert _shows(await _text(client, "/news/a"), 2)
                assert _shows(await _text(client, "/blog"), 2)
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_revalidate_layout_without_a_layout_raises(tmp_path):
    app = make_project(tmp_path)

    async def run():
        try:
            with pytest.raises(ValueError, match="No layout at /blog"):
                await app.revalidate("/blog", type="layout")
            with pytest.raises(ValueError, match="No layout at /missing"):
                await app.revalidate("/missing", type="layout")
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_revalidate_pattern_page_rotates_query_variant_generations(tmp_path):
    app = make_project(tmp_path)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                assert _shows(await _text(client, "/search/dogs?q=1"), 1)
                assert _shows(await _text(client, "/search/dogs?q=1"), 1)
                assert _shows(await _text(client, "/search/cats?q=1"), 2)
                assert await app.revalidate("/search/[term]", type="page") is True
                assert _shows(await _text(client, "/search/dogs?q=1"), 3)
                assert _shows(await _text(client, "/search/cats?q=1"), 4)
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_request_revalidate_forwards_type(tmp_path):
    app = make_project(tmp_path)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                assert _shows(await _text(client, "/news/b"), 1)
                assert (await client.post("/api/purge")).json() == {"removed": True}
                assert (await client.post("/api/purge")).json() == {"removed": False}
                assert _shows(await _text(client, "/news/b"), 2)
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_scoped_revalidation_reaches_another_sqlite_worker(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    build_project(root)
    path = tmp_path / "shared.sqlite3"
    first = create_app(root, production=True, cache_backend="sqlite", cache_path=path)
    second = create_app(root, production=True, cache_backend="sqlite", cache_path=path)

    async def run():
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=first),
                                         base_url="http://test") as client:
                assert _shows(await _text(client, "/news/b"), 1)
                assert _shows(await _text(client, "/news/c"), 2)
            # The second worker never rendered these pages: it finds them
            # through the shared backend's key list, and the first worker
            # observes the invalidation on its next lookup.
            assert await second.revalidate("/news/[slug]", type="page") is True
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=first),
                                         base_url="http://test") as client:
                assert _shows(await _text(client, "/news/b"), 3)
                assert _shows(await _text(client, "/news/c"), 4)
        finally:
            await first.isr.close()
            await second.isr.close()
    asyncio.run(run())


def test_cached_keys_lists_memory_and_sqlite_entries(tmp_path):
    async def run():
        async def render_a():
            return Response("a")

        async def render_b():
            return Response("b")

        memory = ISRCache()
        try:
            assert await memory.get_or_render("/a", {"revalidate": 30.0, "stale": 60.0},
                                              render_a) == b"a"
            assert await memory.cached_keys() == ["/a"]
        finally:
            await memory.close()
        path = tmp_path / "shared.sqlite3"
        first = ISRCache(cache_backend="sqlite", cache_path=path, namespace="ns:")
        second = ISRCache(cache_backend="sqlite", cache_path=path, namespace="ns:")
        try:
            assert await first.get_or_render("/b", {"revalidate": 30.0, "stale": 60.0},
                                             render_b) == b"b"
            # The second instance never rendered /b itself: it finds the
            # entry through the shared backend's key list.
            assert await second.cached_keys() == ["/b"]
            assert await first.cached_keys() == ["/b"]
        finally:
            await first.close()
            await second.close()
    asyncio.run(run())


class _FakePipeline:
    def __init__(self, client):
        self._client = client
        self._calls = []

    def hget(self, key, field):
        self._calls.append((key, field))
        return self

    def execute(self):
        return [self._client._hashes.get(key, {}).get(
            field.encode("utf-8") if isinstance(field, str) else field)
            for key, field in self._calls]


class _FakeRedis:
    """Minimal stand-in for a redis client: only what keys() touches."""

    def __init__(self):
        self._hashes = {}
        self._values = {}
        self.set_calls = []

    def scan_iter(self, match):
        import fnmatch
        keys = list(self._hashes) + list(self._values)
        return [key for key in keys
                if fnmatch.fnmatchcase(key.decode("utf-8"), match)]

    def pipeline(self):
        return _FakePipeline(self)

    def get(self, key):
        return self._values.get(key)

    def set(self, key, value, ex=None):
        self.set_calls.append((key, value, ex))
        self._values[key] = value.encode("utf-8")

    def eval(self, script, numkeys, *args):
        # _GENERATION: first call creates the token, later calls keep it.
        generation_key = args[0]
        if generation_key not in self._values:
            self._values[generation_key] = args[1].encode("utf-8")
            return (args[1].encode("utf-8"), 0)
        return (self._values[generation_key], 1)


def test_redis_keys_lists_entries_and_generation_paths():
    backend = RedisCacheBackend.__new__(RedisCacheBackend)
    backend.client = _FakeRedis()
    backend.client._hashes = {
        b"pynext:isr:{aaa}:entry": {b"path": b"ns:/news/a"},
        b"pynext:isr:{bbb}:entry": {b"path": b"ns:\x00isr:deadbeef"},
        b"pynext:isr:{ccc}:entry": {},
    }
    backend.client._values = {
        b"pynext:isr-generation-path:{ddd}": b"ns:/search/dogs",
    }
    keys = backend.keys()
    assert sorted(keys) == ["ns:\x00isr:deadbeef", "ns:/news/a", "ns:/search/dogs"]


def test_redis_variant_generation_records_the_concrete_path():
    backend = RedisCacheBackend.__new__(RedisCacheBackend)
    backend.client = _FakeRedis()
    token, existed = backend.variant_generation("ns:/search/dogs", invalidate=True)
    assert token and existed is False
    assert backend.client.set_calls, "expected a generation-path companion write"
    key, value, ex = backend.client.set_calls[0]
    assert key.startswith("pynext:isr-generation-path:{")
    assert value == "ns:/search/dogs"
    assert ex == 86400
