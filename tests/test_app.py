import asyncio
import json
from pathlib import Path

import httpx
import pytest

from pynext import _native, Response, create_app, h
from pynext.build import write_manifest
from pynext.server import start_native

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "basic"


async def exercise(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert response.text.startswith("<!DOCTYPE html><html")
    assert "Hello PyNext!" in response.text
    response = await client.get("/blog/%3Cscript%3E")
    assert response.status_code == 200
    assert "&lt;script&gt;" in response.text
    assert "<script>" not in response.text
    assert (await client.get("/api/users")).json() == {"users": ["Alice", "Bob"]}
    response = await client.post("/api/users", json={"name": "N"})
    assert response.status_code == 201 and response.json() == {"name": "N"}
    response = await client.delete("/api/users")
    assert response.status_code == 405 and "GET" in response.headers["allow"]
    assert (await client.get("/missing/path")).status_code == 404
    response = await client.get("/blog/hello/?q=1", follow_redirects=False)
    assert response.status_code == 308 and response.headers["location"] == "/blog/hello?q=1"
    response = await client.head("/")
    assert response.status_code == 200 and response.content == b""
    assert int(response.headers["content-length"]) > 0
    assert (await client.get("/blog/a%2Fb")).status_code == 400
    assert (await client.post("/api/users", content=b"x" * 1_048_577)).status_code == 413


def test_asgi_end_to_end():
    async def run():
        transport = httpx.ASGITransport(app=create_app(EXAMPLE))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await exercise(client)
    asyncio.run(run())


def test_asgi_lifespan_and_disconnected_request():
    async def run():
        app = create_app(EXAMPLE)
        messages = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
        sent = []
        async def receive():
            return next(messages)
        async def send(message):
            sent.append(message)
        await app({"type": "lifespan"}, receive, send)
        assert sent == [{"type": "lifespan.startup.complete"}, {"type": "lifespan.shutdown.complete"}]
        sent.clear()
        async def disconnected():
            return {"type": "http.disconnect"}
        await app({"type": "http", "method": "GET", "path": "/"}, disconnected, send)
        assert not sent
    asyncio.run(run())


def test_asgi_rejects_websocket_and_oversized_multichunk_body():
    async def run():
        app = create_app(EXAMPLE)
        sent = []

        async def send(message):
            sent.append(message)

        async def unused_receive():
            raise AssertionError("websocket scope should be rejected before receiving")

        with pytest.raises(RuntimeError, match="Unsupported ASGI scope: websocket"):
            await app({"type": "websocket"}, unused_receive, send)
        assert not sent
        messages = iter([
            {"type": "http.request", "body": b"x" * 600_000, "more_body": True},
            {"type": "http.request", "body": b"y" * 500_000, "more_body": False},
        ])

        async def receive():
            return next(messages)

        await app({"type": "http", "method": "POST", "path": "/api/users", "headers": []}, receive, send)
        assert sent[0]["status"] == 413
        assert sent[1]["body"] == b"Payload Too Large"
        sent.clear()
        messages = iter([
            {"type": "http.request", "body": b"partial", "more_body": True},
            {"type": "http.disconnect"},
        ])
        await app({"type": "http", "method": "POST", "path": "/api/users", "headers": []}, receive, send)
        assert not sent
        await app.isr.close()
    asyncio.run(run())


def test_axum_end_to_end():
    async def run():
        app = create_app(EXAMPLE)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5) as client:
                await exercise(client)
        finally:
            await asyncio.to_thread(server.stop)
    asyncio.run(run())


def test_renderer_escapes_and_rejects_dangerous_attributes():
    assert _native.render(json.dumps(h("p", {"title": '"<&'}, "<b>&"))) == '<p title="&quot;&lt;&amp;">&lt;b&gt;&amp;</p>'
    for tree in [h("a", {"href": "java\nscript:alert(1)"}, "x"), h("p", {"onclick": "evil()"}), h("script", {}, "x")]:
        with pytest.raises(ValueError):
            _native.render(json.dumps(tree))


def test_equivalent_module_paths_share_one_loaded_module(tmp_path):
    directory = tmp_path / "app"
    directory.mkdir()
    (directory / "page.py").write_text('from pynext import h, page\n@page()\ndef Page(): return h("p", {}, "ok")\n')
    app = create_app(tmp_path)
    assert app._module("app/page.py") is app._module("app/./page.py")
    assert app._module("app/page.py") is app._module(str(Path("app") / "page.py"))


def test_invalid_request_data_is_bad_request_and_headers_are_safe(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("GET")\n'
        'async def get(request): return JSONResponse(request.query)\n'
        '@route("POST")\n'
        'async def post(request): return JSONResponse(await request.json())\n'
    )
    (app / "bad").mkdir()
    (app / "bad" / "page.py").write_text(
        'from pynext import page\n'
        '@page()\n'
        'def Page(): return None\n'
    )

    async def run():
        application = create_app(tmp_path)
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/?x=%FF")).status_code == 400
            query = "&".join(f"x{i}=1" for i in range(1001))
            assert (await client.get("/?" + query)).status_code == 400
            assert (await client.post("/", content=b"{")).status_code == 400
            assert (await client.get("/bad")).status_code == 500
            response = await client.get("/")
            assert response.headers["content-security-policy"] == "script-src 'self'; object-src 'none'"
        await application.isr.close()

    asyncio.run(run())


def test_response_rejects_control_characters_in_media_type():
    with pytest.raises(ValueError, match="media type"):
        Response("ok", media_type="text/plain\r\nx-injected: yes")

    with pytest.raises(ValueError, match="response header"):
        Response("ok", headers=[(b"x-test", b"bad\x01value")])


def test_build_rejects_stale_sources(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    source = app / "page.py"
    source.write_text('from pynext import page, h\n@page()\ndef Page(): return h("p", {}, "one")\n')
    write_manifest(tmp_path)
    create_app(tmp_path, production=True)
    source.write_text(source.read_text().replace("one", "two"))
    with pytest.raises(ValueError, match="changed since build"):
        create_app(tmp_path, production=True)


def test_page_and_api_path_conflict_fails_build(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "page.py").write_text("")
    (app / "route.py").write_text("")
    with pytest.raises(ValueError, match="share a path"):
        write_manifest(tmp_path)
