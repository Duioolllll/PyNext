"""C-P1-3 / C-P1-4: registration-time sync/async cache, unified sync entry,
separate exit limiter, contextvars propagation."""
import asyncio
import contextvars
import functools
import threading

import httpx
import pytest

from pynext import Request, create_app
from pynext.app import _SyncHandlerBusy, _invoke
from pynext.sync_dispatch import (
    CALL_ASYNC,
    CALL_ASYNC_GEN,
    CALL_SYNC,
    cache_call_kind,
    cached_call_kind,
    is_async_callable,
    run_sync_with_context,
)

TRACE_VAR = contextvars.ContextVar("test_sync_dispatch_trace", default=None)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _minimal_app(tmp_path):
    _write(tmp_path / "app" / "page.py",
           'from pynext import page, h\n@page()\ndef Page(): return h("p", {}, "ok")\n')
    return tmp_path


class _SyncCallable:
    def __call__(self):
        return "sync-instance"


class _AsyncCallable:
    async def __call__(self):
        return "async-instance"


def test_is_async_callable_variants():
    async def afn():
        return 1

    def sfn():
        return 1

    async def agen():
        yield 1

    assert is_async_callable(afn) is True
    assert is_async_callable(sfn) is False
    assert is_async_callable(functools.partial(afn)) is True
    assert is_async_callable(functools.partial(sfn)) is False
    assert is_async_callable(_AsyncCallable()) is True
    assert is_async_callable(_SyncCallable()) is False
    # An async generator function is neither: it builds an iterator.
    assert is_async_callable(agen) is False
    assert cache_call_kind(agen) == CALL_ASYNC_GEN
    assert cache_call_kind(afn) == CALL_ASYNC
    assert cache_call_kind(sfn) == CALL_SYNC


def test_cached_kind_stamp_is_trusted(tmp_path):
    """_invoke reads the registration stamp, not a fresh inspect.

    A sync function stamped "async" is awaited directly (and fails,
    because its return value is not awaitable); an async function
    stamped "sync" is routed through _invoke_sync and its returned
    coroutine is still awaited. Both prove the stamp decides the path.
    """
    async def run():
        app = create_app(_minimal_app(tmp_path))
        try:
            def sync_fn():
                return 42

            sync_fn.__pynext_call_kind__ = CALL_ASYNC
            with pytest.raises(TypeError):
                await _invoke(sync_fn)

            async def async_fn():
                return "async-result"

            async_fn.__pynext_call_kind__ = CALL_SYNC
            assert await _invoke(async_fn, _app=app) == "async-result"
        finally:
            await app.shutdown()

    asyncio.run(run())


def test_registration_stamps_page_layout_api_and_asyncgen(tmp_path):
    _write(tmp_path / "app" / "page.py",
           'from pynext import page, h\n@page()\ndef Page(): return h("p", {}, "ok")\n')
    _write(tmp_path / "app" / "layout.py",
           'from pynext import layout, h\n@layout()\n'
           'def Layout(children): return h("div", {}, children)\n')
    _write(tmp_path / "app" / "async" / "page.py",
           'from pynext import page, h\n@page()\nasync def Page(): return h("p", {}, "ok")\n')
    _write(tmp_path / "app" / "stream" / "page.py",
           'from pynext import page, h\n@page()\nasync def Page():\n'
           '    yield h("p", {}, "a")\n    yield h("p", {}, "b")\n')
    _write(tmp_path / "app" / "api" / "echo" / "route.py",
           'from pynext import route, Response\n@route("GET")\n'
           'def get(request): return Response("ok")\n')

    async def run():
        app = create_app(tmp_path)
        try:
            page_fn, layouts = app.entries["/"]
            assert cached_call_kind(page_fn) == CALL_SYNC
            assert getattr(page_fn, "__pynext_call_kind__") == CALL_SYNC
            assert len(layouts) == 1
            assert cached_call_kind(layouts[0]) == CALL_SYNC
            async_fn, _ = app.entries["/async"]
            assert cached_call_kind(async_fn) == CALL_ASYNC
            gen_fn, _ = app.entries["/stream"]
            assert cached_call_kind(gen_fn) == CALL_ASYNC_GEN
            api_fn = app.entries["/api/echo"]["GET"]
            assert cached_call_kind(api_fn) == CALL_SYNC
            # The asyncgen branch in _render_page_inner still renders.
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/stream")
                assert response.status_code == 200
                assert "a" in response.text and "b" in response.text
        finally:
            await app.shutdown()

    asyncio.run(run())


