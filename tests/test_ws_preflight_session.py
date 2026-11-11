"""WebSocket pre-flight session handling (S-P1-24 / M-15).

The upgrade pre-flight (``_authorize_websocket``) runs the middleware
chain on a synthetic GET whose response is discarded. The pre-flight is
therefore read-only for the session: a rotation, invalidation or any
other mutation made by middleware during the pre-flight must never be
persisted — otherwise the old token is already gone from the store
while the rotated token's ``Set-Cookie`` never reaches the client, and
the session is lost. These tests pin that behaviour on both transports
(ASGI and Axum): the handshake still succeeds, and the original token
keeps working afterwards with its original data.
"""
import asyncio
import base64
import os
import secrets
import socket
import struct
import time
from pathlib import Path

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


def _ws_scope(headers):
    return {"type": "websocket", "path": "/ws", "headers": headers, "query_string": b""}


def _cookie_headers(token):
    headers = list(SAME_ORIGIN_HEADERS)
    if token is not None:
        headers.append((b"cookie", f"pynext_session={token}".encode("ascii")))
    return headers


async def _save_session(store, data=None):
    token = secrets.token_urlsafe(32)
    await store.save(token, data if data is not None else {"user_id": "alice"},
                     None, None, time.time() + 3600)
    return token


async def _rotating_middleware(request, call_next):
    # Simulates a privilege-change hook that rotates the session id on
    # any request carrying a session. During a WS pre-flight this must
    # be a no-op for the store: the pre-flight response (and its
    # Set-Cookie) is discarded, so persisting the rotation would delete
    # the client's token without ever delivering its replacement.
    session = request.state.get("session")
    if session is not None and session.get("user_id"):
        session.rotate()
    return await call_next(request)


async def _mutating_middleware(request, call_next):
    # Simulates last-seen tracking / CSRF token issuance: writes data
    # and mints a CSRF token. During a pre-flight neither write may
    # reach the store.
    session = request.state.get("session")
    if session is not None and session.get("user_id"):
        session["last_seen"] = 12345
        session.csrf_token()
    return await call_next(request)


async def _invalidating_middleware(request, call_next):
    # Simulates a suspicious-session hook that logs the user out. During
    # a pre-flight the invalidation must not delete the live session.
    session = request.state.get("session")
    if session is not None and session.get("user_id"):
        session.invalidate()
    return await call_next(request)


def _ws_messages():
    return [
        {"type": "websocket.connect"},
        {"type": "websocket.receive", "text": "x"},
        {"type": "websocket.disconnect", "code": 1000},
    ]


def test_asgi_preflight_rotation_does_not_destroy_session(tmp_path):
    """A rotation during the pre-flight must not delete the client's token:
    the handshake succeeds and the same token still loads afterwards."""
    _project(tmp_path, SECRET_SOURCE)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = create_app(tmp_path, middleware=[SessionMiddleware(store, secure=False),
                                           _rotating_middleware])

    async def run():
        token = await _save_session(store)
        conn = _ScriptedConnection(_ws_messages())
        await app(_ws_scope(_cookie_headers(token)), conn.receive, conn.send)
        assert conn.sent == [{"type": "websocket.accept"},
                             {"type": "websocket.send", "text": "secret-data"},
                             {"type": "websocket.close", "code": 1000}]
        loaded = await store.load(token)
        assert loaded is not None and loaded[0] == {"user_id": "alice"}
        # The token still authorizes a second handshake, end to end.
        again = _ScriptedConnection(_ws_messages())
        await app(_ws_scope(_cookie_headers(token)), again.receive, again.send)
        assert again.sent[1] == {"type": "websocket.send", "text": "secret-data"}
        await app.isr.close()

    _run(run())


def test_asgi_preflight_mutation_is_not_persisted(tmp_path):
    """Session writes made during the pre-flight are dropped: the stored
    data and CSRF token are unchanged after the handshake."""
    _project(tmp_path, SECRET_SOURCE)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = create_app(tmp_path, middleware=[SessionMiddleware(store, secure=False),
                                           _mutating_middleware])

    async def run():
        token = await _save_session(store)
        conn = _ScriptedConnection(_ws_messages())
        await app(_ws_scope(_cookie_headers(token)), conn.receive, conn.send)
        assert conn.sent[1] == {"type": "websocket.send", "text": "secret-data"}
        loaded = await store.load(token)
        assert loaded is not None
        assert loaded[0] == {"user_id": "alice"}
        assert loaded[1] is None  # no CSRF token minted by the pre-flight
        await app.isr.close()

    _run(run())


def test_asgi_preflight_invalidation_is_not_persisted(tmp_path):
    """An invalidation during the pre-flight must not delete the live
    session (public handler, so the pre-flight itself still passes)."""
    _project(tmp_path, PUBLIC_SOURCE)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = create_app(tmp_path, middleware=[SessionMiddleware(store, secure=False),
                                           _invalidating_middleware])

    async def run():
        token = await _save_session(store)
        conn = _ScriptedConnection(_ws_messages())
        await app(_ws_scope(_cookie_headers(token)), conn.receive, conn.send)
        assert conn.sent[1] == {"type": "websocket.send", "text": "public-data"}
        loaded = await store.load(token)
        assert loaded is not None and loaded[0] == {"user_id": "alice"}
        await app.isr.close()

    _run(run())


class _RawHandshake:
    """Minimal raw-socket WebSocket handshake reporting the status line."""

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


def test_axum_preflight_rotation_does_not_destroy_session(tmp_path):
    """Native path: a rotation during the pre-flight still yields 101 +
    secret-data, and the original token still loads afterwards."""
    _project(tmp_path, SECRET_SOURCE)
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    app = create_app(tmp_path, middleware=[SessionMiddleware(store, secure=False),
                                           _rotating_middleware])

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
        loaded = await store.load(token)
        assert loaded is not None and loaded[0] == {"user_id": "alice"}

    _run(run())
