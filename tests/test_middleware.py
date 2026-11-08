import asyncio

import httpx
import pytest

from pynext import Response, SecurityConfig, StreamingResponse, create_app
from pynext.build import write_manifest
from pynext.server import start_native
from pynext.watch import Watcher


def make_project(root):
    app = root / "app"
    app.mkdir()
    (app / "page.py").write_text(
        'from pynext import h, page\n@page(render="ssg")\ndef Page(): return h("p", {}, "static")\n'
    )
    (app / "news").mkdir()
    (app / "news" / "page.py").write_text(
        'from pynext import h, page\n@page(render="isr", revalidate=30)\n'
        'def Page(): return h("p", {}, "cached")\n'
    )
    api = app / "api"
    api.mkdir()
    (api / "route.py").write_text(
        'from pynext import JSONResponse, route\n@route("GET")\n'
        'def get(request): return JSONResponse({"order": request.state["order"]})\n'
    )
    (root / "public").mkdir()
    (root / "public" / "file.txt").write_text("asset")
    (root / "middleware.py").write_text(
        'from pynext import Response\n'
        'async def layer(request, call_next):\n'
        '    request.state["order"] = ["file"]\n'
        '    if request.path == "/blocked":\n'
        '        return Response("blocked", status=403)\n'
        '    response = await call_next(request)\n'
        '    response.headers.append((b"x-file", b"yes"))\n'
        '    return response\n'
        'MIDDLEWARE = [layer]\n'
    )
    write_manifest(root)


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_middleware_wraps_dynamic_static_cached_and_preflight(tmp_path, mode):
    make_project(tmp_path)

    async def programmatic(request, call_next):
        request.state["order"].append("api")
        response = await call_next(request)
        response.headers.append((b"x-api", b"yes"))
        return response

    app = create_app(tmp_path, production=True, middleware=[programmatic],
                     security=SecurityConfig(cors_allowed_origins=("https://example.com",)))

    async def run():
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                for path in ("/", "/file.txt", "/news", "/news", "/missing"):
                    response = await client.get(path)
                    assert response.headers["x-file"] == "yes"
                    assert response.headers["x-api"] == "yes"
                    assert response.status_code == (404 if path == "/missing" else 200)
                response = await client.get("/api")
                assert response.json() == {"order": ["file", "api"]}
                response = await client.head("/file.txt")
                assert response.content == b"" and response.headers["x-file"] == "yes"
                response = await client.get("/blocked")
                assert response.status_code == 403 and "x-api" not in response.headers
                response = await client.options("/api", headers={
                    "origin": "https://example.com", "access-control-request-method": "GET"
                })
                assert response.status_code == 204
                assert response.headers["access-control-allow-origin"] == "https://example.com"
                assert response.headers["x-file"] == "yes" and response.headers["x-api"] == "yes"
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_middleware_source_is_watched_and_invalidates_build(tmp_path):
    make_project(tmp_path)
    watcher = Watcher(tmp_path)
    middleware = tmp_path / "middleware.py"
    middleware.write_text(middleware.read_text() + "\n# changed\n")
    assert "middleware.py" in watcher.changed(watcher._baseline, watcher.snapshot())
    with pytest.raises(ValueError, match="sources changed"):
        create_app(tmp_path, production=True)


def test_middleware_contract_rejects_sync_and_repeated_next(tmp_path):
    make_project(tmp_path)
    with pytest.raises(TypeError, match="async callables"):
        create_app(tmp_path, middleware=[lambda request, next: Response()])

    async def twice(request, call_next):
        await call_next(request)
        return await call_next(request)

    app = create_app(tmp_path, middleware=[twice])

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/")
            assert response.status_code == 500
        await app.isr.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_middleware_streaming_short_circuit_and_head_close(tmp_path, mode):
    make_project(tmp_path)
    closed = []

    class Chunks:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if closed:
                raise StopAsyncIteration
            closed.append("sent")
            return b"streamed"

        async def aclose(self):
            closed.append("closed")

    async def streaming(request, call_next):
        return StreamingResponse(Chunks(), media_type="text/plain")

    app = create_app(tmp_path, middleware=[streaming])

    async def run():
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", trust_env=False)
        try:
            async with client:
                assert (await client.head("/")).content == b""
                assert closed == ["closed"]
                closed.clear()
                assert (await client.get("/")).content == b"streamed"
                assert closed == ["sent", "closed"]
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())
