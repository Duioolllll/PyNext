"""Timed-out synchronous handlers remain bounded and observe cancellation."""
import asyncio
import logging
import socket
import threading
import time

import httpx
import pytest

from pynext import Request, RequestCancelled, create_app
from pynext.server import start_native


def test_request_cancellation_prevents_framework_side_effects():
    request = Request("POST", "/")
    assert not request.cancelled
    request._cancel_event.set()
    with pytest.raises(RequestCancelled):
        request.check_cancelled()
    with pytest.raises(RequestCancelled):
        asyncio.run(request.revalidate("/"))


def test_cooperative_worker_exit_before_awaiter_cancellation_keeps_deadline_status(tmp_path, monkeypatch, caplog):
    from pynext.http import _RequestCancellation

    caplog.set_level(logging.INFO, logger="pynext")
    route = tmp_path / "app/slow"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import route, Response\n@route("GET")\n'
        'def get(request):\n'
        '    request._cancel_event.wait(5)\n'
        '    request.check_cancelled()\n'
        '    return Response("unexpected")\n')
    cancel = _RequestCancellation.cancel

    def yield_after_signalling(self, status):
        cancel(self, status)
        # Model a legal thread switch between setting the signal and
        # cancelling the coroutine: cooperative work can finish first.
        time.sleep(0.1)

    monkeypatch.setattr(_RequestCancellation, "cancel", yield_after_signalling)
    monkeypatch.setattr("pynext.server.REQUEST_TIMEOUT", 0.15)

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False) as client:
                assert (await client.get("/slow")).status_code == 504
            assert await app.wait_for_inflight(2)
            assert app.metrics.snapshot()["routes"]["/slow"]["status"] == {"504": 1}
            assert not any(record.msg == "Request failed" for record in caplog.records)
        finally:
            await asyncio.to_thread(server.stop)
            await app.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_unsignalled_cancellation_exception_remains_an_application_error(tmp_path, mode):
    route = tmp_path / "app/error"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import route, RequestCancelled\n@route("GET")\n'
        'def get(): raise RequestCancelled("application error without cancellation")\n')

    async def run():
        app = create_app(tmp_path)
        server = None
        if mode == "axum":
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        else:
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")
        try:
            async with client:
                assert (await asyncio.wait_for(client.get("/error"), 2)).status_code == 500
            assert app.metrics.snapshot()["routes"]["/error"]["status"] == {"500": 1}
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("transport", ["native-timeout", "asgi-disconnect"])
def test_cancelled_sync_work_keeps_capacity_until_thread_finishes(tmp_path, monkeypatch, caplog, transport):
    caplog.set_level(logging.INFO, logger="pynext")
    folder = tmp_path / "app/api/slow"
    folder.mkdir(parents=True)
    (folder / "route.py").write_text(
        "from pynext import route, Response\n"
        "import threading\n"
        "entered = threading.Event()\nrelease = threading.Event()\nfinished = threading.Event()\nwrites = []\n"
        "@route('POST')\ndef post(request):\n"
        "    entered.set()\n"
        "    try:\n"
        "        release.wait(5)\n"
        "        request.check_cancelled()\n"
        "        writes.append('committed')\n"
        "        return Response('done')\n"
        "    finally:\n"
        "        finished.set()\n")

    async def exercise():
        # sync_wait_timeout=0.05: the second request must still be
        # refused fast. With the default 2.0s bounded wait it would
        # outlast the 0.15s bridge timeout in the native branch and the
        # client would see 504 instead of 503.
        app = create_app(tmp_path, sync_workers=1, sync_wait_timeout=0.05)
        handler = app.entries["/api/slow"]["POST"]
        state = handler.__globals__
        server = None
        try:
            if transport == "native-timeout":
                monkeypatch.setattr("pynext.server.REQUEST_TIMEOUT", 0.15)
                server = start_native(app, asyncio.get_running_loop(), port=0)
                async with httpx.AsyncClient(base_url=f"http://{server.address}") as client:
                    result = await client.post("/api/slow")
                    assert result.status_code == 504
                    assert state["entered"].is_set()
                    assert (await client.post("/api/slow")).status_code == 503
                    assert (await client.get("/healthz")).status_code == 200
            else:
                messages = asyncio.Queue()
                await messages.put({"type": "http.request", "body": b""})
                async def send(message):
                    pytest.fail(f"Disconnected request emitted a response: {message}")
                task = asyncio.create_task(app({"type": "http", "method": "POST", "path": "/api/slow",
                                               "headers": [], "scheme": "http"}, messages.get, send))
                assert await asyncio.to_thread(state["entered"].wait, 2)
                await messages.put({"type": "http.disconnect"})
                await asyncio.wait_for(task, 2)
                result = await app.dispatch(Request("POST", "/api/slow", _app=app))
                assert result.status == 503
            assert not await app.wait_for_inflight(0)
            assert app._sync_active == 1
            state["release"].set()
            assert await asyncio.to_thread(state["finished"].wait, 2)
            assert await app.wait_for_inflight(2)
            assert state["writes"] == []
            expected_status = 504 if transport == "native-timeout" else 499
            assert app.metrics.snapshot()["routes"]["/api/slow"]["status"] == {
                str(expected_status): 1, "503": 1,
            }
            statuses = [record.status for record in caplog.records
                        if record.name == "pynext" and record.msg == "request"
                        and getattr(record, "route", None) == "/api/slow"]
            assert sorted(statuses) == sorted([expected_status, 503])
            # Actual completion releases capacity for a new request.
            response = await app.dispatch(Request("POST", "/api/slow", _app=app))
            assert response.status == 200
            assert state["writes"] == ["committed"]
        finally:
            state["release"].set()
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.shutdown()

    asyncio.run(exercise())


