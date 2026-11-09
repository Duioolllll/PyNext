"""Prefetch stale-time hint (P2-5, Next.js `staleTimes`).

Static pages and fresh ISR responses carry
``X-PyNext-Stale-Time: <seconds>`` — how long a prefetched copy may be
reused before it goes stale. The value is min(page stale time, layout
stale times); layouts declare one with ``@layout(stale=<seconds>)``.
Stale-served, private, and dynamic (SSR) responses carry no hint, so a
client never retains a copy that is already stale or user-specific.
Both transports must agree: the native fast path serves static pages
and fresh ISR hits without Python dispatch.
"""

import asyncio

import httpx
import pytest

from pynext import create_app
from pynext.build import write_manifest
from pynext.server import start_native

HEADER = "x-pynext-stale-time"


def _write_isr_project(root, layout_source=None):
    directory = root / "app" / "blog" / "[slug]"
    directory.mkdir(parents=True)
    if layout_source is not None:
        (root / "app" / "layout.py").write_text(layout_source, encoding="utf-8")
    (directory / "page.py").write_text(
        'from pynext import h, page\n'
        'started = None\ngate = None\n'
        '@page(render="isr", revalidate=30, stale=60)\n'
        'async def Page(params):\n'
        '    if started is not None: started.set()\n'
        '    if gate is not None: await gate.wait()\n'
        '    return h("p", {}, params["slug"])\n'
    )
    return directory / "page.py"


def _write_ssr_project(root):
    directory = root / "app"
    directory.mkdir(parents=True)
    (directory / "page.py").write_text(
        'from pynext import h, page\n'
        '@page()\n'
        'def Page(): return h("p", {}, "dynamic")\n'
    )


async def _until(condition, timeout=5):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def test_isr_prefetch_hint_on_miss_fresh_stale_and_private(tmp_path):
    _write_isr_project(tmp_path)
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client, \
                    httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as native:
                # Miss: newly generated, so the hint is the fresh window.
                miss = await client.get("/blog/alpha")
                assert miss.status_code == 200
                assert miss.headers[HEADER] == "30"

                # Fresh hit: hint is the remaining fresh window.
                fresh = await client.get("/blog/alpha")
                assert 28 <= int(fresh.headers[HEADER]) <= 30

                # Native fresh hit bypasses Python dispatch entirely, but
                # must carry the same hint.
                native_fresh = await native.get("/blog/alpha")
                assert 28 <= int(native_fresh.headers[HEADER]) <= 30

                # A directly inserted fresh entry with a 60s window.
                assert app.isr.store.insert("/blog/beta", b"<p>native</p>", 60, 1)
                native_inserted = await native.get("/blog/beta")
                assert native_inserted.text == "<p>native</p>"
                assert 58 <= int(native_inserted.headers[HEADER]) <= 60

                # Stale-served: already stale, so no hint — the client
                # must not retain this copy.
                assert app.isr.store.insert("/blog/alpha", b"<p>old</p>", 0.02, 60)
                await _until(lambda: app.isr.store.lookup("/blog/alpha")[0] == "stale")
                stale = await client.get("/blog/alpha")
                assert stale.text == "<p>old</p>"
                assert HEADER not in stale.headers

                # Private render: no hint, never prefetchable.
                private = await client.get("/blog/alpha", headers={"cookie": "session=x"})
                assert private.headers["cache-control"] == "private, no-store"
                assert HEADER not in private.headers
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_isr_prefetch_hint_is_min_of_page_and_layout_stale(tmp_path):
    _write_isr_project(tmp_path, layout_source=(
        'from pynext import h, layout\n'
        '@layout(stale=10)\n'
        'def Layout(children): return h("main", {}, children)\n'
    ))
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                miss = await client.get("/blog/alpha")
                assert miss.headers[HEADER] == "10"
                fresh = await client.get("/blog/alpha")
                assert fresh.headers[HEADER] == "10"
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_ssr_has_no_prefetch_hint(tmp_path):
    _write_ssr_project(tmp_path)
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                response = await client.get("/")
                assert response.status_code == 200
                assert HEADER not in response.headers
        finally:
            await app.isr.close()
    asyncio.run(run())


