import asyncio
import hashlib

import httpx
import pytest

from pynext import create_app
from pynext.build import write_manifest
from pynext.server import start_native


def make_project(root):
    app = root / "app"
    app.mkdir()
    (app / "page.py").write_text(
        'from pynext import h, page\n'
        '@page(render="ssg")\ndef Page(): return h("p", {}, "rendered")\n', encoding="utf-8"
    )
    public = root / "public"
    public.mkdir()
    (public / "data.bin").write_bytes(b"0123456789")
    (public / "empty.bin").write_bytes(b"")
    write_manifest(root)
    return create_app(root, production=True)


async def exercise(client):
    expected = '"' + hashlib.sha256(b"0123456789").hexdigest() + '"'
    response = await client.get("/data.bin")
    assert response.status_code == 200 and response.content == b"0123456789"
    assert response.headers["etag"] == expected
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["cache-control"] == "public, max-age=0, must-revalidate"

    response = await client.get("/data.bin", headers={"if-none-match": '"other,tag", W/' + expected})
    assert response.status_code == 304 and response.content == b""
    assert response.headers["etag"] == expected and "content-length" not in response.headers
    assert (await client.get("/data.bin", headers={"if-match": 'W/' + expected})).status_code == 412
    assert (await client.get("/data.bin", headers={"if-match": expected})).status_code == 200
    assert (await client.get("/data.bin", headers={"if-match": '"other"', "if-none-match": expected})).status_code == 412
    assert (await client.get("/data.bin", headers={"if-none-match": '"unterminated, ' + expected})).status_code == 200
    assert (await client.get("/data.bin", headers={"if-none-match": "*", "range": "bytes=0-1"})).status_code == 304

    for header, wanted, content_range in [
        ("bytes=2-5", b"2345", "bytes 2-5/10"),
        ("bytes=7-", b"789", "bytes 7-9/10"),
        ("bytes=-3", b"789", "bytes 7-9/10"),
        ("bytes=-999999999999999999999999999", b"0123456789", "bytes 0-9/10"),
    ]:
        response = await client.get("/data.bin", headers={"range": header})
        assert response.status_code == 206 and response.content == wanted
        assert response.headers["content-range"] == content_range
        assert int(response.headers["content-length"]) == len(wanted)
        assert response.headers["etag"] == expected
    for header in ("bytes=10-", "bytes=-0", "bytes=999999999999999999999999-"):
        response = await client.get("/data.bin", headers={"range": header})
        assert response.status_code == 416 and response.content == b""
        assert response.headers["content-range"] == "bytes */10"
    response = await client.get("/empty.bin", headers={"range": "bytes=0-"})
    assert response.status_code == 416 and response.headers["content-range"] == "bytes */0"
    for header in ("bytes=5-2", "bytes=0-1,3-4", "items=0-1", "bytes=nope"):
        response = await client.get("/data.bin", headers={"range": header})
        assert response.status_code == 200 and response.content == b"0123456789"
    response = await client.get("/data.bin", headers={"range": "bytes=1-2", "if-range": expected})
    assert response.status_code == 206 and response.content == b"12"
    for validator in ('W/' + expected, '"different"', 'Wed, 21 Oct 2015 07:28:00 GMT'):
        response = await client.get("/data.bin", headers={"range": "bytes=1-2", "if-range": validator})
        assert response.status_code == 200 and response.content == b"0123456789"
    response = await client.head("/data.bin", headers={"range": "bytes=0-2"})
    assert response.status_code == 200 and response.content == b""
    assert response.headers["content-length"] == "10"
    response = await client.head("/data.bin", headers={"if-none-match": expected})
    assert response.status_code == 304 and response.content == b""

    response = await client.get("/")
    assert response.status_code == 200 and response.text == "<p>rendered</p>"
    assert response.headers["cache-control"] == "public, max-age=0, must-revalidate"
    assert response.headers["vary"] == "Cookie, Authorization"
    page_etag = response.headers["etag"]
    assert (await client.get("/", headers={"if-none-match": page_etag})).status_code == 304
    response = await client.get("/", headers={"range": "bytes=0-2"})
    assert response.status_code == 206 and response.content == b"<p>"


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_static_conditional_and_single_range_requests(tmp_path, mode):
    app = make_project(tmp_path)

    async def run():
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10)
        try:
            async with client:
                await exercise(client)
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_client_island_asset_supports_conditional_and_range_requests(tmp_path, esbuild):
    app_dir, islands = tmp_path / "app", tmp_path / "islands"
    app_dir.mkdir()
    islands.mkdir()
    (app_dir / "page.py").write_text(
        'from pynext import island, page\n'
        '@page()\ndef Page(): return island("counter", props={})\n', encoding="utf-8"
    )
    (islands / "counter.js").write_text('export function mount() {}\n', encoding="utf-8")
    app = create_app(tmp_path)
    path, body = next(iter(app.client_assets.items()))
    expected = '"' + hashlib.sha256(body).hexdigest() + '"'

    async def run():
        server = start_native(app, asyncio.get_running_loop(), port=0)
        clients = [httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"),
                   httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10)]
        try:
            for client in clients:
                async with client:
                    response = await client.get(path, headers={"range": "bytes=0-2"})
                    assert response.status_code == 206 and response.content == body[:3]
                    assert response.headers["etag"] == expected
                    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
                    assert (await client.get(path, headers={"if-none-match": expected})).status_code == 304
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())
