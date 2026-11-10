import asyncio
import threading

import httpx
import pytest

from pynext import JSONResponse, create_app
from pynext.server import start_native


def make_app(root):
    directory = root / "app" / "api" / "input"
    directory.mkdir(parents=True)
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
                assert response.status_code == 500
                # Production 500 body: correlation id + digest, never the
                # exception message.
                assert response.text.startswith("Internal Server Error (request_id: ")
                assert ", digest: " in response.text
                assert "internal details" not in response.text
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
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
