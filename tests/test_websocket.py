"""WebSocket coverage: ASGI behaviour plus the Axum upgrade path.

The Axum checks use a minimal raw-socket WebSocket client (handshake plus
masked frames) so the suite needs no extra client dependency.
"""
import asyncio
import array
import base64
import hashlib
import os
import socket
import struct
from pathlib import Path

import pytest

from pynext import MAX_WS_MESSAGE, create_app
from pynext.server import start_native

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "basic"
SAME_ORIGIN_HEADERS = [(b"host", b"localhost"), (b"origin", b"http://localhost")]


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


def test_asgi_echo_text_and_binary_round_trip():
    async def run():
        app = create_app(EXAMPLE)
        conn = _ScriptedConnection([
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": "hello"},
            {"type": "websocket.receive", "bytes": b"\x00\x01\x02"},
            {"type": "websocket.disconnect", "code": 1000},
        ])
        await app({"type": "websocket", "path": "/ws/echo", "headers": SAME_ORIGIN_HEADERS,
                   "query_string": b""}, conn.receive, conn.send)
        assert conn.sent == [
            {"type": "websocket.accept"},
            {"type": "websocket.send", "text": "hello"},
            {"type": "websocket.send", "bytes": b"\x00\x01\x02"},
        ]
        await app.isr.close()

    _run(run())


def test_asgi_peer_close_ends_handler_and_oversized_message():
    async def run():
        app = create_app(EXAMPLE)
        conn = _ScriptedConnection([
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": "bye"},
            {"type": "websocket.disconnect", "code": 1000},
        ])
        # The echo handler never closes on its own, so the peer closing must
        # end the handler without the server sending anything more.
        await app({"type": "websocket", "path": "/ws/echo", "headers": SAME_ORIGIN_HEADERS,
                   "query_string": b""}, conn.receive, conn.send)
        assert conn.sent == [
            {"type": "websocket.accept"},
            {"type": "websocket.send", "text": "bye"},
        ]
        await app.isr.close()

    _run(run())
    # Oversized messages are refused with close code 1009.
    async def run_oversized():
        app = create_app(EXAMPLE)
        conn = _ScriptedConnection([
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": "x" * (MAX_WS_MESSAGE + 1)},
        ])
        await app({"type": "websocket", "path": "/ws/echo", "headers": SAME_ORIGIN_HEADERS,
                   "query_string": b""}, conn.receive, conn.send)
        assert conn.sent == [
            {"type": "websocket.accept"},
            {"type": "websocket.close", "code": 1009, "reason": "message too large"},
        ]
        await app.isr.close()

    _run(run_oversized())


def _tmp_ws_app(tmp_path, source):
    ws_dir = tmp_path / "app" / "ws"
    ws_dir.mkdir(parents=True)
    (ws_dir / "route.py").write_text(source)
    return create_app(tmp_path)


def test_asgi_disconnect_runs_handler_cleanup(tmp_path):
    async def run():
        source = (
            "from pynext import WebSocket, WebSocketDisconnect, websocket\n"
            "cleanup_log = []\n"
            "@websocket\n"
            "async def handler(websocket: WebSocket):\n"
            "    await websocket.accept()\n"
            "    try:\n"
            "        await websocket.receive()\n"
            "    except WebSocketDisconnect:\n"
            "        cleanup_log.append('disconnected')\n"
            "    finally:\n"
            "        cleanup_log.append('cleaned-up')\n"
        )
        app = _tmp_ws_app(tmp_path, source)
        module = app._module("app/ws/route.py")
        conn = _ScriptedConnection([
            {"type": "websocket.connect"},
            {"type": "websocket.disconnect", "code": 1000},
        ])
        await app({"type": "websocket", "path": "/ws", "headers": SAME_ORIGIN_HEADERS,
                   "query_string": b""}, conn.receive, conn.send)
        assert module.cleanup_log == ["disconnected", "cleaned-up"]
        assert conn.sent == [{"type": "websocket.accept"}]
        await app.isr.close()

    _run(run())


