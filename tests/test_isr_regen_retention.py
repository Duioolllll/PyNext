"""ISR regeneration retention (ROADMAP Part 2 Phase C, C-2 / P0-8).

Next.js ``retainPreviousCacheEntry``: when an ISR regeneration fails,
the previous cache entry is kept and served instead of erroring, and
the next retry is delayed with exponential backoff clamped to 3~30s.
"""
import asyncio
import time

import httpx
import pytest

from conftest import page_html
from pynext import Response, create_app
from pynext.build import write_manifest
from pynext.cache import ISRCache, CacheBusy, MAX_INFLIGHT, regen_retry_delay


async def _until(condition, timeout=5):
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def _rendering_cache(cache=None):
    """An ISRCache with a controllable render function.

    Returns ``(cache, render, state)`` where ``state`` is a dict with
    ``calls`` (render invocations) and ``fail`` (make renders raise).
    """
    state = {"calls": 0, "fail": False}

    async def render():
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("origin down")
        return Response(f"<p>v{state['calls']}</p>".encode())

    return cache if cache is not None else ISRCache(), render, state


def test_regen_retry_delay_clamped_to_3_to_30_seconds():
    # 3s, 6s, 12s, 24s, then clamped at 30s.
    assert [regen_retry_delay(n) for n in range(0, 9)] == [
        3.0, 3.0, 6.0, 12.0, 24.0, 30.0, 30.0, 30.0, 30.0,
    ]


def test_stale_regeneration_failure_serves_previous_and_backs_off():
    async def run():
        cache, render, state = _rendering_cache()
        policy = {"revalidate": 0.05, "stale": 60.0}
        try:
            assert await cache.get_or_render("/page", policy, render) == b"<p>v1</p>"
            await _until(lambda: cache.store.lookup("/page")[0] == "stale")
            state["fail"] = True
            # Stale: the previous value is served while one background
            # regeneration runs and fails.
            assert await cache.get_or_render("/page", policy, render) == b"<p>v1</p>"
            await _until(lambda: "/page" not in cache._inflight)
            assert state["calls"] == 2
            assert cache._regen_failures["/page"]["failures"] == 1
            # Immediate retries inside the backoff serve the previous
            # value without another regeneration attempt.
            for _ in range(3):
                assert await cache.get_or_render("/page", policy, render) == b"<p>v1</p>"
            assert state["calls"] == 2
            assert cache._regen_backoff_remaining("/page") > 0
            # Once the backoff has elapsed, the next request retries.
            cache._regen_failures["/page"]["next_retry_at"] = time.monotonic() - 1
            assert await cache.get_or_render("/page", policy, render) == b"<p>v1</p>"
            await _until(lambda: "/page" not in cache._inflight)
            assert state["calls"] == 3
            assert cache._regen_failures["/page"]["failures"] == 2
            # A successful regeneration clears the backoff and replaces
            # the retained value.
            state["fail"] = False
            cache._regen_failures["/page"]["next_retry_at"] = time.monotonic() - 1
            assert await cache.get_or_render("/page", policy, render) == b"<p>v1</p>"
            await _until(lambda: "/page" not in cache._inflight)
            assert state["calls"] == 4
            assert "/page" not in cache._regen_failures
            assert await cache.get_or_render("/page", policy, render) == b"<p>v4</p>"
        finally:
            await cache.close()

    asyncio.run(run())


def test_expired_regeneration_failure_serves_retained_value():
    async def run():
        cache, render, state = _rendering_cache()
        policy = {"revalidate": 0.05, "stale": 0.05}
        try:
            assert await cache.get_or_render("/page", policy, render) == b"<p>v1</p>"
            await _until(lambda: cache.store.lookup("/page")[0] == "expired")
            state["fail"] = True
            # Expired and the regeneration fails: the retained previous
            # value is served instead of an error.
            assert await cache.get_or_render("/page", policy, render) == b"<p>v1</p>"
            assert state["calls"] == 2
            assert cache._regen_failures["/page"]["failures"] == 1
            # A retry inside the backoff serves the retained value
            # without regenerating again.
            assert await cache.get_or_render("/page", policy, render) == b"<p>v1</p>"
            assert state["calls"] == 2
            # After the backoff, a successful regeneration replaces it.
            state["fail"] = False
            cache._regen_failures["/page"]["next_retry_at"] = time.monotonic() - 1
            assert await cache.get_or_render("/page", policy, render) == b"<p>v3</p>"
            assert "/page" not in cache._regen_failures
            assert cache.store.lookup("/page")[0] == "fresh"
        finally:
            await cache.close()

    asyncio.run(run())


def test_first_generation_failure_still_errors():
    async def run():
        cache, render, state = _rendering_cache()
        try:
            state["fail"] = True
            # No previous value exists, so there is nothing to retain.
            with pytest.raises(RuntimeError, match="origin down"):
                await cache.get_or_render("/page", {"revalidate": 60.0, "stale": 60.0}, render)
            assert "/page" not in cache._retained
        finally:
            await cache.close()

    asyncio.run(run())


def test_invalidate_drops_retained_value():
    async def run():
        cache, render, state = _rendering_cache()
        policy = {"revalidate": 0.05, "stale": 0.05}
        try:
            assert await cache.get_or_render("/page", policy, render) == b"<p>v1</p>"
            await _until(lambda: cache.store.lookup("/page")[0] == "expired")
            state["fail"] = True
            assert await cache.get_or_render("/page", policy, render) == b"<p>v1</p>"
            assert "/page" in cache._retained
            # An explicit invalidation voids the retained value: the
            # next request must regenerate (and fail) instead of
            # serving invalidated content. (The entry itself was
            # already evicted by the expired lookup, so invalidate
            # returns False here — only the retained/backoff drop is
            # asserted.)
            await cache.invalidate("/page")
            assert "/page" not in cache._retained
            assert "/page" not in cache._regen_failures
            with pytest.raises(RuntimeError, match="origin down"):
                await cache.get_or_render("/page", policy, render)
        finally:
            await cache.close()

    asyncio.run(run())


