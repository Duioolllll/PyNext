import asyncio
import os
import uuid

import httpx
import pytest

from pynext import create_app, page
from pynext.build import write_manifest
from pynext.server import start_native


def make_project(root):
    directory = root / "app" / "catalog" / "[slug]"
    directory.mkdir(parents=True)
    (directory / "page.py").write_text(
        'from pynext import h, page\n'
        'calls = 0\nstarted = None\ngate = None\n'
        '@page(render="isr", revalidate=30, query_keys=("lang", "tag"))\n'
        'async def Page(params, query):\n'
        '    global calls\n'
        '    calls += 1\n'
        '    number = calls\n'
        '    if started is not None: started.set()\n'
        '    if gate is not None: await gate.wait()\n'
        '    return h("p", {}, f"{params[\'slug\']}:{query}:{number}")\n'
        f'# test namespace {uuid.uuid4().hex}\n', encoding="utf-8")
    write_manifest(root)


@pytest.mark.parametrize("mode", ["asgi", "axum"])
@pytest.mark.parametrize("backend", ["memory", "sqlite", "redis"])
def test_query_variants_and_cross_worker_invalidation(tmp_path, backend, mode):
    if backend == "redis" and not os.environ.get("PYNEXT_TEST_REDIS_URL"):
        pytest.skip("PYNEXT_TEST_REDIS_URL is not configured")
    make_project(tmp_path)
    opts = {"production": True, "cache_backend": backend}
    if backend == "sqlite":
        opts["cache_path"] = tmp_path / "shared.sqlite3"
    elif backend == "redis":
        opts["cache_url"] = os.environ["PYNEXT_TEST_REDIS_URL"]

    async def run():
        first = create_app(tmp_path, **opts)
        second = create_app(tmp_path, **opts) if backend != "memory" else first
        module = first._module("app/catalog/[slug]/page.py")
        second_module = second._module("app/catalog/[slug]/page.py")
        server = None
        if mode == "axum":
            server = start_native(first, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}")
        else:
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=first), base_url="http://test")
        other = httpx.AsyncClient(transport=httpx.ASGITransport(app=second), base_url="http://other")
        try:
            async with client, other:
                path = "/catalog/a"
                english = await client.get(path + "?lang=en&ignored=private")
                assert english.status_code == 200
                assert "private" not in english.text
                assert (await client.get(path + "?ignored=other&lang=en")).text == english.text
                french = await client.get(path + "?lang=fr")
                assert french.text != english.text
                assert (await client.get(path + "?lang=fr")).text == french.text
                assert (await other.get(path + "?lang=en")).text == english.text
                assert (await client.get(path + "?tag=a&tag=b")).text != (
                    await client.get(path + "?tag=b&tag=a")).text
                assert (await client.get(path + "?" + "&".join(["lang=x"] * 9))).status_code == 400
                assert (await client.get(path, params={"lang": "x" * 2100})).status_code == 400
                private = await client.get(path + "?lang=en", headers={"cookie": "session=secret"})
                assert private.headers["cache-control"] == "private, no-store"
                assert private.text != english.text
                assert (await client.get(path + "?lang=en")).text == english.text

                before = second_module.calls
                assert await first.revalidate(path)
                new_en = await other.get(path + "?lang=en")
                assert second_module.calls == before + 1
                assert new_en.text != english.text or backend != "memory"
                before = second_module.calls
                await other.get(path + "?lang=fr")
                assert second_module.calls == before + 1
        finally:
            if server:
                await asyncio.to_thread(server.stop)
            await first.isr.close()
            if second is not first:
                await second.isr.close()
    asyncio.run(run())


def test_inflight_variant_cannot_be_seen_after_invalidation(tmp_path):
    make_project(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/catalog/[slug]/page.py")
        module.started, module.gate = asyncio.Event(), asyncio.Event()
        old_gate = module.gate
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                pending = asyncio.create_task(client.get("/catalog/a?lang=en"))
                await asyncio.wait_for(module.started.wait(), 3)
                assert await app.revalidate("/catalog/a")
                module.gate = None
                fresh = await client.get("/catalog/a?lang=en")
                old_gate.set()
                old = await pending
                assert old.text != fresh.text
                assert (await client.get("/catalog/a?lang=en")).text == fresh.text
        finally:
            await app.isr.close()
    asyncio.run(run())


@pytest.mark.parametrize("keys", ["lang", ["bad key"], ["x", "x"], list(map(str, range(17)))])
def test_query_allowlist_validation(keys):
    with pytest.raises(ValueError):
        page(render="isr", revalidate=1, query_keys=keys)
