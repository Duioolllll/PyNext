"""Bounded wait for sync workers (C-P1-2) and app-owned pools (C-P1-1).

The sync-worker path is "wait -> timeout -> refuse": when every sync
worker is busy a request joins a FIFO waiter queue (``sync_wait_timeout``
deadline, ``sync_queue_limit`` cap) instead of being refused at once,
and only a deadline expiry or a full queue produces 503. Sync handlers,
renders and SQLite session I/O each run on an app-owned pool instead of
sharing asyncio's default executor.
"""
import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from pynext import SessionMiddleware, SQLiteSessionStore, create_app

_BLOCKING_ROUTE = (
    "from pynext import route, Response\n"
    "import threading\n"
    "entered = threading.Event()\nrelease = threading.Event()\n"
    "names = []\n"
    "@route('GET')\ndef get(request):\n"
    "    names.append(threading.current_thread().name)\n"
    "    entered.set()\n"
    "    release.wait(5)\n"
    "    return Response('ok')\n"
)

_FIFO_ROUTE = (
    "from pynext import route, Response\n"
    "import threading\n"
    "order = []\n"
    "release1 = threading.Event()\nrelease2 = threading.Event()\n"
    "@route('GET')\ndef get(request):\n"
    "    n = len(order)\n"
    "    order.append(n + 1)\n"
    "    if n == 0:\n"
    "        release1.wait(5)\n"
    "    else:\n"
    "        release2.wait(5)\n"
    "    return Response('ok')\n"
)


def _write_route(tmp_path, code, path="/api/slow"):
    folder = tmp_path / "app" / path.strip("/")
    folder.mkdir(parents=True)
    (folder / "route.py").write_text(code, encoding="utf-8")


async def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition not met within %.1fs" % timeout)
        await asyncio.sleep(0.02)


def _default_pool_size():
    return min(32, (os.cpu_count() or 1) + 4)


async def _saturate_default_executor(loop):
    """Block every thread of the loop's default executor.

    Returns (release_event, futures). The caller must set the event and
    await the futures afterwards.
    """
    release = threading.Event()
    entered = []
    lock = threading.Lock()

    def blocker():
        with lock:
            entered.append(1)
        release.wait(10)

    futures = [loop.run_in_executor(None, blocker)
               for _ in range(_default_pool_size())]
    await _wait_until(lambda: len(entered) >= _default_pool_size())
    return release, futures


def test_sync_waiter_succeeds_after_slot_frees(tmp_path):
    """A queued sync request runs (200) once the worker slot frees."""
    _write_route(tmp_path, _BLOCKING_ROUTE)

    async def run():
        app = create_app(tmp_path, sync_workers=1)
        state = app.entries["/api/slow"]["GET"].__globals__
        try:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = asyncio.create_task(client.get("/api/slow"))
                await asyncio.to_thread(state["entered"].wait, 5)
                assert app._sync_active == 1
                second = asyncio.create_task(client.get("/api/slow"))
                await asyncio.sleep(0.3)
                # Still queued: the second handler has not started.
                assert len(state["names"]) == 1
                state["release"].set()
                assert (await asyncio.wait_for(first, 5)).status_code == 200
                assert (await asyncio.wait_for(second, 5)).status_code == 200
                assert len(state["names"]) == 2
                assert app._sync_active == 0
        finally:
            state["release"].set()
            await app.shutdown()

    asyncio.run(run())


def test_sync_wait_deadline_exceeded_returns_503_with_retry_after(tmp_path):
    """A waiter that outlasts sync_wait_timeout gets 503 + Retry-After."""
    _write_route(tmp_path, _BLOCKING_ROUTE)

    async def run():
        app = create_app(tmp_path, sync_workers=1, sync_wait_timeout=0.2)
        state = app.entries["/api/slow"]["GET"].__globals__
        try:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = asyncio.create_task(client.get("/api/slow"))
                await asyncio.to_thread(state["entered"].wait, 5)
                started = time.monotonic()
                response = await asyncio.wait_for(client.get("/api/slow"), 5)
                elapsed = time.monotonic() - started
                assert response.status_code == 503
                assert response.headers["retry-after"] == "1"
                # The saturation 503 is counted (C-P1-6): one refused
                # request counts once in workers.unavailable.
                assert app.metrics.snapshot()["workers"]["unavailable"] == 1
                # It waited out (most of) the deadline instead of
                # refusing at once, and did not wait forever.
                assert 0.1 <= elapsed < 2.0
                # The first handler is still running on its slot.
                assert app._sync_active == 1
                state["release"].set()
                assert (await asyncio.wait_for(first, 5)).status_code == 200
        finally:
            state["release"].set()
            await app.shutdown()

    asyncio.run(run())


