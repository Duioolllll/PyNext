"""ISR policy-based Cache-Control headers and failure retention.

Covers REPORT.md §2.1 #8/#9 (Next.js ``server/lib/cache-control.ts``
and ``server/response-cache/retainPreviousCacheEntry``):
* ISR responses carry ``s-maxage`` + ``stale-while-revalidate`` derived
  from the page policy, on the ASGI path, the streaming path and the
  native fresh-hit path.
* A failed stale regeneration retains the previous entry with the
  revalidate clamped to 3..30s, so retries breathe instead of hammering
  a failing origin on every request.
"""
import asyncio
import os

import httpx
import pytest

from pynext import create_app, page
from pynext.build import write_manifest
from pynext.cache import _retain_durations, cache_control_header
from pynext.server import start_native

from conftest import page_html


def test_cache_control_header_from_policy():
    assert cache_control_header({"revalidate": 30.0, "stale": 60.0}) == \
        "public, s-maxage=30, stale-while-revalidate=60"
    # No SWR window: only s-maxage is emitted.
    assert cache_control_header({"revalidate": 30.0, "stale": 0.0}) == \
        "public, s-maxage=30"
    # Fractional and large values render without scientific notation.
    assert cache_control_header({"revalidate": 0.5, "stale": 1.5}) == \
        "public, s-maxage=0.5, stale-while-revalidate=1.5"
    assert cache_control_header({"revalidate": 604800.0, "stale": 0.0}) == \
        "public, s-maxage=604800"


def test_retain_durations_clamp_to_nextjs_bounds():
    # Revalidate is clamped into 3..30s; the stale window is at least 3s
    # (Next.js: expire >= revalidate + 3).
    assert _retain_durations({"revalidate": 0.2, "stale": 1.0}) == (3.0, 3.0)
    assert _retain_durations({"revalidate": 30.0, "stale": 1.0}) == (30.0, 3.0)
    assert _retain_durations({"revalidate": 604800.0, "stale": 60.0}) == (30.0, 60.0)
    assert _retain_durations({"revalidate": 10.0, "stale": 60.0}) == (10.0, 60.0)


async def until(condition, timeout=10):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def make_project(root, revalidate=30, stale=60, streaming=False):
    directory = root / "app" / "blog" / "[slug]"
    directory.mkdir(parents=True)
    if streaming:
        body = (
            'from pynext import h, page\n'
            'calls = 0\ngate = None\n'
            f'@page(render="isr", revalidate={revalidate}, stale={stale})\n'
            'async def Page(params):\n'
            '    global calls\n'
            '    calls += 1\n'
            '    number = calls\n'
            '    yield h("p", {}, f"first-{number}")\n'
            '    if gate is not None: await gate.wait()\n'
            '    yield h("p", {}, f"last-{number}")\n'
        )
    else:
        body = (
            'from pynext import h, page\n'
            'calls = 0\nfail = False\n'
            f'@page(render="isr", revalidate={revalidate}, stale={stale})\n'
            'async def Page(params):\n'
            '    global calls\n'
            '    calls += 1\n'
            '    number = calls\n'
            '    if fail: raise RuntimeError("generation failed")\n'
            '    return h("p", {}, f"{params[\'slug\']}:{number}")\n'
        )
    (directory / "page.py").write_text(body)
    return directory / "page.py"


def test_isr_responses_carry_policy_swr_header(tmp_path):
    make_project(tmp_path)
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/blog/[slug]/page.py")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                # Miss (first generation) already carries the policy header.
                first = await client.get("/blog/alpha")
                assert first.status_code == 200
                assert first.headers["cache-control"] == \
                    "public, s-maxage=30, stale-while-revalidate=60"
                # Fresh hit: same header.
                assert (await client.get("/blog/alpha")).headers["cache-control"] == \
                    "public, s-maxage=30, stale-while-revalidate=60"
                # Stale serve: same header while one regeneration runs.
                assert app.isr.store.insert("/blog/alpha", b"<p>old</p>", 0.02, 60)
                await until(lambda: app.isr.store.lookup("/blog/alpha")[0] == "stale")
                stale = await client.get("/blog/alpha")
                assert stale.text == "<p>old</p>"
                assert stale.headers["cache-control"] == \
                    "public, s-maxage=30, stale-while-revalidate=60"
                await until(lambda: "/blog/alpha" not in app.isr._inflight)
                assert module.calls == 2
                # Private bypasses keep private, no-store.
                private = await client.get("/blog/alpha", headers={"cookie": "session=a"})
                assert private.headers["cache-control"] == "private, no-store"
                assert module.calls == 3
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_isr_header_without_swr_window(tmp_path):
    make_project(tmp_path, stale=0)
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                response = await client.get("/blog/alpha")
                assert response.headers["cache-control"] == "public, s-maxage=30"
        finally:
            await app.isr.close()
    asyncio.run(run())


