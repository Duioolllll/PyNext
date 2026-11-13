"""CacheSignal warmup (REPORT.md §2.3 B P2 + tracks/b-rendering.md P2-4).

Covers the CacheSignal port of Next.js ``server/app-render/cache-signal.ts``,
the ISRCache warmup built on it, the App-level static-route warmup, the
``pynext start --warm-isr`` wiring, and the warmup metrics.
"""
import asyncio
import logging
import time

import httpx
import pytest

from conftest import page_html

from pynext import Response, create_app
from pynext.build import write_manifest
from pynext.cache import CacheBusy, CacheSignal, ISRCache, SharedEntry
from pynext.observability import MetricsCollector
from pynext.server import serve


# -- CacheSignal ----------------------------------------------------------

def test_cache_signal_ready_when_idle():
    async def run():
        signal = CacheSignal()
        await asyncio.wait_for(signal.cache_ready(), 1)
        await asyncio.wait_for(signal.input_ready(), 1)
        assert not signal.has_pending_reads()

    asyncio.run(run())


def test_cache_signal_waits_for_pending_reads():
    async def run():
        signal = CacheSignal()
        signal.begin_read()
        ready = asyncio.ensure_future(signal.cache_ready())
        await asyncio.sleep(0.05)
        assert not ready.done()
        signal.end_read()
        await asyncio.wait_for(ready, 1)

    asyncio.run(run())


def test_cache_signal_new_read_during_settle_delays_ready():
    """A read that starts after the last one ends — but before the
    settle check runs — must delay cache_ready(), mirroring Next.js's
    "wait a task before claiming the cache is settled" rule."""
    async def run():
        signal = CacheSignal()
        signal.begin_read()
        ready = asyncio.ensure_future(signal.cache_ready())
        await asyncio.sleep(0)  # let cache_ready() attach to the idle event
        signal.end_read()
        signal.begin_read()  # starts before cache_ready()'s settle check runs
        await asyncio.sleep(0.05)
        assert not ready.done()
        signal.end_read()
        await asyncio.wait_for(ready, 1)

    asyncio.run(run())


def test_cache_signal_input_ready_drains_current_reads():
    async def run():
        signal = CacheSignal()
        signal.begin_read()
        ready = asyncio.ensure_future(signal.input_ready())
        await asyncio.sleep(0.05)
        assert not ready.done()
        signal.end_read()
        await asyncio.wait_for(ready, 1)

    asyncio.run(run())


def test_cache_signal_end_read_without_begin_raises():
    with pytest.raises(ValueError, match="more end_read"):
        CacheSignal().end_read()


def test_cache_signal_track_read():
    async def run():
        signal = CacheSignal()

        async def work():
            assert signal.has_pending_reads()
            return 42

        assert await signal.track_read(work()) == 42
        assert not signal.has_pending_reads()

        async def boom():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await signal.track_read(boom())
        assert not signal.has_pending_reads()

    asyncio.run(run())


def test_cache_signal_subscribe_to_reads():
    async def run():
        source = CacheSignal()
        subscriber = CacheSignal()
        source.begin_read()
        unsubscribe = source.subscribe_to_reads(subscriber)
        assert subscriber.has_pending_reads()  # forwarded the in-flight read
        source.begin_read()
        source.end_read()
        source.end_read()
        await asyncio.wait_for(subscriber.cache_ready(), 1)
        unsubscribe()
        source.begin_read()
        assert not subscriber.has_pending_reads()
        source.end_read()

    asyncio.run(run())


def test_cache_signal_cannot_subscribe_to_itself():
    signal = CacheSignal()
    with pytest.raises(ValueError, match="cannot subscribe to itself"):
        signal.subscribe_to_reads(signal)


# -- ISRCache.warmup ------------------------------------------------------

async def _render_ok():
    return Response(b"<p>ok</p>")


def test_warmup_fills_cache_and_reports_summary():
    async def run():
        cache = ISRCache()
        try:
            result = await cache.warmup([("/page", {"revalidate": 60.0, "stale": 60.0}, _render_ok)])
            assert result == {"requested": 1, "warmed": 1, "skipped": 0, "failed": 0,
                              "pending": 0, "timed_out": False, "failures": {}}
            assert cache.store.lookup("/page") == ("fresh", b"<p>ok</p>")
            assert not cache.signal.has_pending_reads()
        finally:
            await cache.close()

    asyncio.run(run())


def test_warmup_skips_fresh_entries():
    calls = 0

    async def render():
        nonlocal calls
        calls += 1
        return Response(b"<p>ok</p>")

    async def run():
        cache = ISRCache()
        try:
            assert cache.store.insert("/page", b"<p>cached</p>", 60, 60)
            result = await cache.warmup([("/page", {"revalidate": 60.0, "stale": 60.0}, render)])
            assert result["warmed"] == 0
            assert result["skipped"] == 1
            assert calls == 0
            assert cache.store.lookup("/page") == ("fresh", b"<p>cached</p>")
        finally:
            await cache.close()

    asyncio.run(run())