def test_sync_queue_full_returns_503_immediately(tmp_path):
    """When the waiter queue is full the next request is refused at once."""
    _write_route(tmp_path, _BLOCKING_ROUTE)

    async def run():
        app = create_app(tmp_path, sync_workers=1, sync_queue_limit=1,
                         sync_wait_timeout=5)
        state = app.entries["/api/slow"]["GET"].__globals__
        try:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = asyncio.create_task(client.get("/api/slow"))
                await asyncio.to_thread(state["entered"].wait, 5)
                second = asyncio.create_task(client.get("/api/slow"))
                await asyncio.sleep(0.3)
                assert len(state["names"]) == 1  # second is queued
                started = time.monotonic()
                response = await asyncio.wait_for(client.get("/api/slow"), 5)
                elapsed = time.monotonic() - started
                assert response.status_code == 503
                assert response.headers["retry-after"] == "1"
                # The saturation 503 is counted (C-P1-6): one refused
                # request counts once in workers.unavailable.
                assert app.metrics.snapshot()["workers"]["unavailable"] == 1
                # Immediate refusal despite the 5s wait timeout: the
                # queue (limit 1) is already full.
                assert elapsed < 2.0
                state["release"].set()
                assert (await asyncio.wait_for(first, 5)).status_code == 200
                assert (await asyncio.wait_for(second, 5)).status_code == 200
        finally:
            state["release"].set()
            await app.shutdown()

    asyncio.run(run())


def test_sync_waiters_are_fifo(tmp_path):
    """The oldest waiter gets the freed slot first."""
    _write_route(tmp_path, _FIFO_ROUTE)

    async def run():
        app = create_app(tmp_path, sync_workers=1, sync_queue_limit=2,
                         sync_wait_timeout=5)
        state = app.entries["/api/slow"]["GET"].__globals__
        order = state["order"]
        try:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = asyncio.create_task(client.get("/api/slow"))
                await _wait_until(lambda: order == [1])
                second = asyncio.create_task(client.get("/api/slow"))
                third = asyncio.create_task(client.get("/api/slow"))
                await asyncio.sleep(0.3)
                assert order == [1]  # both queued, neither started
                state["release1"].set()
                await _wait_until(lambda: order == [1, 2])
                state["release2"].set()
                assert (await asyncio.wait_for(first, 5)).status_code == 200
                assert (await asyncio.wait_for(second, 5)).status_code == 200
                assert (await asyncio.wait_for(third, 5)).status_code == 200
                assert order == [1, 2, 3]
        finally:
            state["release1"].set()
            state["release2"].set()
            await app.shutdown()

    asyncio.run(run())


def test_sync_queue_metrics_record_length_and_wait(tmp_path):
    """Queue length and wait time are recorded (C-P1-2).

    The deepest the FIFO queue grew is kept on the app
    (``_sync_queue_max``), and each waiter's wait lands in the
    request's queue-wait metric (snapshot ``queue.waits_ms``) via
    ``queue_wait_var`` — the same channel the admission wait uses,
    so the C-P1-6 slot-wait histogram sees sync waits too.
    """
    _write_route(tmp_path, _BLOCKING_ROUTE)

    async def run():
        app = create_app(tmp_path, sync_workers=1, sync_queue_limit=2,
                         sync_wait_timeout=5)
        state = app.entries["/api/slow"]["GET"].__globals__
        try:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = asyncio.create_task(client.get("/api/slow"))
                await asyncio.to_thread(state["entered"].wait, 5)
                second = asyncio.create_task(client.get("/api/slow"))
                third = asyncio.create_task(client.get("/api/slow"))
                await asyncio.sleep(0.3)
                assert len(app._sync_waiters) == 2
                assert app._sync_queue_max == 2
                state["release"].set()
                assert (await asyncio.wait_for(first, 5)).status_code == 200
                assert (await asyncio.wait_for(second, 5)).status_code == 200
                assert (await asyncio.wait_for(third, 5)).status_code == 200
            queue = app.metrics.snapshot()["queue"]
            # Both waiters recorded their ~0.3s wait (the slot holder
            # never waited, so it contributes no sample).
            assert queue["wait_samples"] == 2
            assert queue["waits_ms"]["p50"] >= 100
        finally:
            state["release"].set()
            await app.shutdown()

    asyncio.run(run())


def test_sync_wait_timeout_records_wait_time(tmp_path):
    """A waiter that hits its deadline still records its wait (C-P1-2)."""
    _write_route(tmp_path, _BLOCKING_ROUTE)

    async def run():
        app = create_app(tmp_path, sync_workers=1, sync_wait_timeout=0.2)
        state = app.entries["/api/slow"]["GET"].__globals__
        try:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = asyncio.create_task(client.get("/api/slow"))
                await asyncio.to_thread(state["entered"].wait, 5)
                response = await asyncio.wait_for(client.get("/api/slow"), 5)
                assert response.status_code == 503
                state["release"].set()
                assert (await asyncio.wait_for(first, 5)).status_code == 200
            queue = app.metrics.snapshot()["queue"]
            assert queue["wait_samples"] == 1
            assert queue["waits_ms"]["p50"] >= 100
        finally:
            state["release"].set()
            await app.shutdown()

    asyncio.run(run())


