"""S-P1-7: parser/writer two-way CTL validation.

The request parser (Rust ``dispatch`` + hyper) and the response writer
(Rust bridge loop + ASGI ``_send_response``) must apply the same CTL
rule (aiohttp ``_safe_header``: values reject ``0x00-0x08``,
``0x0A-0x1F``, ``0x7F``; names reject any byte below ``0x20`` or
``0x7F``), on both the Rust fast path and the Python ASGI path:

- request header with a CTL -> 400;
- app-set response header with a CTL -> rejected (native bridge: 500)
  or stripped (ASGI send), never serialized to the wire;
- clean headers pass through unchanged on both paths.
"""
import asyncio
import socket

import httpx
import pytest

from pynext import Response, create_app
from pynext.server import start_native

KINDS = ("ctl", "nul", "del", "crlf", "name")


def make_app(root):
    directory = root / "app" / "api" / "ctl"
    directory.mkdir(parents=True)
    (directory / "route.py").write_text(
        'from pynext import Response, route\n'
        'KINDS = {\n'
        '    "ctl": (b"x-evil", b"a\\x01b"),\n'
        '    "nul": (b"x-evil", b"a\\x00b"),\n'
        '    "del": (b"x-evil", b"a\\x7fb"),\n'
        '    "crlf": (b"x-evil", b"a\\r\\nInjected: 1"),\n'
        '    "name": (b"x-\\x01vil", b"a"),\n'
        '}\n'
        '@route("GET")\n'
        'def get(request):\n'
        '    kind = (request.query.get("kind") or ["ok"])[0]\n'
        '    response = Response("ok", headers=[(b"x-normal", b"fine")])\n'
        '    if kind in KINDS:\n'
        '        name, value = KINDS[kind]\n'
        '        response.headers.append((name, value))\n'
        '    return response\n'
    )
    stream_dir = root / "app" / "api" / "stream"
    stream_dir.mkdir(parents=True)
    (stream_dir / "route.py").write_text(
        'from pynext import StreamingResponse, route\n'
        '@route("GET")\n'
        'def get(request):\n'
        '    async def gen():\n'
        '        yield b"chunk1"\n'
        '        yield b"chunk2"\n'
        '    response = StreamingResponse(gen(), headers=[(b"x-normal", b"fine")])\n'
        '    response.headers.append((b"x-evil", b"a\\x01b"))\n'
        '    return response\n'
    )
    return create_app(root)


def test_response_constructor_rejects_ctl_headers():
    # Locks in the existing constructor validation: a handler cannot
    # construct a Response carrying a CTL header in the first place.
    for headers in ([(b"x-a", b"a\nb")], [(b"x-a", b"a\rb")],
                    [(b"x-a", b"a\x00b")], [(b"x-a", b"a\x7fb")],
                    [(b"x-\x01a", b"a")], [(b"bad name", b"a")]):
        with pytest.raises(ValueError, match="Invalid response header"):
            Response("ok", headers=headers)


@pytest.mark.parametrize("kind", KINDS)
def test_asgi_strips_ctl_response_header(tmp_path, kind):
    async def run():
        app = make_app(tmp_path)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://test")
        async with client:
            response = await client.get(f"/api/ctl?kind={kind}")
            assert response.status_code == 200
            assert response.text == "ok"
            assert "x-evil" not in response.headers
            assert "x-\x01vil" not in response.headers
            assert response.headers["x-normal"] == "fine"
            assert b"Injected" not in response.content
    asyncio.run(run())


def test_asgi_strips_ctl_header_from_raw_send_messages(tmp_path):
    # Byte-level check: no CTL byte reaches the ASGI send at all.
    async def run():
        app = make_app(tmp_path)
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await app({"type": "http", "method": "GET", "path": "/api/ctl",
                   "query_string": b"kind=crlf",
                   "headers": [(b"host", b"test")]}, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        assert start["status"] == 200
        for name, value in start["headers"]:
            assert not any(b < 0x20 or b == 0x7F for b in name)
            assert not any((b < 0x20 and b != 0x09) or b == 0x7F for b in value)
        assert not any(name == b"x-evil" for name, _ in start["headers"])
    asyncio.run(run())


def test_asgi_strips_ctl_header_from_streaming_response(tmp_path):
    async def run():
        app = make_app(tmp_path)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                   base_url="http://test")
        async with client:
            response = await client.get("/api/stream")
            assert response.status_code == 200
            assert response.text == "chunk1chunk2"
            assert "x-evil" not in response.headers
            assert response.headers["x-normal"] == "fine"
    asyncio.run(run())