def test_warmup_dedupes_repeated_keys():
    calls = 0

    async def render():
        nonlocal calls
        calls += 1
        return Response(b"<p>ok</p>")

    async def run():
        cache = ISRCache()
        try:
            result = await cache.warmup([
                ("/page", {"revalidate": 60.0, "stale": 60.0}, render),
                ("/page", {"revalidate": 60.0, "stale": 60.0}, render),
            ])
            assert result["warmed"] == 1
            assert result["skipped"] == 1
            assert calls == 1
        finally:
            await cache.close()

    asyncio.run(run())


def test_warmup_failure_is_best_effort(caplog):
    async def fail():
        raise RuntimeError("generation failed")

    async def run():
        cache = ISRCache()
        try:
            with caplog.at_level(logging.WARNING, logger="pynext"):
                result = await cache.warmup([
                    ("/bad", {"revalidate": 60.0, "stale": 60.0}, fail),
                    ("/good", {"revalidate": 60.0, "stale": 60.0}, _render_ok),
                ])
            assert result["warmed"] == 1
            assert result["failed"] == 1
            assert result["failures"] == {"/bad": "RuntimeError: generation failed"}
            assert cache.store.lookup("/good")[0] == "fresh"
            assert cache.store.lookup("/bad")[0] == "miss"
        finally:
            await cache.close()

    asyncio.run(run())
    assert "ISR warmup failed for /bad: RuntimeError: generation failed" in caplog.text


def test_warmup_timeout_reports_pending_and_generation_continues():
    async def run():
        gate = asyncio.Event()

        async def slow():
            await gate.wait()
            return Response(b"<p>slow</p>")

        cache = ISRCache()
        try:
            result = await cache.warmup(
                [("/slow", {"revalidate": 60.0, "stale": 60.0}, slow)], timeout=0.2)
            assert result["timed_out"] is True
            assert result["pending"] == 1
            assert result["warmed"] == 0
            gate.set()
            async with asyncio.timeout(5):
                while cache.store.lookup("/slow")[0] != "fresh":
                    await asyncio.sleep(0.01)
        finally:
            await cache.close()

    asyncio.run(run())


def test_warmup_signal_tracks_inflight_generations():
    async def run():
        started = asyncio.Event()
        gate = asyncio.Event()

        async def slow():
            started.set()
            await gate.wait()
            return Response(b"<p>slow</p>")

        cache = ISRCache()
        try:
            task = asyncio.ensure_future(cache.warmup(
                [("/slow", {"revalidate": 60.0, "stale": 60.0}, slow)]))
            await asyncio.wait_for(started.wait(), 3)
            assert cache.signal.has_pending_reads()
            gate.set()
            result = await asyncio.wait_for(task, 3)
            assert result["warmed"] == 1
            assert not cache.signal.has_pending_reads()
            await asyncio.wait_for(cache.signal.cache_ready(), 1)
        finally:
            await cache.close()

    asyncio.run(run())


def test_warmup_on_closed_cache_raises():
    async def run():
        cache = ISRCache()
        await cache.close()
        with pytest.raises(CacheBusy):
            await cache.warmup([("/page", {"revalidate": 60.0, "stale": 60.0}, _render_ok)])

    asyncio.run(run())


class _LeaseHeldShared:
    """A shared backend whose lease is held by another worker forever."""

    def __init__(self):
        self.lookups = 0
        self.status = "stale"

    def lookup_entry(self, key):
        self.lookups += 1
        return SharedEntry(self.status, None, 1, 0.0, 0.0)

    def acquire_lease(self, key, owner, ttl=None):
        return None

    def release_lease(self, key, owner):
        return False

    def owns_lease(self, key, owner):
        return False

    def insert_fenced(self, key, body, fresh_seconds, stale_seconds, expected_version, owner=None):
        return False

    def remove(self, key):
        return False

    def variant_generation(self, key, *, invalidate=False):
        return "token", False

    def close(self):
        pass


def test_wait_for_shared_wakes_on_signal_without_spinning():
    """The signal path must wake the poll when local reads settle, and
    must not turn it into a busy spin: ~6 polls fit in 0.3s at the
    0.05s interval."""
    async def run():
        cache = ISRCache()
        try:
            shared = _LeaseHeldShared()
            cache.shared = shared
            signal = cache.signal
            signal.begin_read()

            async def release():
                await asyncio.sleep(0.1)
                signal.end_read()

            releaser = asyncio.ensure_future(release())
            started = time.monotonic()
            outcome, payload = await cache._wait_for_shared("/page", 0.3, signal)
            elapsed = time.monotonic() - started
            await releaser
            assert outcome == "timeout"
            assert payload is None
            assert elapsed >= 0.25
            assert shared.lookups < 50
        finally:
            await cache.close()

    asyncio.run(run())


