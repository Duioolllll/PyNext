import asyncio
import json
from pathlib import Path

import httpx
import pytest

from pynext import _native, create_app, h
from pynext.build import write_manifest
from pynext.server import start_native

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "basic"


async def exercise(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == "script-src 'self'; object-src 'none'"
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


def test_page_returning_plain_value_is_server_error(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "page.py").write_text(
        'from pynext import page\n'
        '@page()\n'
        'def Page(): return "plain text"\n'
    )
    async def run():
        app = create_app(tmp_path)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/")).status_code == 500
    asyncio.run(run())


def test_renderer_escapes_and_rejects_dangerous_attributes():
    assert _native.render(json.dumps(h("p", {"title": '"<&'}, "<b>&"))) == '<p title="&quot;&lt;&amp;">&lt;b&gt;&amp;</p>'
    for tree in [h("a", {"href": "java\nscript:alert(1)"}, "x"), h("p", {"onclick": "evil()"}), h("script", {}, "x")]:
        with pytest.raises(ValueError):
            _native.render(json.dumps(tree))


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
