import asyncio
import threading

import httpx
import pytest

from pynext import JSONResponse, create_app
from pynext.server import start_native


def make_app(root):
    directory = root / "app" / "api" / "input"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "route.py").write_text(
        'from pynext import BadRequest, JSONResponse, route\n'
        '@route("GET")\n'
        'def get(request): return JSONResponse(request.query)\n'
        '@route("POST")\n'
        'async def post(request): return JSONResponse(await request.json())\n'
        '@route("PUT")\n'
        'def put(): raise BadRequest("private validation details")\n'
        '@route("DELETE")\n'
        'def delete(): raise ValueError("internal details")\n'
    )
    return create_app(root)


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_request_parsing_errors_and_unexpected_errors(tmp_path, mode):
    async def run():
        app = make_app(tmp_path)
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10)
        try:
            async with client:
                response = await client.get("/api/input?name=%ED%95%9C%EA%B8%80&name=two&empty=")
                assert response.status_code == 200
                assert response.json() == {"name": ["한글", "two"], "empty": [""]}
                for query in ("name=%FF", "%FF=value", "name=%E2%82"):
                    response = await client.get(f"/api/input?{query}")
                    assert response.status_code == 400 and response.text == "Bad Request"
                response = await client.head("/api/input?name=%FF")
                assert response.status_code == 400 and response.content == b""

                response = await client.post("/api/input", json={"name": "한글"})
                assert response.status_code == 200 and response.json() == {"name": "한글"}
                for body in (b'{"name":', b"", b"\xff", b"NaN", b"Infinity", b"-Infinity"):
                    response = await client.post("/api/input", content=body)
                    assert response.status_code == 400 and response.text == "Bad Request"
                response = await client.put("/api/input")
                assert response.status_code == 400 and response.text == "Bad Request"
                response = await client.delete("/api/input")
                assert response.status_code == 500 and response.text == "Internal Server Error"
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_query_field_limit_is_bad_request(tmp_path, mode):
    async def run():
        app = make_app(tmp_path)
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10)
        try:
            async with client:
                query = "&".join(f"f{i}=1" for i in range(1001))
                response = await client.get(f"/api/input?{query}")
                assert response.status_code == 400 and response.text == "Bad Request"
                query = "&".join(f"f{i}=1" for i in range(1000))
                response = await client.get(f"/api/input?{query}")
                assert response.status_code == 200
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
    asyncio.run(run())


def test_axum_concurrency_slot_waits_before_rejecting(tmp_path):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow_handler(request):
        started.set()
        if not release.wait(10):
            raise RuntimeError("test did not release handler")
        finished.set()
        return JSONResponse({"ok": True})

    async def run_case(timeout_ms, expect_status, release_after=None):
        started.clear()
        release.clear()
        finished.clear()
        app = make_app(tmp_path)
        app.entries["/api/input"]["POST"] = slow_handler
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              max_concurrent=1, acquire_timeout_ms=timeout_ms)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
                task = asyncio.create_task(client.post("/api/input"))
                assert await asyncio.to_thread(started.wait, 5)
                async def release_later():
                    await asyncio.sleep(release_after)
                    release.set()

                releaser = asyncio.create_task(release_later()) if release_after is not None else None
                response = await client.post("/api/input")
                if releaser is not None:
                    await releaser
                assert response.status_code == expect_status
                if expect_status == 503:
                    assert response.headers["retry-after"] == "1"
                release.set()
                first = await task
                assert first.status_code == 200
        finally:
            release.set()
            await asyncio.to_thread(server.stop)

    async def run():
        await run_case(100, 503)
        await run_case(5000, 200, release_after=0.3)

    asyncio.run(run())


def test_asgi_rejects_raw_non_utf8_query_bytes(tmp_path):
    async def run():
        app = make_app(tmp_path)
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await app({"type": "http", "method": "GET", "path": "/api/input",
                   "raw_path": b"/api/input", "query_string": b"name=\xff", "headers": []}, receive, send)
        assert sent[0]["status"] == 400
        assert sent[1]["body"] == b"Bad Request"
    asyncio.run(run())


def test_axum_sync_handler_can_continue_after_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr("pynext.server.REQUEST_TIMEOUT", 1)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    effects = []

    def slow_handler(request):
        started.set()
        if not release.wait(10):
            raise RuntimeError("test did not release handler")
        effects.append("write completed")
        finished.set()
        return JSONResponse({"ok": True})

    async def run():
        app = make_app(tmp_path)
        app.entries["/api/input"]["POST"] = slow_handler
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10) as client:
                task = asyncio.create_task(client.post("/api/input"))
                assert await asyncio.to_thread(started.wait, 5)
                response = await task
                assert response.status_code == 504 and response.text == "Gateway Timeout"
                assert not finished.is_set() and not effects
                assert (await client.get("/api/input?x=ok")).json() == {"x": ["ok"]}
                release.set()
                assert await asyncio.to_thread(finished.wait, 5)
                assert effects == ["write completed"]
        finally:
            release.set()
            if started.is_set():
                await asyncio.to_thread(finished.wait, 5)
            await asyncio.to_thread(server.stop)
    asyncio.run(run())