def test_asgi_handler_returning_without_accept_closes(tmp_path):
    async def run():
        source = (
            "from pynext import websocket\n"
            "@websocket\n"
            "async def handler(websocket):\n"
            "    return\n"
        )
        app = _tmp_ws_app(tmp_path, source)
        conn = _ScriptedConnection([{"type": "websocket.connect"}])
        await app({"type": "websocket", "path": "/ws", "headers": SAME_ORIGIN_HEADERS,
                   "query_string": b""}, conn.receive, conn.send)
        assert conn.sent == [{"type": "websocket.close", "code": 1000}]
        await app.isr.close()

    _run(run())


def test_websocket_handler_must_be_async_and_unique(tmp_path):
    ws_dir = tmp_path / "app" / "ws"
    ws_dir.mkdir(parents=True)
    (ws_dir / "route.py").write_text(
        "from pynext import websocket\n"
        "@websocket\n"
        "def handler(websocket):\n"
        "    pass\n"
    )
    with pytest.raises(ValueError, match="must be async"):
        create_app(tmp_path)


class _RawWSClient:
    """Minimal WebSocket client over a plain socket (RFC 6455)."""

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, address, path, protocols=()):
        host, port = address.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=5)
        self.sock.settimeout(5)
        self.key = base64.b64encode(os.urandom(16)).decode("ascii")
        protocol_header = f"Sec-WebSocket-Protocol: {', '.join(protocols)}\r\n" if protocols else ""
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {address}\r\nOrigin: http://{address}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {self.key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n{protocol_header}\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise AssertionError("handshake closed early")
            response += chunk
        head, _, self._pending = response.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 101"), head
        selected = [line.split(b":", 1)[1].strip().decode("ascii") for line in head.split(b"\r\n")
                    if line.lower().startswith(b"sec-websocket-protocol:")]
        self.protocol = selected[0] if selected else None
        expected = base64.b64encode(
            hashlib.sha1((self.key + self.GUID).encode("ascii")).digest()).decode("ascii")
        accept = [line.split(b":", 1)[1].strip() for line in head.split(b"\r\n")
                  if line.lower().startswith(b"sec-websocket-accept:")]
        assert accept == [expected.encode("ascii")], head

    def send(self, opcode, payload):
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_text(self, text):
        self.send(1, text.encode("utf-8"))

    def send_bytes(self, data):
        self.send(2, bytes(data))

    def _read_exact(self, count):
        # The server may send its first frame in the same TCP read as the
        # upgrade response, especially when a handler sends immediately.
        data, self._pending = self._pending[:count], self._pending[count:]
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise AssertionError("connection closed while reading a frame")
            data += chunk
        return data

    def recv(self):
        """Return (opcode, payload) for one complete message."""
        payload = b""
        while True:
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
            if opcode == 9:  # ping: answer with pong
                self.send(10, chunk)
                continue
            if opcode == 0:
                payload += chunk
            else:
                payload = chunk
                message_opcode = opcode
            if fin:
                return message_opcode, payload

    def close(self, code=1000, reason=b""):
        self.send(8, struct.pack("!H", code) + reason)

    def shutdown(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _axum_client_sequence(address):
    """Blocking raw-socket WebSocket checks, run in a worker thread.

    The client blocks on socket reads, so it must not run on the same thread
    as the app's event loop: the Python handler only progresses while the
    loop runs.
    """
    results = {}
    client = _RawWSClient(address, "/ws/echo")
    try:
        client.send_text("hello axum")
        results["text"] = client.recv()
        client.send_bytes(b"\x00\xff")
        results["bytes"] = client.recv()
        client.close()
        results["close"] = client.recv()
    finally:
        client.shutdown()
    # An oversized message is refused with close code 1009.
    client = _RawWSClient(address, "/ws/echo")
    try:
        client.send_text("x" * (MAX_WS_MESSAGE + 1))
        results["oversized"] = client.recv()
    finally:
        client.shutdown()
    return results


def test_axum_websocket_echo_and_oversized_close():
    async def run():
        app = create_app(EXAMPLE)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            results = await asyncio.to_thread(_axum_client_sequence, server.address)
            assert results["text"] == (1, b"hello axum")
            assert results["bytes"] == (2, b"\x00\xff")
            assert results["close"][0] == 8
            opcode, payload = results["oversized"]
            assert opcode == 8 and struct.unpack("!H", payload[:2])[0] == 1009
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    _run(run())


def test_websocket_subprotocol_and_configured_message_limit(tmp_path):
    ws_dir = tmp_path / "app" / "ws"
    ws_dir.mkdir(parents=True)
    (ws_dir / "route.py").write_text(
        'from pynext import websocket\n@websocket(protocols=("fast", "slow"))\n'
        'async def handler(websocket):\n'
        '    await websocket.accept()\n'
        '    await websocket.send_text(websocket.subprotocol or "none")\n'
        '    await websocket.receive()\n'
    )

    async def run():
        app = create_app(tmp_path, websocket_max_message=4)
        conn = _ScriptedConnection([{"type": "websocket.connect"},
                                    {"type": "websocket.receive", "text": "12345"}])
        await app({"type": "websocket", "path": "/ws", "headers": SAME_ORIGIN_HEADERS, "query_string": b"",
                   "subprotocols": ["slow", "fast"]}, conn.receive, conn.send)
        assert conn.sent == [{"type": "websocket.accept", "subprotocol": "fast"},
                             {"type": "websocket.send", "text": "fast"},
                             {"type": "websocket.close", "code": 1009, "reason": "message too large"}]
        denied = _ScriptedConnection([{"type": "websocket.connect"}])
        await app({"type": "websocket", "path": "/ws", "headers": SAME_ORIGIN_HEADERS, "query_string": b"",
                   "subprotocols": ["unsupported"]}, denied.receive, denied.send)
        assert denied.sent == [{"type": "websocket.close", "code": 1008}]
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            def check():
                client = _RawWSClient(server.address, "/ws", ["slow", "fast"])
                try:
                    assert client.protocol == "fast"
                    assert client.recv() == (1, b"fast")
                    client.send_text("12345")
                    opcode, payload = client.recv()
                    assert opcode == 8 and struct.unpack("!H", payload[:2])[0] == 1009
                finally:
                    client.shutdown()
                with pytest.raises(AssertionError, match="HTTP/1.1 400"):
                    _RawWSClient(server.address, "/ws", ["unsupported"])
            await asyncio.to_thread(check)
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    _run(run())


def test_websocket_binary_limit_counts_bytes_in_memoryviews():
    from pynext import WebSocket

    async def run():
        async def send(_item):
            pass
        ws = WebSocket(receive=None, send=send, path="/", max_message_bytes=4)
        await ws.accept()
        with pytest.raises(ValueError, match="configured limit"):
            await ws.send_bytes(memoryview(array.array("I", [1, 2])))
    _run(run())


def test_native_websocket_bounds_slow_receiver_queue(tmp_path):
    ws_dir = tmp_path / "app" / "ws"
    ws_dir.mkdir(parents=True)
    (ws_dir / "route.py").write_text(
        "import asyncio\nfrom pynext import websocket\n@websocket\n"
        "async def handler(websocket):\n    await websocket.accept()\n"
        "    await asyncio.sleep(30)\n"
    )

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            def check():
                client = _RawWSClient(server.address, "/ws")
                try:
                    for _ in range(40):
                        client.send_text("x")
                    opcode, payload = client.recv()
                    assert opcode == 8 and struct.unpack("!H", payload[:2])[0] == 1013
                finally:
                    client.shutdown()
            await asyncio.to_thread(check)
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    _run(run())


def test_websocket_pubsub_fanout_native(tmp_path):
    ws_dir = tmp_path / "app" / "ws"
    ws_dir.mkdir(parents=True)
    (ws_dir / "route.py").write_text(
        "from pynext import websocket, WebSocketPubSub\n"
        "broker = WebSocketPubSub()\n@websocket\n"
        "async def handler(websocket):\n"
        "    async with broker.subscribe('news') as subscription:\n"
        "        await websocket.accept()\n"
        "        await websocket.send_text('ready')\n"
        "        while True:\n"
        "            await websocket.send_text(await subscription.receive())\n"
    )

    async def run():
        app = create_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0)
        clients = []
        try:
            for _ in range(2):
                client = await asyncio.to_thread(_RawWSClient, server.address, "/ws")
                clients.append(client)
                assert await asyncio.to_thread(client.recv) == (1, b"ready")
            broker = app._module("app/ws/route.py").broker
            assert await broker.publish("news", "hello") == 2
            for client in clients:
                assert await asyncio.to_thread(client.recv) == (1, b"hello")
        finally:
            for client in clients:
                client.shutdown()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    _run(run())


