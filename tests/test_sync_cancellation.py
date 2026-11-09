"""Timed-out synchronous handlers remain bounded and observe cancellation."""
import asyncio
import logging
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
        app = create_app(tmp_path, sync_workers=1)
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


@pytest.mark.parametrize("workers", [0, -1, True, 1.5, 1025])
def test_sync_capacity_is_validated(tmp_path, workers):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/page.py").write_text('from pynext import page, h\n@page()\ndef Page(): return h("p", {}, "ok")\n')
    with pytest.raises(ValueError, match="sync_workers"):
        create_app(tmp_path, sync_workers=workers)
