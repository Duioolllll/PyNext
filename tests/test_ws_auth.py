"""WebSocket authorization: the global middleware chain and @require_auth
apply to upgrades, before the handshake completes, on ASGI and Axum.

Regression coverage for review #4 finding #1: a middleware returning 403
for /ws plus a @require_auth handler used to let the upgrade through with
HTTP 101 and leak secret-data. Both transports must now reject the
upgrade instead (ASGI: websocket.close 1008 without accept; Axum: plain
HTTP 403, no 101).
"""
import asyncio
import base64
import os
import secrets
import socket
import struct
import time
from pathlib import Path

import httpx
import pytest

from pynext import Response, SessionMiddleware, SQLiteSessionStore, create_app
from pynext.server import start_native

SAME_ORIGIN_HEADERS = [(b"host", b"localhost"), (b"origin", b"http://localhost")]

SECRET_SOURCE = (
    "from pynext import require_auth, websocket\n"
    "@websocket\n"
    "@require_auth\n"
    "async def handler(websocket):\n"
    "    await websocket.accept()\n"
    "    await websocket.send_text('secret-data')\n"
    "    await websocket.receive()\n"
)

PUBLIC_SOURCE = (
    "from pynext import websocket\n"
    "@websocket\n"
    "async def handler(websocket):\n"
    "    await websocket.accept()\n"
    "    await websocket.send_text('public-data')\n"
    "    await websocket.receive()\n"
)


class _ScriptedConnection:
    def __init__(self, incoming):
        self._incoming = iter(incoming)
        self.sent = []

    async def receive(self):
        return next(self._incoming)

    async def send(self, message):
        self.sent.append(message)


def _run(coro):
    asyncio.run(coro)


def _project(tmp_path, source):
    ws_dir = tmp_path / "app" / "ws"
    ws_dir.mkdir(parents=True)
    (ws_dir / "route.py").write_text(source)
    return tmp_path


async def _block_ws(request, call_next):
    if request.path == "/ws":
        return Response("blocked", status=403)
    return await call_next(request)


async def _pass_through(request, call_next):
    request.state["seen_by_middleware"] = True
    return await call_next(request)


async def _short_circuit_200(request, call_next):
    if request.path == "/ws":
        return Response("handled here", status=200)
    return await call_next(request)


async def _exploding(request, call_next):
    if request.path == "/ws":
        raise RuntimeError("boom")
    return await call_next(request)


def _ws_scope(headers):
    return {"type": "websocket", "path": "/ws", "headers": headers, "query_string": b""}


def _cookie_headers(token):
    headers = list(SAME_ORIGIN_HEADERS)
    if token is not None:
        headers.append((b"cookie", f"pynext_session={token}".encode("ascii")))
    return headers


async def _save_session(store):
    token = secrets.token_urlsafe(32)
    await store.save(token, {"user_id": "alice"}, None, None, time.time() + 3600)
    return token


def test_asgi_blocking_middleware_rejects_ws_upgrade(tmp_path):
    """Exact review reproduction: 403 middleware + @require_auth handler."""
    _project(tmp_path, SECRET_SOURCE)
    app = create_app(tmp_path, middleware=[_block_ws])

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            assert (await client.get("/ws")).status_code == 403
        conn = _ScriptedConnection([{"type": "websocket.connect"}])
        await app(_ws_scope(SAME_ORIGIN_HEADERS), conn.receive, conn.send)
        assert conn.sent == [{"type": "websocket.close", "code": 1008}]
        await app.isr.close()

    _run(run())


def test_asgi_require_auth_rejects_without_session(tmp_path):
    _project(tmp_path, SECRET_SOURCE)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = create_app(tmp_path, middleware=[SessionMiddleware(store, secure=False),
                                           _pass_through])

    async def run():
        conn = _ScriptedConnection([{"type": "websocket.connect"}])
        await app(_ws_scope(_cookie_headers(None)), conn.receive, conn.send)
        assert conn.sent == [{"type": "websocket.close", "code": 1008}]
        await app.isr.close()

    _run(run())


def test_asgi_require_auth_allows_with_session(tmp_path):
    """A valid session cookie passes: SessionMiddleware runs for WS."""
    _project(tmp_path, SECRET_SOURCE)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = create_app(tmp_path, middleware=[SessionMiddleware(store, secure=False),
                                           _pass_through])

    async def run():
        token = await _save_session(store)
        conn = _ScriptedConnection([
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": "x"},
            {"type": "websocket.disconnect", "code": 1000},
        ])
        await app(_ws_scope(_cookie_headers(token)), conn.receive, conn.send)
        assert conn.sent == [{"type": "websocket.accept"},
                             {"type": "websocket.send", "text": "secret-data"},
                             {"type": "websocket.close", "code": 1000}]
        await app.isr.close()

    _run(run())


def test_asgi_public_handler_still_works_with_passing_middleware(tmp_path):
    _project(tmp_path, PUBLIC_SOURCE)
    app = create_app(tmp_path, middleware=[_pass_through])

    async def run():
        conn = _ScriptedConnection([
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": "x"},
            {"type": "websocket.disconnect", "code": 1000},
        ])
        await app(_ws_scope(SAME_ORIGIN_HEADERS), conn.receive, conn.send)
        assert conn.sent == [{"type": "websocket.accept"},
                             {"type": "websocket.send", "text": "public-data"},
                             {"type": "websocket.close", "code": 1000}]
        await app.isr.close()

    _run(run())


