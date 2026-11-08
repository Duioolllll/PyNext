from conftest import page_html
import asyncio

import httpx
import pytest

from pynext import Request, Response, create_app
from pynext.build import write_manifest
from pynext.cache import CacheBusy, ISRCache
from pynext.server import start_native


POLICY = {"revalidate": 30.0, "stale": 60.0}


def test_invalidated_generation_cannot_replace_newer_cache_entry():
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
        assert await cache.invalidate("/item") is True
        assert await cache.get_or_render("/item", POLICY, new_render) == b"new"
        release.set()
        assert await old == b"old"
        assert cache.store.lookup("/item") == ("fresh", b"new")
        assert await cache.invalidate("/item") is True
        assert await cache.invalidate("/item") is False
        await cache.close()
    asyncio.run(run())


def test_retired_generation_still_counts_toward_inflight_limit(monkeypatch):
    monkeypatch.setattr("pynext.cache.MAX_INFLIGHT", 1)

    async def run():
        cache = ISRCache()
        started, release = asyncio.Event(), asyncio.Event()

        async def render():
            started.set()
            await release.wait()
            return Response("old")

        old = asyncio.create_task(cache.get_or_render("/item", POLICY, render))
        await asyncio.wait_for(started.wait(), 3)
        assert await cache.invalidate("/item")
        with pytest.raises(CacheBusy):
            await cache.get_or_render("/item", POLICY, render)
        release.set()
        assert await old == b"old"
        await asyncio.sleep(0)
        assert cache.store.lookup("/item")[0] == "miss"
        assert await cache.get_or_render("/item", POLICY, render) == b"old"
        await cache.close()
    asyncio.run(run())


def test_close_cancels_invalidated_generation():
    async def run():
        cache = ISRCache()
        started, finished = asyncio.Event(), asyncio.Event()

        async def render():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                finished.set()

        old = asyncio.create_task(cache.get_or_render("/item", POLICY, render))
        await asyncio.wait_for(started.wait(), 3)
        assert await cache.invalidate("/item")
        await cache.close()
        with pytest.raises(asyncio.CancelledError):
            await old
        assert finished.is_set()
    asyncio.run(run())


def make_project(root):
    page = root / "app" / "news" / "[slug]"
    api = root / "app" / "api" / "purge"
    page.mkdir(parents=True)
    api.mkdir(parents=True)
    (page / "page.py").write_text(
        'from pynext import h, page\n'
        'calls = 0\n'
        '@page(render="isr", revalidate=30)\n'
        'async def Page(params):\n'
        '    global calls\n'
        '    calls += 1\n'
        '    return h("p", {}, str(calls))\n', encoding="utf-8"
    )
    (api / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("POST")\n'
        'async def purge(request): return JSONResponse({"removed": await request.revalidate("/news/a")})\n',
        encoding="utf-8"
    )
    public = root / "public" / "news"
    public.mkdir(parents=True)
    (public / "b").write_text("public", encoding="utf-8")
    write_manifest(root)
    return create_app(root, production=True)


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_request_revalidation_invalidates_the_shared_local_native_store(tmp_path, mode):
    app = make_project(tmp_path)

    async def run():
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10)
        try:
            async with client:
                assert (await client.get("/news/a")).text == page_html("<p>1</p>")
                assert (await client.get("/news/a")).text == page_html("<p>1</p>")
                assert (await client.post("/api/purge")).json() == {"removed": True}
                assert (await client.post("/api/purge")).json() == {"removed": False}
                assert (await client.get("/news/a")).text == page_html("<p>2</p>")
                with pytest.raises(ValueError, match="does not match an ISR page"):
                    await app.revalidate("/missing")
                with pytest.raises(ValueError, match="does not match an ISR page"):
                    await app.revalidate("/news/b")
                with pytest.raises(ValueError, match="trailing slash"):
                    await app.revalidate("/news/a/")
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_unattached_request_cannot_revalidate():
    async def run():
        with pytest.raises(RuntimeError, match="not attached"):
            await Request("GET", "/").revalidate("/news/a")
    asyncio.run(run())