def test_requestless_invoke_goes_through_sync_limiter(tmp_path):
    """Build-time / prerender sync work is limiter-guarded (C-P1-3b).

    With sync_workers=1 and the one slot held, a request-less _invoke
    of a sync callable is refused with _SyncHandlerBusy — before this
    change it ran on a bare to_thread, bypassing the limit entirely.
    """
    async def run():
        app = create_app(_minimal_app(tmp_path), sync_workers=1)
        entered = threading.Event()
        release = threading.Event()

        def blocking():
            entered.set()
            release.wait(5)

        def other():
            return "other"

        try:
            task = asyncio.create_task(
                app._invoke_sync(blocking, {}, Request("GET", "/x", _app=app)))
            await asyncio.to_thread(entered.wait, 5)
            assert app._sync_active == 1
            with pytest.raises(_SyncHandlerBusy):
                await _invoke(other, _app=app)
            release.set()
            await task
            assert app._sync_active == 0
            assert await _invoke(other, _app=app) == "other"
        finally:
            release.set()
            await app.shutdown()

    asyncio.run(run())


def test_sync_fn_returning_coroutine_is_still_awaited(tmp_path):
    async def run():
        app = create_app(_minimal_app(tmp_path))
        try:
            async def inner():
                return "from-coro"

            def sync_returns_coro():
                return inner()

            assert await _invoke(sync_returns_coro, _app=app) == "from-coro"
        finally:
            await app.shutdown()

    asyncio.run(run())


def test_exit_lane_runs_while_entry_limiter_saturated(tmp_path):
    """Teardown on the exit lane never waits behind sync workers (C-P1-3c).

    With sync_workers=1 and the one entry slot held by a blocking sync
    handler, _run_exit_sync still completes promptly, on a pynext-exit
    thread, while _sync_active stays 1.
    """
    async def run():
        app = create_app(_minimal_app(tmp_path), sync_workers=1)
        entered = threading.Event()
        release = threading.Event()

        def blocking():
            entered.set()
            release.wait(5)

        try:
            task = asyncio.create_task(
                app._invoke_sync(blocking, {}, Request("GET", "/x", _app=app)))
            await asyncio.to_thread(entered.wait, 5)
            assert app._sync_active == 1
            name = await asyncio.wait_for(
                app._run_exit_sync(lambda: threading.current_thread().name), 2)
            assert name.startswith("pynext-exit")
            assert app._sync_active == 1
            release.set()
            await task
        finally:
            release.set()
            await app.shutdown()

    asyncio.run(run())


def test_context_propagates_into_sync_worker_and_stays_copied(tmp_path):
    """A sync worker sees the caller's contextvars; its writes stay local."""
    async def run():
        app = create_app(_minimal_app(tmp_path))
        try:
            seen = {}

            def work():
                seen["in_worker"] = TRACE_VAR.get()
                TRACE_VAR.set("mutated-in-worker")
                return threading.current_thread().name

            TRACE_VAR.set("request-123")
            name = await app._invoke_sync(work, {}, Request("GET", "/x", _app=app))
            assert seen["in_worker"] == "request-123"
            assert TRACE_VAR.get() == "request-123"
            assert name != threading.current_thread().name
            # The same guarantee holds for the bare primitive.
            seen.clear()
            TRACE_VAR.set("request-456")
            await run_sync_with_context(work)
            assert seen["in_worker"] == "request-456"
            assert TRACE_VAR.get() == "request-456"
        finally:
            await app.shutdown()

    asyncio.run(run())


def test_exit_lane_propagates_context(tmp_path):
    async def run():
        app = create_app(_minimal_app(tmp_path))
        try:
            seen = {}

            def work():
                seen["value"] = TRACE_VAR.get()

            TRACE_VAR.set("teardown-ctx")
            await app._run_exit_sync(work)
            assert seen["value"] == "teardown-ctx"
        finally:
            await app.shutdown()

    asyncio.run(run())


def test_exit_pool_survives_app_restart(tmp_path):
    """Shutdown retires the exit pool, but a restart recreates it.

    Regression: the first shutdown retired the pool, and the second
    shutdown submitted the cache close to the dead pool
    ("cannot schedule new futures after shutdown").
    """
    async def run():
        app = create_app(_minimal_app(tmp_path), cache_backend="sqlite",
                         cache_path=str(tmp_path / "isr.sqlite3"))
        try:
            first = app._exit_executor
            await app.startup()
            await app.shutdown()
            with pytest.raises(RuntimeError):
                first.submit(lambda: None)
            # Restart recreates the pool; a second shutdown works.
            await app.startup()
            assert app._exit_executor is not first
            assert app.is_ready
            await app.shutdown()
            assert not app.is_ready
            # Teardown after shutdown recreates the pool on demand.
            name = await app._run_exit_sync(lambda: threading.current_thread().name)
            assert name.startswith("pynext-exit")
        finally:
            await app.shutdown()

    asyncio.run(run())


def test_isr_close_uses_exit_executor_and_shutdown_retires_it(tmp_path):
    async def run():
        app = create_app(_minimal_app(tmp_path))
        try:
            assert app._exit_executor is not None
            await app.isr.close(executor=app._exit_executor)
            assert app.isr._closed
            # Closing twice (shutdown closes again) stays harmless.
            await app.shutdown()
            with pytest.raises(RuntimeError):
                app._exit_executor.submit(lambda: None)
        finally:
            await app.shutdown()

    asyncio.run(run())