def test_asgi_no_middleware_public_handler_is_unaffected(tmp_path):
    _project(tmp_path, PUBLIC_SOURCE)
    app = create_app(tmp_path)

    async def run():
        conn = _ScriptedConnection([
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": "x"},
            {"type": "websocket.disconnect", "code": 1000},
        ])
        await app(_ws_scope(SAME_ORIGIN_HEADERS), conn.receive, conn.send)
        assert conn.sent[0] == {"type": "websocket.accept"}
        assert conn.sent[1] == {"type": "websocket.send", "text": "public-data"}
        await app.isr.close()

    _run(run())


def test_asgi_middleware_short_circuit_200_still_denies_upgrade(tmp_path):
    """A middleware answering the upgrade itself denies it, even with 200:
    the upgrade is allowed only when the chain reaches the handler."""
    _project(tmp_path, PUBLIC_SOURCE)
    app = create_app(tmp_path, middleware=[_short_circuit_200])

    async def run():
        conn = _ScriptedConnection([{"type": "websocket.connect"}])
        await app(_ws_scope(SAME_ORIGIN_HEADERS), conn.receive, conn.send)
        assert conn.sent == [{"type": "websocket.close", "code": 1008}]
        await app.isr.close()

    _run(run())


def test_asgi_failing_middleware_fails_closed(tmp_path):
    _project(tmp_path, PUBLIC_SOURCE)
    app = create_app(tmp_path, middleware=[_exploding])

    async def run():
        conn = _ScriptedConnection([{"type": "websocket.connect"}])
        await app(_ws_scope(SAME_ORIGIN_HEADERS), conn.receive, conn.send)
        assert conn.sent == [{"type": "websocket.close", "code": 1008}]
        await app.isr.close()

    _run(run())


class _RawHandshake:
    """Minimal raw-socket WebSocket handshake that reports the status line
    instead of asserting 101, so denials (403/503) are observable."""

    def __init__(self, address, path, extra_headers=""):
        host, port = address.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=5)
        self.sock.settimeout(5)
        self.key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {address}\r\nOrigin: http://{address}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {self.key}\r\nSec-WebSocket-Version: 13\r\n"
            f"{extra_headers}\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise AssertionError("handshake closed early")
            response += chunk
        head, _, self._pending = response.partition(b"\r\n\r\n")
        self.status_line = head.split(b"\r\n")[0].decode("ascii")

    def _read_exact(self, count):
        data, self._pending = self._pending[:count], self._pending[count:]
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise AssertionError("connection closed while reading a frame")
            data += chunk
        return data

    def recv(self):
        first, second = self._read_exact(2)
        fin = first & 0x80
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        if second & 0x80:
            mask = self._read_exact(4)
            chunk = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read_exact(length)))
        else:
            chunk = self._read_exact(length)
        assert fin
        return opcode, chunk

    def shutdown(self):
        try:
            self.sock.close()
        except OSError:
            pass


def test_axum_blocking_middleware_rejects_ws_upgrade(tmp_path):
    """Native-path review reproduction: no 101, no secret-data."""
    _project(tmp_path, SECRET_SOURCE)
    app = create_app(tmp_path, middleware=[_block_ws])

    async def run():
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            def check():
                client = _RawHandshake(server.address, "/ws")
                try:
                    assert client.status_line == "HTTP/1.1 403 Forbidden"
                finally:
                    client.shutdown()
            await asyncio.to_thread(check)
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    _run(run())


def test_axum_require_auth_rejects_without_session(tmp_path):
    _project(tmp_path, SECRET_SOURCE)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = create_app(tmp_path, middleware=[SessionMiddleware(store, secure=False)])

    async def run():
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            def check():
                client = _RawHandshake(server.address, "/ws")
                try:
                    assert client.status_line == "HTTP/1.1 403 Forbidden"
                finally:
                    client.shutdown()
            await asyncio.to_thread(check)
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    _run(run())


def test_axum_require_auth_allows_with_session(tmp_path):
    """Native positive case: authorized upgrade still gets 101 + secret."""
    _project(tmp_path, SECRET_SOURCE)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = create_app(tmp_path, middleware=[SessionMiddleware(store, secure=False)])

    async def run():
        token = await _save_session(store)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            def check():
                client = _RawHandshake(server.address, "/ws",
                                       f"Cookie: pynext_session={token}\r\n")
                try:
                    assert client.status_line == "HTTP/1.1 101 Switching Protocols"
                    assert client.recv() == (1, b"secret-data")
                finally:
                    client.shutdown()
            await asyncio.to_thread(check)
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    _run(run())


def test_axum_public_handler_still_works(tmp_path):
    _project(tmp_path, PUBLIC_SOURCE)
    app = create_app(tmp_path, middleware=[_pass_through])

    async def run():
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            def check():
                client = _RawHandshake(server.address, "/ws")
                try:
                    assert client.status_line == "HTTP/1.1 101 Switching Protocols"
                    assert client.recv() == (1, b"public-data")
                finally:
                    client.shutdown()
            await asyncio.to_thread(check)
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    _run(run())


def test_axum_no_middleware_public_handler_is_unaffected(tmp_path):
    _project(tmp_path, PUBLIC_SOURCE)
    app = create_app(tmp_path)

    async def run():
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            def check():
                client = _RawHandshake(server.address, "/ws")
                try:
                    assert client.status_line == "HTTP/1.1 101 Switching Protocols"
                    assert client.recv() == (1, b"public-data")
                finally:
                    client.shutdown()
            await asyncio.to_thread(check)
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    _run(run())