@pytest.mark.parametrize("layout_source, expected", [
    (None, "300"),
    ('from pynext import h, layout\n@layout(stale=10)\n'
     'def Layout(children): return h("main", {}, children)\n', "10"),
    ('from pynext import h, layout\n@layout\n'
     'def Layout(children): return h("main", {}, children)\n', "300"),
])
def test_ssg_prefetch_hint_in_asgi_and_native(tmp_path, layout_source, expected):
    directory = tmp_path / "app"
    directory.mkdir(parents=True)
    if layout_source is not None:
        (directory / "layout.py").write_text(layout_source, encoding="utf-8")
    (directory / "page.py").write_text(
        'from pynext import h, page\n@page(render="ssg")\ndef Page(): return h("p", {}, "built")\n'
    )
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        assert app.prefetch_stale_times == {"/": int(expected)}
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client, \
                    httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as native:
                assert (await client.get("/")).headers[HEADER] == expected
                assert (await native.get("/")).headers[HEADER] == expected
                assert (await native.head("/")).headers[HEADER] == expected
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_ssg_layout_stale_zero_means_no_hint(tmp_path):
    directory = tmp_path / "app"
    directory.mkdir(parents=True)
    (directory / "layout.py").write_text(
        'from pynext import h, layout\n@layout(stale=0)\n'
        'def Layout(children): return h("main", {}, children)\n'
    )
    (directory / "page.py").write_text(
        'from pynext import h, page\n@page(render="ssg")\ndef Page(): return h("p", {}, "built")\n'
    )
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        assert app.prefetch_stale_times == {}
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                assert HEADER not in (await client.get("/")).headers
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_streaming_isr_first_generation_carries_hint(tmp_path):
    directory = tmp_path / "app" / "blog" / "[slug]"
    directory.mkdir(parents=True)
    (directory / "page.py").write_text(
        'from pynext import h, page\n'
        'import asyncio\n'
        'started = None\ngate = None\n'
        '@page(render="isr", revalidate=30, stale=60)\n'
        'async def Page(params):\n'
        '    if started is not None: started.set()\n'
        '    if gate is not None: await gate.wait()\n'
        '    yield h("p", {}, "one")\n'
        '    yield h("p", {}, "two")\n'
    )
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/blog/[slug]/page.py")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                module.started, module.gate = asyncio.Event(), asyncio.Event()
                # The request must run as a task: the ASGI transport runs
                # the app inline, so awaiting the response directly would
                # never reach the gate release below.
                pending = asyncio.create_task(client.get("/blog/streamed"))
                await asyncio.wait_for(module.started.wait(), 3)
                module.gate.set()
                response = await pending
                assert response.status_code == 200
                assert response.headers[HEADER] == "30"
                assert b"<p>one</p>" in response.content and b"<p>two</p>" in response.content
        finally:
            await app.isr.close()
    asyncio.run(run())


@pytest.mark.parametrize("bad", ["-1", "604801", "'30'", "True", "float('nan')", "float('inf')"])
def test_layout_stale_rejects_invalid_values(tmp_path, bad):
    directory = tmp_path / "app"
    directory.mkdir(parents=True)
    (directory / "layout.py").write_text(
        'from pynext import h, layout\n'
        f'@layout(stale={bad})\n'
        'def Layout(children): return h("main", {}, children)\n'
    )
    (directory / "page.py").write_text(
        'from pynext import h, page\n@page(render="ssg")\ndef Page(): return h("p", {}, "built")\n'
    )
    with pytest.raises(ValueError, match="stale"):
        write_manifest(tmp_path)
        create_app(tmp_path, production=True)


def test_layout_stale_set_by_hand_is_rejected_at_startup(tmp_path):
    directory = tmp_path / "app"
    directory.mkdir(parents=True)
    (directory / "layout.py").write_text(
        'from pynext import h, layout\n@layout\n'
        'def Layout(children): return h("main", {}, children)\n'
        'Layout.__pynext_stale__ = -5\n'
    )
    (directory / "page.py").write_text(
        'from pynext import h, page\n@page(render="ssg")\ndef Page(): return h("p", {}, "built")\n'
    )
    # write_manifest already builds an app to render the static pages,
    # so the invalid layout fails the build before startup is reached.
    with pytest.raises(ValueError, match="Invalid layout stale time"):
        write_manifest(tmp_path)
        create_app(tmp_path, production=True)
