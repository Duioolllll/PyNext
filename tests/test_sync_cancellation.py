"""Timed-out synchronous handlers remain bounded and observe cancellation."""
import asyncio
import threading

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


@pytest.mark.parametrize("transport", ["native-timeout", "asgi-disconnect"])
def test_cancelled_sync_work_keeps_capacity_until_thread_finishes(tmp_path, monkeypatch, transport):
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