def test_native_client_disconnect_cancels_running_sync_handler(tmp_path, caplog):
    """A client that closes the TCP connection cancels the sync handler (C-P1-8).

    Axum drops the in-flight handler future on connection close, the
    bridge notices within one poll interval and cancels the Python task,
    and the sync worker observes the cancellation signal instead of
    committing its write. The slot stays occupied until the thread
    actually finishes (ADR-0002), then the worker is reusable: a
    follow-up request succeeds and commits.
    """
    caplog.set_level(logging.INFO, logger="pynext")
    folder = tmp_path / "app/api/slow"
    folder.mkdir(parents=True)
    (folder / "route.py").write_text(
        "from pynext import route, Response\n"
        "import threading\n"
        "entered = threading.Event()\nobserved = threading.Event()\nrelease = threading.Event()\nfinished = threading.Event()\nwrites = []\ncalls = 0\n"
        "@route('POST')\ndef post(request):\n"
        "    global calls\n"
        "    calls += 1\n"
        "    entered.set()\n"
        "    try:\n"
        "        if calls == 1:\n"
        "            request._cancel_event.wait(5)\n"
        "            if request.cancelled:\n"
        "                observed.set()\n"
        "        release.wait(5)\n"
        "        request.check_cancelled()\n"
        "        writes.append('committed')\n"
        "        return Response('done')\n"
        "    finally:\n"
        "        finished.set()\n")

    async def exercise():
        app = create_app(tmp_path, sync_workers=1)
        handler = app.entries["/api/slow"]["POST"]
        state = handler.__globals__
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            host, port = server.address.rsplit(":", 1)

            def disconnect_mid_request():
                sock = socket.create_connection((host, int(port)), timeout=5)
                # The Host must be on the native allowlist (S-P1-19
                # loopback default): the placeholder "x" was answered
                # 400 by Rust before the handler ever ran, so the sync
                # handler never started.
                sock.sendall(
                    f"POST /api/slow HTTP/1.1\r\nHost: {host}\r\n"
                    "Content-Length: 0\r\n\r\n".encode("ascii"))
                assert state["entered"].wait(5), "sync handler never started"
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()

            await asyncio.to_thread(disconnect_mid_request)
            # The Axum disconnect reaches the worker promptly: the
            # cancellation signal is observed while the thread is still
            # blocked, not only when it finishes.
            assert await asyncio.to_thread(state["observed"].wait, 5)
            assert not await app.wait_for_inflight(0)
            assert app._sync_active == 1
            state["release"].set()
            assert await asyncio.to_thread(state["finished"].wait, 5)
            assert await app.wait_for_inflight(2)
            assert app._sync_active == 0
            assert state["writes"] == []
            assert app.metrics.snapshot()["routes"]["/api/slow"]["status"] == {"499": 1}
            statuses = [record.status for record in caplog.records
                        if record.name == "pynext" and record.msg == "request"
                        and getattr(record, "route", None) == "/api/slow"]
            assert statuses == [499]
            # The worker is reusable once the cancelled thread exits.
            async with httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False) as client:
                assert (await client.post("/api/slow")).status_code == 200
            assert state["writes"] == ["committed"]
        finally:
            state["release"].set()
            await asyncio.to_thread(server.stop)
            await app.shutdown()

    asyncio.run(exercise())


