from conftest import page_html
import asyncio
import math

import httpx
import pytest

from pynext import _native, create_app, page
from pynext.build import write_manifest
from pynext.server import start_native


def make_project(root):
    directory = root / "app" / "blog" / "[slug]"
    directory.mkdir(parents=True)
    (directory / "page.py").write_text(
        'from pynext import h, page\n'
        'calls = 0\nstarted = None\ngate = None\nfail = False\n'
        '@page(render="isr", revalidate=30, stale=1)\n'
        'async def Page(params):\n'
        '    global calls\n'
        '    calls += 1\n'
        '    number = calls\n'
        '    if started is not None: started.set()\n'
        '    if gate is not None: await gate.wait()\n'
        '    if fail: raise RuntimeError("generation failed")\n'
        '    return h("p", {}, f"{params[\'slug\']}:{number}")\n'
    )
    return directory / "page.py"


async def until(condition, timeout=5):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def test_isr_single_flight_stale_failure_cancellation_and_native_hit(tmp_path):
    make_project(tmp_path)
    manifest = write_manifest(tmp_path)
    assert manifest["isr_policies"]["/blog/[slug]"] == {"revalidate": 30.0, "stale": 1.0, "expire": 31.0}

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/blog/[slug]/page.py")
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client, \
                    httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as native:
                module.started, module.gate = asyncio.Event(), asyncio.Event()
                requests = [asyncio.create_task(client.get("/blog/alpha?different=" + str(i))) for i in range(8)]
                await asyncio.wait_for(module.started.wait(), 3)
                assert module.calls == 1
                module.gate.set()
                results = await asyncio.gather(*requests)
                assert {r.text for r in results} == {page_html("<p>alpha:1</p>")}
                assert all(r.status_code == 200 for r in results)
                assert (await client.post("/blog/alpha")).status_code == 405

                # A fresh native hit must not call Python dispatch at all.
                assert app.isr.store.insert("/blog/alpha", b"<p>native-hit</p>", 60, 1)
                original = app.dispatch
                async def fail_dispatch(request):
                    raise AssertionError("fresh ISR hit crossed into Python")
                app.dispatch = fail_dispatch
                assert (await native.get("/blog/alpha")).text == "<p>native-hit</p>"
                head = await native.head("/blog/alpha")
                assert head.status_code == 200 and int(head.headers["content-length"]) == len(b"<p>native-hit</p>")
                app.dispatch = original

                # Cookie or authorization requests are rendered privately, never from the shared cache.
                for http, header in ((client, {"cookie": "session=a"}), (native, {"authorization": "Bearer x"})):
                    response = await http.get("/blog/alpha", headers=header)
                    assert response.text != "<p>native-hit</p>"
                    assert response.headers["cache-control"] == "private, no-store"
                    assert app.isr.store.lookup("/blog/alpha") == ("fresh", b"<p>native-hit</p>")

                # Stale responses return immediately while exactly one regeneration runs.
                assert app.isr.store.insert("/blog/alpha", b"<p>old</p>", 0.02, 2)
                await until(lambda: app.isr.store.lookup("/blog/alpha")[0] == "stale")
                module.started, module.gate = asyncio.Event(), asyncio.Event()
                previous_calls = module.calls
                stale = await asyncio.gather(*(client.get("/blog/alpha") for _ in range(5)))
                assert {r.text for r in stale} == {"<p>old</p>"}
                await asyncio.wait_for(module.started.wait(), 3)
                assert module.calls == previous_calls + 1
                module.gate.set()
                await until(lambda: app.isr.store.lookup("/blog/alpha")[0] == "fresh")
                assert (await client.get("/blog/alpha")).text == page_html(f"<p>alpha:{previous_calls + 1}</p>")
                await until(lambda: "/blog/alpha" not in app.isr._inflight)

                # C-2 (retainPreviousCacheEntry): a failed regeneration
                # keeps serving the retained previous page even after
                # the stale window expires, instead of a 500. The
                # retained value is the last rendered page, not the
                # directly seeded "<p>old</p>" body (which was never
                # retained); the exact generation varies with
                # background regenerations, so only the prefix is
                # asserted.
                assert app.isr.store.insert("/blog/alpha", b"<p>old</p>", 0.02, 1)
                await until(lambda: app.isr.store.lookup("/blog/alpha")[0] == "stale")
                module.started, module.gate, module.fail = asyncio.Event(), None, True
                assert (await client.get("/blog/alpha")).text == "<p>old</p>"
                await asyncio.wait_for(module.started.wait(), 3)
                await until(lambda: "/blog/alpha" not in app.isr._inflight)
                assert app.isr.store.lookup("/blog/alpha")[0] == "stale"
                await until(lambda: app.isr.store.lookup("/blog/alpha")[0] == "expired")
                final = await client.get("/blog/alpha")
                assert final.status_code == 200
                assert final.text.startswith("<p>alpha:")

                # One cancelled caller cannot cancel a shared generation.
                module.fail = False
                module.started, module.gate = asyncio.Event(), asyncio.Event()
                original_get = app.isr.get_or_render
                both_arrived, count = asyncio.Event(), 0
                async def record_arrival(key, policy, render, tags=()):
                    nonlocal count
                    if key == "/blog/cancel":
                        count += 1
                        if count == 2:
                            both_arrived.set()
                    return await original_get(key, policy, render, tags=tags)
                app.isr.get_or_render = record_arrival
                first = asyncio.create_task(client.get("/blog/cancel"))
                await asyncio.wait_for(module.started.wait(), 3)
                second = asyncio.create_task(client.get("/blog/cancel"))
                await asyncio.wait_for(both_arrived.wait(), 3)
                before = module.calls
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
                module.gate.set()
                assert (await second).status_code == 200
                assert module.calls == before
                assert app.isr.store.lookup("/blog/cancel")[0] == "fresh"
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_isr_capacity_and_lifespan_shutdown(tmp_path, monkeypatch):
    make_project(tmp_path)
    write_manifest(tmp_path)
    monkeypatch.setattr("pynext.cache.MAX_INFLIGHT", 1)
    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/blog/[slug]/page.py")
        module.started, module.gate = asyncio.Event(), asyncio.Event()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = asyncio.create_task(client.get("/blog/one"))
            await asyncio.wait_for(module.started.wait(), 3)
            busy = await client.get("/blog/two")
            assert busy.status_code == 503
            assert busy.headers["retry-after"] == "1"
            assert app.isr.store.insert("/blog/two", b"<p>stale</p>", 0.01, 2)
            await until(lambda: app.isr.store.lookup("/blog/two")[0] == "stale")
            assert (await client.get("/blog/two")).text == "<p>stale</p>"
            await app.isr.close()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not app.isr._inflight
    asyncio.run(run())