def test_isr_streaming_response_carries_policy_header(tmp_path):
    make_project(tmp_path, streaming=True)
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/blog/[slug]/page.py")
        module.gate = asyncio.Event()
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        await incoming.put({"type": "http.request", "body": b"", "more_body": False})
        scope = {"type": "http", "method": "GET", "path": "/blog/stream",
                 "headers": []}
        task = asyncio.create_task(app(scope, incoming.get, outgoing.put))
        try:
            start = await asyncio.wait_for(outgoing.get(), 5)
            assert start["status"] == 200
            headers = dict(start["headers"])
            assert headers[b"cache-control"] == \
                b"public, s-maxage=30, stale-while-revalidate=60"
            module.gate.set()
            await asyncio.wait_for(task, 5)
        finally:
            if not task.done():
                task.cancel()
            await app.isr.close()
    asyncio.run(run())


def test_native_fresh_hit_carries_policy_header(tmp_path):
    make_project(tmp_path)
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            assert app.isr.store.insert("/blog/alpha", b"<p>native-hit</p>", 60, 60)
            async with httpx.AsyncClient(base_url=f"http://{server.address}",
                                         timeout=10) as client:
                response = await client.get("/blog/alpha")
                assert response.text == "<p>native-hit</p>"
                assert response.headers["cache-control"] == \
                    "public, s-maxage=30, stale-while-revalidate=60"
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


@pytest.mark.parametrize("backend", ["memory", "sqlite", "redis"])
def test_failed_regeneration_retains_previous_entry(tmp_path, backend):
    if backend == "redis" and not os.environ.get("PYNEXT_TEST_REDIS_URL"):
        pytest.skip("PYNEXT_TEST_REDIS_URL is not configured")
    # Tiny policy so the retained window (fresh 3s + stale 3s) expires
    # inside the test instead of the 30s a production policy would keep.
    make_project(tmp_path, revalidate=0.2, stale=1)
    write_manifest(tmp_path)
    options = {"production": True, "cache_backend": backend}
    if backend == "sqlite":
        options["cache_path"] = tmp_path / "cache.sqlite3"
    elif backend == "redis":
        options["cache_url"] = os.environ["PYNEXT_TEST_REDIS_URL"]

    async def run():
        app = create_app(tmp_path, **options)
        module = app._module("app/blog/[slug]/page.py")
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as client:
                assert (await client.get("/blog/alpha")).status_code == 200
                assert module.calls == 1
                module.fail = True
                await until(lambda: app.isr.store.lookup("/blog/alpha")[0] == "stale")
                # Stale serve triggers exactly one failing regeneration.
                assert (await client.get("/blog/alpha")).text == page_html("<p>alpha:1</p>")
                await until(lambda: "/blog/alpha" not in app.isr._inflight)
                assert module.calls == 2
                # The original stale window (1s) is long past, yet the
                # retained entry still serves and no retry storm starts.
                await asyncio.sleep(1.5)
                for _ in range(3):
                    assert (await client.get("/blog/alpha")).text == page_html("<p>alpha:1</p>")
                assert module.calls == 2
                # Once the retained window expires, the next request blocks
                # on regeneration again and fails visibly.
                await until(lambda: app.isr.store.lookup("/blog/alpha")[0] == "expired",
                            timeout=10)
                assert (await client.get("/blog/alpha")).status_code == 500
        finally:
            await app.isr.close()
    asyncio.run(run())