@pytest.mark.parametrize("kind", KINDS)
def test_native_rejects_ctl_response_header(tmp_path, kind):
    async def run():
        app = make_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}",
                                         timeout=10) as client:
                response = await client.get(f"/api/ctl?kind={kind}")
                assert response.status_code == 500
                assert "x-evil" not in response.headers
                assert "x-\x01vil" not in response.headers
                assert b"Injected" not in response.content
        finally:
            await asyncio.to_thread(server.stop)
    asyncio.run(run())


def test_native_response_bytes_carry_no_ctl(tmp_path):
    # Raw socket: the rejected response must not leak the CTL byte or
    # the injected header line onto the wire.
    async def run():
        app = make_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            host, port = server.address.rsplit(":", 1)
            raw = await asyncio.to_thread(
                _raw_request, host, int(port),
                b"GET /api/ctl?kind=crlf HTTP/1.1\r\n"
                b"Host: x\r\nConnection: close\r\n\r\n")
            assert raw.startswith(b"HTTP/1.1 500")
            assert b"\x01" not in raw and b"\x00" not in raw
            assert b"Injected" not in raw
        finally:
            await asyncio.to_thread(server.stop)
    asyncio.run(run())


def _raw_request(host, port, request_bytes):
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall(request_bytes)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return b"".join(chunks)


@pytest.mark.parametrize("header_line", [
    b"X-Test: a\x01b\r\n",      # CTL in value
    b"X-Test: a\x00b\r\n",      # NUL in value
    b"X-Test: a\x7fb\r\n",      # DEL in value
    b"X-Test: a\rb\r\n",        # bare CR in value
    b"X-\x01Test: a\r\n",       # CTL in name
    b"X-Test: a\x1fb\r\n",      # CTL 0x1F in value
])
def test_native_parser_rejects_ctl_request_header(tmp_path, header_line):
    async def run():
        app = make_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            host, port = server.address.rsplit(":", 1)
            raw = await asyncio.to_thread(
                _raw_request, host, int(port),
                b"GET /api/ctl?kind=ok HTTP/1.1\r\n"
                b"Host: x\r\nConnection: close\r\n" + header_line + b"\r\n")
            assert raw.startswith(b"HTTP/1.1 400"), raw[:200]
        finally:
            await asyncio.to_thread(server.stop)
    asyncio.run(run())


def test_native_parser_accepts_clean_headers(tmp_path):
    async def run():
        app = make_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            async with httpx.AsyncClient(base_url=f"http://{server.address}",
                                         timeout=10) as client:
                response = await client.get(
                    "/api/ctl?kind=ok",
                    headers={"X-Test": "a\tb c~d", "X-Normal": "fine"})
                assert response.status_code == 200
                assert response.text == "ok"
                assert response.headers["x-normal"] == "fine"
        finally:
            await asyncio.to_thread(server.stop)
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["asgi", "axum"])
def test_clean_headers_pass_through_both_paths(tmp_path, mode):
    async def run():
        app = make_app(tmp_path)
        server = None
        if mode == "asgi":
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                       base_url="http://test")
        else:
            server = start_native(app, asyncio.get_running_loop(), port=0)
            client = httpx.AsyncClient(base_url=f"http://{server.address}", timeout=10)
        try:
            async with client:
                response = await client.get("/api/ctl?kind=ok")
                assert response.status_code == 200
                assert response.text == "ok"
                assert response.headers["x-normal"] == "fine"
                assert "content-security-policy" in response.headers
        finally:
            if server is not None:
                await asyncio.to_thread(server.stop)
    asyncio.run(run())