def test_isr_policy_drives_real_expiration_and_regeneration(tmp_path):
    source = make_project(tmp_path)
    source.write_text(source.read_text().replace("revalidate=30", "revalidate=0.2"))
    write_manifest(tmp_path)
    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/blog/[slug]/page.py")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/blog/policy")).text == page_html("<p>policy:1</p>")
            await until(lambda: app.isr.store.lookup("/blog/policy")[0] == "stale")
            module.started, module.gate = asyncio.Event(), asyncio.Event()
            assert (await client.get("/blog/policy")).text == page_html("<p>policy:1</p>")
            await asyncio.wait_for(module.started.wait(), 3)
            assert module.calls == 2
            module.gate.set()
            await until(lambda: "/blog/policy" not in app.isr._inflight)
            assert app.isr.store.lookup("/blog/policy") == ("fresh", page_html("<p>policy:2</p>").encode())
        await app.isr.close()
    asyncio.run(run())


def test_isr_timeout_returns_504_and_releases_the_slot(tmp_path, monkeypatch):
    make_project(tmp_path)
    write_manifest(tmp_path)
    monkeypatch.setattr("pynext.cache.GENERATION_TIMEOUT", 0.05)
    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/blog/[slug]/page.py")
        module.gate = asyncio.Event()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/blog/slow")).status_code == 504
            await until(lambda: not app.isr._inflight)
        await app.isr.close()
    asyncio.run(run())


def test_isr_shared_render_does_not_inherit_request_context(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "page.py").write_text(
        'from contextvars import ContextVar\nfrom pynext import h, page\n'
        'identity = ContextVar("identity", default="public")\n'
        '@page(render="isr", revalidate=30)\n'
        'async def Page(): return h("p", {}, identity.get())\n'
    )
    write_manifest(tmp_path)
    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/page.py")
        module.identity.set("private-data")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            private = await client.get("/", headers={"cookie": "session=x"})
            assert private.text == page_html("<p>private-data</p>")
            assert private.headers["cache-control"] == "private, no-store"
            assert private.headers["vary"] == "Cookie, Authorization"
            assert app.isr.store.lookup("/")[0] == "miss"
            public = await client.get("/")
            assert public.text == page_html("<p>public</p>")
            assert public.headers["cache-control"] == "public, max-age=0, must-revalidate"
            assert public.headers["vary"] == "Cookie, Authorization"
            assert app.isr.store.lookup("/")[1] == page_html("<p>public</p>").encode()
        await app.isr.close()
    asyncio.run(run())


def test_rust_cache_bounds_and_isolation():
    # Entry size is key length + body length, so "a"/"one" and "b"/"two"
    # are 4 bytes each and fit together in max_bytes=8.
    cache = _native.PageCache(2, 8, 3)
    other = _native.PageCache(2, 8, 3)
    assert cache.insert("a", b"one", 10, 0)
    assert other.lookup("a")[0] == "miss"
    assert cache.insert("b", b"two", 10, 0)
    assert cache.lookup("a") == ("fresh", b"one")
    assert cache.insert("c", b"tri", 10, 0)
    assert cache.lookup("b")[0] == "miss"
    assert not cache.insert("a", b"large", 10, 0)
    assert cache.lookup("a") == ("fresh", b"one")