def test_tag_invalidation_voids_retained_value():
    async def run():
        cache, render, state = _rendering_cache()
        policy = {"revalidate": 0.05, "stale": 0.05}
        try:
            assert await cache.get_or_render("/page", policy, render, tags=("t1",)) == b"<p>v1</p>"
            await _until(lambda: cache.store.lookup("/page")[0] == "expired")
            state["fail"] = True
            # Invalidating the tag the entry carries voids the retained
            # value even though the entry itself already expired.
            assert await cache.revalidate_tag("t1")
            assert "/page" not in cache._retained
            with pytest.raises(RuntimeError, match="origin down"):
                await cache.get_or_render("/page", policy, render, tags=("t1",))
        finally:
            await cache.close()

    asyncio.run(run())


def test_shared_expired_regeneration_failure_serves_retained_value(tmp_path):
    async def run():
        cache, render, state = _rendering_cache()
        cache = ISRCache(cache_backend="sqlite",
                         cache_path=str(tmp_path / "isr.sqlite3"))
        policy = {"revalidate": 0.05, "stale": 0.05}
        try:
            assert await cache.get_or_render("/page", policy, render) == b"<p>v1</p>"
            await _until(lambda: cache.store.lookup("/page")[0] == "expired")
            state["fail"] = True
            # The shared row is deleted on expiry, but the retained
            # previous value is still served on regeneration failure.
            assert await cache.get_or_render("/page", policy, render) == b"<p>v1</p>"
            assert state["calls"] == 2
            # A retry inside the backoff does not regenerate again.
            assert await cache.get_or_render("/page", policy, render) == b"<p>v1</p>"
            assert state["calls"] == 2
        finally:
            await cache.close()

    asyncio.run(run())


def test_retained_body_accessor():
    async def run():
        cache, render, state = _rendering_cache()
        try:
            assert await cache.retained_body("/page") is None
            assert await cache.get_or_render("/page", {"revalidate": 0.05, "stale": 0.05}, render) == b"<p>v1</p>"
            assert await cache.retained_body("/page") == b"<p>v1</p>"
            # Still available after the entry itself expires (the store
            # drops it on lookup) — this is what the app's CacheBusy
            # handler falls back to.
            await _until(lambda: cache.store.lookup("/page")[0] == "expired")
            assert await cache.retained_body("/page") == b"<p>v1</p>"
            await cache.invalidate("/page")
            assert await cache.retained_body("/page") is None
        finally:
            await cache.close()

    asyncio.run(run())


def test_shared_cache_busy_serves_retained_value(tmp_path):
    async def run():
        cache = ISRCache(cache_backend="sqlite",
                         cache_path=str(tmp_path / "isr.sqlite3"))
        _, render, state = _rendering_cache(cache)
        policy = {"revalidate": 0.05, "stale": 0.05}
        dummies = []
        try:
            assert await cache.get_or_render("/page", policy, render) == b"<p>v1</p>"
            await _until(lambda: cache.store.lookup("/page")[0] == "expired")
            # Fill the generation pool so no regeneration can start.
            for _ in range(MAX_INFLIGHT):
                task = asyncio.create_task(asyncio.sleep(3600))
                dummies.append(task)
                cache._tasks.add(task)
            # Too busy to regenerate, but the retained previous value is
            # served instead of a CacheBusy error — and no render runs.
            assert await cache.get_or_render("/page", policy, render) == b"<p>v1</p>"
            assert state["calls"] == 1
            # With no retained value there is nothing to serve, so
            # CacheBusy still surfaces.
            with pytest.raises(CacheBusy):
                await cache.get_or_render("/other", policy, render)
        finally:
            for task in dummies:
                task.cancel()
            await cache.close()

    asyncio.run(run())


def _make_project(root):
    directory = root / "app" / "blog" / "[slug]"
    directory.mkdir(parents=True)
    (directory / "page.py").write_text(
        'from pynext import h, page\n'
        'calls = 0\nfail = False\n'
        '@page(render="isr", revalidate=0.1, stale=0.1)\n'
        'async def Page(params):\n'
        '    global calls\n'
        '    calls += 1\n'
        '    number = calls\n'
        '    if fail: raise RuntimeError("generation failed")\n'
        '    return h("p", {}, f"{params[\'slug\']}:{number}")\n'
    )


def test_http_expired_regeneration_failure_serves_previous(tmp_path):
    _make_project(tmp_path)
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        module = app._module("app/blog/[slug]/page.py")
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                first = await client.get("/blog/alpha")
                assert first.status_code == 200
                assert first.text == page_html("<p>alpha:1</p>")
                await _until(lambda: app.isr.store.lookup("/blog/alpha")[0] == "expired")
                module.fail = True
                # Expired and the regeneration fails: the previous page
                # is served with 200 instead of a 500.
                failed = await client.get("/blog/alpha")
                assert failed.status_code == 200
                assert failed.text == page_html("<p>alpha:1</p>")
                assert module.calls == 2
                # A retry inside the backoff does not regenerate again.
                again = await client.get("/blog/alpha")
                assert again.status_code == 200
                assert again.text == page_html("<p>alpha:1</p>")
                assert module.calls == 2
        finally:
            await app.isr.close()

    asyncio.run(run())