def test_disconnect_during_sync_layout_leaves_shell_uncached(tmp_path):
    """Disconnecting mid-layout cancels the render without caching it (C-P1-8).

    A sync layout that is still computing when the client disconnects
    is cancelled like a sync handler: the layout shell lock is released
    and no half-built shell is cached, so the next request recomputes
    the shell from scratch and succeeds.
    """
    folder = tmp_path / "app" / "a"
    folder.mkdir(parents=True)
    (folder / "layout.py").write_text(
        'from pynext import layout, h\n'
        'import threading\n'
        'entered = threading.Event()\nrelease = threading.Event()\nfinished = threading.Event()\n'
        '@layout()\n'
        'def Layout(children):\n'
        '    entered.set()\n'
        '    try:\n'
        '        release.wait(10)\n'
        '        return h("div", {}, children)\n'
        '    finally:\n'
        '        finished.set()\n',
        encoding="utf-8",
    )
    (folder / "page.py").write_text(
        'from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "ok")\n',
        encoding="utf-8",
    )

    async def run():
        app = create_app(tmp_path, sync_workers=1)
        mod = app._module("app/a/layout.py")
        try:
            messages = asyncio.Queue()
            await messages.put({"type": "http.request", "body": b""})

            async def send(message):
                pytest.fail(f"Disconnected request emitted a response: {message}")

            task = asyncio.create_task(app({"type": "http", "method": "GET", "path": "/a",
                                           "headers": [], "scheme": "http"}, messages.get, send))
            assert await asyncio.to_thread(mod.entered.wait, 5)
            await messages.put({"type": "http.disconnect"})
            await asyncio.wait_for(task, 5)
            assert "/a" not in app._layout_shells
            assert app._sync_active == 1
            mod.release.set()
            assert await asyncio.to_thread(mod.finished.wait, 5)
            assert await app.wait_for_inflight(2)
            assert app._sync_active == 0
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/a")).status_code == 200
            assert "/a" in app._layout_shells
        finally:
            mod.release.set()
            await app.shutdown()

    asyncio.run(run())


def test_sync_layouts_share_the_sync_worker_limit(tmp_path):
    """Sync layouts run under the sync-worker limit, like sync handlers.

    With sync_workers=1, a second concurrent request whose layout shell
    is still being computed must be refused with 503, and the tracking
    counter must show the first layout occupying the slot. Before the
    shell cache threaded the request through, layouts ran on a bare
    worker thread: both executed at once and the counter stayed 0.

    sync_wait_timeout=0 pins the refuse-immediately mode: with the
    default bounded wait the second request would queue behind the
    first layout and succeed with 200 once the slot frees instead.
    """
    for route in ("a", "b"):
        folder = tmp_path / "app" / route
        folder.mkdir(parents=True)
        (folder / "layout.py").write_text(
            'from pynext import layout, h\n'
            'import threading\n'
            'entered = threading.Event()\nrelease = threading.Event()\n'
            '@layout()\n'
            'def Layout(children):\n'
            '    entered.set()\n'
            '    release.wait(10)\n'
            '    return h("div", {}, children)\n',
            encoding="utf-8",
        )
        (folder / "page.py").write_text(
            'from pynext import h, page\n'
            '@page()\ndef Page(): return h("p", {}, "ok")\n',
            encoding="utf-8",
        )

    async def run():
        app = create_app(tmp_path, sync_workers=1, sync_wait_timeout=0)
        mod_a = mod_b = None
        try:
            mod_a = app._module("app/a/layout.py")
            mod_b = app._module("app/b/layout.py")
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = asyncio.create_task(client.get("/a"))
                await asyncio.to_thread(mod_a.entered.wait, 5)
                assert mod_a.entered.is_set()
                assert app._sync_active == 1
                second = asyncio.create_task(client.get("/b"))
                await asyncio.sleep(0.5)
                assert not mod_b.entered.is_set()
                mod_a.release.set()
                mod_b.release.set()
                assert (await first).status_code == 200
                assert (await second).status_code == 503
        finally:
            for mod in (mod_a, mod_b):
                if mod is not None:
                    mod.release.set()
            await app.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("workers", [0, -1, True, 1.5, 1025])
def test_sync_capacity_is_validated(tmp_path, workers):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/page.py").write_text('from pynext import page, h\n@page()\ndef Page(): return h("p", {}, "ok")\n')
    with pytest.raises(ValueError, match="sync_workers"):
        create_app(tmp_path, sync_workers=workers)