def test_rust_cache_evicts_least_recently_used():
    cache = _native.PageCache(2, 100, 100)
    assert cache.insert("a", b"one", 10, 0)
    assert cache.insert("b", b"two", 10, 0)
    # Touching "a" makes it most recently used; "b" is evicted next.
    assert cache.lookup("a") == ("fresh", b"one")
    assert cache.insert("c", b"tri", 10, 0)
    assert cache.lookup("b")[0] == "miss"
    assert cache.lookup("a") == ("fresh", b"one")
    assert cache.lookup("c") == ("fresh", b"tri")


def test_rust_cache_size_accounts_for_key_length():
    # Sizes are key + body: "aa"+4 = 6, "bbbb"+4 = 8. Bodies alone
    # (4 + 4 = 8) would fit in max_bytes=10; with keys they do not.
    cache = _native.PageCache(100, 10, 100)
    assert cache.insert("aa", b"1234", 10, 0)
    assert cache.insert("bbbb", b"1234", 10, 0)
    assert cache.lookup("aa")[0] == "miss"
    assert cache.lookup("bbbb") == ("fresh", b"1234")


@pytest.mark.parametrize("kwargs", [
    {"render": "isr", "revalidate": 0},
    {"render": "isr", "revalidate": math.nan}, {"render": "isr", "revalidate": True},
    {"render": "isr", "revalidate": 1, "stale": -1}, {"render": "ssg", "revalidate": 1},
])
def test_invalid_isr_policy_is_rejected(kwargs):
    with pytest.raises(ValueError):
        page(**kwargs)


@pytest.mark.parametrize("policy", [
    '{"revalidate": 0, "stale": 1}',
    '{"revalidate": -1, "stale": 1}',
    '{"revalidate": True, "stale": 1}',
    '{"revalidate": float("nan"), "stale": 1}',
    '{"revalidate": 1, "stale": -1}',
])
def test_isr_policy_is_revalidated_when_loading_page(tmp_path, policy):
    source = make_project(tmp_path)
    source.write_text(source.read_text(encoding="utf-8") + f"\nPage.__pynext_cache_policy__ = {policy}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid ISR policy"):
        create_app(tmp_path)


def test_loaded_isr_policy_does_not_alias_page_metadata(tmp_path):
    make_project(tmp_path)
    app = create_app(tmp_path)
    app._module("app/blog/[slug]/page.py").Page.__pynext_cache_policy__["revalidate"] = 0
    assert app.isr_policies["/blog/[slug]"] == {"revalidate": 30.0, "stale": 1.0, "expire": 31.0}


def test_isr_rejects_request_access_and_static_generator(tmp_path):
    source = make_project(tmp_path)
    original = source.read_text()
    source.write_text(original.replace("async def Page(params):", "async def Page(request, params):"))
    with pytest.raises(ValueError, match="cannot accept request"):
        write_manifest(tmp_path)
    source.write_text(original + '\ndef generate_static_params(): return []\n')
    with pytest.raises(ValueError, match="cannot export generate_static_params"):
        write_manifest(tmp_path)


def test_isr_encoded_delimiter_and_plain_path_have_separate_entries(tmp_path):
    """``/files/a%2Fb`` and ``/files/a/b`` are different routes.

    The cache key is the routing-normalized path, so the encoded
    single-segment param and the two-segment static path never share
    an entry (and never serve each other's body).
    """
    directory = tmp_path / "app" / "files" / "[name]"
    directory.mkdir(parents=True)
    (directory / "page.py").write_text(
        'from pynext import h, page\n'
        '@page(render="isr", revalidate=60)\n'
        'def Page(params): return h("p", {}, f"dyn:{params[\'name\']}")\n',
        encoding="utf-8",
    )
    static = tmp_path / "app" / "files" / "a" / "b"
    static.mkdir(parents=True)
    (static / "page.py").write_text(
        'from pynext import h, page\n'
        '@page(render="isr", revalidate=60)\n'
        'def Page(): return h("p", {}, "static-ab")\n',
        encoding="utf-8",
    )
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        try:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = await client.get("/files/a%2Fb")
                assert first.status_code == 200
                assert "dyn:a%2Fb" in first.text
                second = await client.get("/files/a/b")
                assert second.status_code == 200
                assert "static-ab" in second.text
                # The cached entries stay separate on repeat requests.
                assert "dyn:a%2Fb" in (await client.get("/files/a%2Fb")).text
                assert "static-ab" in (await client.get("/files/a/b")).text
        finally:
            await app.isr.close()
    asyncio.run(run())