def test_sync_handler_runs_on_app_pool_not_default_executor(tmp_path):
    """Sync handlers run on the app's sync pool (C-P1-1).

    Proved two ways: the handler thread carries the app pool's name,
    and the request still completes while every thread of asyncio's
    default executor is blocked.
    """
    _write_route(tmp_path, _BLOCKING_ROUTE)

    async def run():
        app = create_app(tmp_path, sync_workers=1)
        state = app.entries["/api/slow"]["GET"].__globals__
        loop = asyncio.get_running_loop()
        release_sat, sat_futures = await _saturate_default_executor(loop)
        try:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                task = asyncio.create_task(client.get("/api/slow"))
                await _wait_until(lambda: state["entered"].is_set())
                state["release"].set()
                response = await asyncio.wait_for(task, 5)
                assert response.status_code == 200
            assert state["names"][0].startswith("pynext-sync")
            assert not state["names"][0].startswith("asyncio")
        finally:
            state["release"].set()
            release_sat.set()
            await asyncio.gather(*sat_futures)
            await app.shutdown()

    asyncio.run(run())


def test_render_runs_on_app_pool_not_default_executor(tmp_path):
    """Page rendering runs on the app's render pool (C-P1-1).

    The page still renders while every thread of asyncio's default
    executor is blocked, so _native.render cannot be using it.
    """
    page = tmp_path / "app"
    page.mkdir(parents=True)
    (page / "page.py").write_text(
        'from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "ok")\n',
        encoding="utf-8")

    async def run():
        app = create_app(tmp_path)
        assert isinstance(app._render_executor, ThreadPoolExecutor)
        assert app._render_executor is not app._sync_executor
        assert app._render_executor is not app._session_executor
        loop = asyncio.get_running_loop()
        release_sat, sat_futures = await _saturate_default_executor(loop)
        try:
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await asyncio.wait_for(client.get("/"), 5)
                assert response.status_code == 200
                assert "ok" in response.text
        finally:
            release_sat.set()
            await asyncio.gather(*sat_futures)
            await app.shutdown()

    asyncio.run(run())


def test_session_store_uses_app_session_pool(tmp_path):
    """The App wires its session pool into SQLite stores (C-P1-1).

    The store's executor is the app's session pool, and a save/load
    round-trip still completes while asyncio's default executor is
    fully blocked.
    """
    page = tmp_path / "app"
    page.mkdir(parents=True)
    (page / "page.py").write_text(
        'from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "ok")\n',
        encoding="utf-8")
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = create_app(tmp_path, middleware=[SessionMiddleware(store, secure=False)])

    async def run():
        assert store.executor is app._session_executor
        loop = asyncio.get_running_loop()
        release_sat, sat_futures = await _saturate_default_executor(loop)
        try:
            token = "test-token-for-pool-check"
            assert await asyncio.wait_for(
                store.save(token, {"value": 1}, None, None, time.time() + 60), 3) == 1
            loaded = await asyncio.wait_for(store.load(token), 3)
            assert loaded[0] == {"value": 1}
        finally:
            release_sat.set()
            await asyncio.gather(*sat_futures)
            await app.shutdown()

    asyncio.run(run())


def test_session_store_without_app_uses_dedicated_pool(tmp_path):
    """A standalone store still avoids the shared default executor."""
    from pynext.sessions import _default_session_executor

    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    assert store.executor is None

    async def run():
        loop = asyncio.get_running_loop()
        release_sat, sat_futures = await _saturate_default_executor(loop)
        try:
            token = "test-token-standalone"
            assert await asyncio.wait_for(
                store.save(token, {"value": 2}, None, None, time.time() + 60), 3) == 1
            assert (await asyncio.wait_for(store.load(token), 3))[0] == {"value": 2}
        finally:
            release_sat.set()
            await asyncio.gather(*sat_futures)

    asyncio.run(run())
    assert _default_session_executor() is not None


def test_sync_pool_defaults(tmp_path):
    """Pool options default from sync_workers and stay separate."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app/page.py").write_text(
        'from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "ok")\n',
        encoding="utf-8")

    async def run():
        app = create_app(tmp_path, sync_workers=4)
        try:
            assert app.sync_workers == 4
            assert app.sync_wait_timeout == 2.0
            assert app.sync_queue_limit == 8
            assert app.render_workers == max(2, min(8, os.cpu_count() or 4))
            assert app.session_workers == 4
            assert app._sync_executor is not app._render_executor
            assert app._render_executor is not app._session_executor
            assert app._sync_executor is not app._session_executor
        finally:
            await app.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("kwargs", [
    {"sync_wait_timeout": "2"},
    {"sync_wait_timeout": True},
    {"sync_wait_timeout": -1},
    {"sync_wait_timeout": 301},
    {"sync_queue_limit": "2"},
    {"sync_queue_limit": True},
    {"sync_queue_limit": -1},
    {"sync_queue_limit": 65537},
    {"render_workers": 0},
    {"render_workers": 1025},
    {"render_workers": "4"},
    {"session_workers": 0},
    {"session_workers": 1025},
    {"session_workers": "4"},
])
def test_sync_pool_options_are_validated(tmp_path, kwargs):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/page.py").write_text(
        'from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "ok")\n',
        encoding="utf-8")
    with pytest.raises(ValueError):
        create_app(tmp_path, **kwargs)