def test_dynamic_websocket_route_matches_asgi_and_native(tmp_path):
    dynamic = tmp_path / "app" / "ws" / "[room]"
    dynamic.mkdir(parents=True)
    (dynamic / "route.py").write_text(
        "from pynext import websocket\n"
        "@websocket\n"
        "async def handler(websocket, params):\n"
        "    await websocket.accept()\n"
        "    await websocket.send_text(params['room'])\n"
    )
    static = tmp_path / "app" / "ws" / "reserved"
    static.mkdir()
    (static / "route.py").write_text(
        "from pynext import route, Response\n"
        "@route('GET')\n"
        "def handler():\n"
        "    return Response('ordinary HTTP route')\n"
    )
    catch_all = tmp_path / "app" / "events" / "[[...parts]]"
    catch_all.mkdir(parents=True)
    (catch_all / "route.py").write_text(
        "from pynext import websocket\n"
        "@websocket\n"
        "async def handler(websocket, params):\n"
        "    await websocket.accept()\n"
        "    await websocket.send_text('/'.join(params['parts']))\n"
    )

    async def run():
        app = create_app(tmp_path)
        conn = _ScriptedConnection([{"type": "websocket.connect"}])
        await app({"type": "websocket", "path": "/ws/abc", "headers": SAME_ORIGIN_HEADERS,
                   "query_string": b""}, conn.receive, conn.send)
        assert conn.sent[:2] == [{"type": "websocket.accept"},
                                 {"type": "websocket.send", "text": "abc"}]
        for path, expected in (("/events", ""), ("/events/one/two", "one/two")):
            catch_conn = _ScriptedConnection([{"type": "websocket.connect"}])
            await app({"type": "websocket", "path": path, "headers": SAME_ORIGIN_HEADERS,
                       "query_string": b""}, catch_conn.receive, catch_conn.send)
            assert catch_conn.sent[:2] == [{"type": "websocket.accept"},
                                           {"type": "websocket.send", "text": expected}]
        reserved = _ScriptedConnection([{"type": "websocket.connect"}])
        await app({"type": "websocket", "path": "/ws/reserved", "headers": SAME_ORIGIN_HEADERS,
                   "query_string": b""}, reserved.receive, reserved.send)
        assert reserved.sent == [{"type": "websocket.close", "code": 1008}]
        server = start_native(app, asyncio.get_running_loop(), port=0)
        try:
            def client_checks():
                client = _RawWSClient(server.address, "/ws/abc")
                try:
                    assert client.recv() == (1, b"abc")
                finally:
                    client.shutdown()
                for path, expected in (("/events", b""), ("/events/one/two", b"one/two"),
                                       ("/ws/%E2%9C%93", "✓".encode())):
                    client = _RawWSClient(server.address, path)
                    try:
                        assert client.recv() == (1, expected)
                    finally:
                        client.shutdown()
                with pytest.raises(AssertionError, match="HTTP/1.1 404"):
                    _RawWSClient(server.address, "/ws/reserved")

            await asyncio.to_thread(client_checks)
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()

    _run(run())