# -- App-level warmup -----------------------------------------------------

def _write_warmup_project(root):
    (root / "app").mkdir()
    (root / "app" / "page.py").write_text(
        'from pynext import h, page\n'
        'calls = 0\n'
        '@page(render="isr", revalidate=60)\n'
        'async def Page():\n'
        '    global calls\n'
        '    calls += 1\n'
        '    return h("p", {}, f"home:{calls}")\n'
    )
    stream = root / "app" / "stream"
    stream.mkdir()
    (stream / "page.py").write_text(
        'from pynext import h, page\n'
        'calls = 0\n'
        '@page(render="isr", revalidate=60)\n'
        'async def Page():\n'
        '    global calls\n'
        '    calls += 1\n'
        '    yield h("p", {}, f"stream:{calls}")\n'
    )
    blog = root / "app" / "blog" / "[slug]"
    blog.mkdir(parents=True)
    (blog / "page.py").write_text(
        'from pynext import h, page\n'
        '@page(render="isr", revalidate=60)\n'
        'async def Page(params):\n'
        '    return h("p", {}, params["slug"])\n'
    )
    search = root / "app" / "search"
    search.mkdir()
    (search / "page.py").write_text(
        'from pynext import h, page\n'
        '@page(render="isr", revalidate=60, query_keys=("lang",))\n'
        'async def Page(query):\n'
        '    return h("p", {}, query)\n'
    )


def test_app_warm_isr_cache_warms_static_routes(tmp_path):
    _write_warmup_project(tmp_path)
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        try:
            await app.startup()
            result = await app.warm_isr_cache()
            assert result["requested"] == 2
            assert result["warmed"] == 2
            assert result["failed"] == 0
            assert result["skipped_routes"] == {"dynamic": ["/blog/[slug]"], "query": ["/search"]}
            assert app.isr.store.lookup("/")[0] == "fresh"
            assert app.isr.store.lookup("/stream")[0] == "fresh"
            assert app.metrics.snapshot()["warmup"]["warmed"] == 2
            # A request for a warmed page serves the cached body without
            # re-rendering.
            module = app._module("app/page.py")
            calls_before = module.calls
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/")
                assert response.status_code == 200
                assert response.text == page_html("<p>home:1</p>")
                stream = await client.get("/stream")
                assert stream.status_code == 200
                assert stream.text == page_html("<p>stream:1</p>")
            assert module.calls == calls_before
        finally:
            await app.shutdown()

    asyncio.run(run())


def test_app_warm_isr_cache_failure_is_best_effort(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.py").write_text(
        'from pynext import h, page\n'
        '@page(render="isr", revalidate=60)\n'
        'async def Page():\n'
        '    raise RuntimeError("page is broken")\n'
    )
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        try:
            await app.startup()
            result = await app.warm_isr_cache()
            assert result["warmed"] == 0
            assert result["failed"] == 1
            assert result["failures"] == {"/": "RuntimeError: page is broken"}
        finally:
            await app.shutdown()

    asyncio.run(run())


def test_serve_warm_isr_prefills_cache_before_first_request(tmp_path):
    _write_warmup_project(tmp_path)
    write_manifest(tmp_path)

    async def run():
        app = create_app(tmp_path, production=True)
        stop_event = asyncio.Event()
        address = {}

        async def serve_task():
            await serve(app, "127.0.0.1", 0, stop_event=stop_event, shutdown_timeout=10.0,
                        warm_isr=True, on_ready=lambda value: address.__setitem__("value", value))

        task = asyncio.create_task(serve_task())
        try:
            async with asyncio.timeout(10):
                while "value" not in address:
                    await asyncio.sleep(0.05)
            async with asyncio.timeout(10):
                while app.isr.store.lookup("/")[0] != "fresh":
                    await asyncio.sleep(0.01)
            module = app._module("app/page.py")
            assert module.calls == 1
            async with httpx.AsyncClient(trust_env=False,
                                         base_url=f"http://{address['value']}", timeout=10) as client:
                response = await client.get("/")
                assert response.status_code == 200
                assert response.text == page_html("<p>home:1</p>")
            assert module.calls == 1
        finally:
            stop_event.set()
            await asyncio.wait_for(task, 10)

    asyncio.run(run())


# -- Warmup metrics -------------------------------------------------------

def test_warmup_metrics_recorded():
    collector = MetricsCollector()
    collector.observe_warmup({"warmed": 2, "skipped": 1, "failed": 1, "pending": 0,
                              "timed_out": True, "failures": {"/bad": "RuntimeError: boom"}})
    snap = collector.snapshot()
    assert snap["warmup"] == {"warmed": 2, "skipped": 1, "failed": 1, "pending": 0, "timed_out": 1}
    text = collector.prometheus_text()
    assert 'pynext_warmup_total{outcome="warmed"} 2' in text
    assert 'pynext_warmup_total{outcome="timed_out"} 1' in text
