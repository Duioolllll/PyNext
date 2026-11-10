"""S-P1-14/15/16 (ROADMAP): streaming lifetime cap, per-IP body-slot cap
+ trickle protection, and WebSocket idle/lifetime/ping timeouts.

All limits live in the Rust native server; the tests drive them through
``start_native`` with small values so each bound fires in seconds.
"""
import asyncio
import base64
import hashlib
import os
import socket
import struct
import time

import httpx
import pytest

from pynext import Response, StreamingResponse, create_app
from pynext.server import _validate_server_limits, start_native


def _app_dir(tmp_path):
    route = tmp_path / "app" / "api" / "input"
    route.mkdir(parents=True)
    (route / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("GET")\ndef get(): return JSONResponse({"ok": True})\n'
        '@route("POST")\ndef post(): return JSONResponse({"ok": True})\n',
        encoding="utf-8",
    )
    (tmp_path / "app" / "quick").mkdir(parents=True)
    (tmp_path / "app" / "quick" / "route.py").write_text(
        'from pynext import JSONResponse, route\n'
        '@route("GET")\ndef get(): return JSONResponse({"ok": True})\n',
        encoding="utf-8",
    )
    return tmp_path


def _stream_app(tmp_path, handler):
    for name in ("stream", "quick"):
        directory = tmp_path / "app" / name
        directory.mkdir(parents=True)
        (directory / "route.py").write_text(
            'from pynext import Response, route\n'
            '@route("GET")\ndef get(): return Response("ok")\n',
            encoding="utf-8",
        )
    app = create_app(tmp_path)
    app.entries["/stream"]["GET"] = handler
    return app


async def _connect(server):
    host, port = server.address.rsplit(":", 1)
    return await asyncio.open_connection(host, int(port))


async def _get_first_http1_chunk(reader, writer):
    writer.write(b"GET /stream HTTP/1.1\r\nHost: localhost\r\n\r\n")
    await writer.drain()
    headers = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    assert headers.startswith(b"HTTP/1.1 200 ")
    assert b"transfer-encoding: chunked\r\n" in headers.lower()
    size = int((await asyncio.wait_for(reader.readline(), 5)).strip(), 16)
    chunk = await asyncio.wait_for(reader.readexactly(size + 2), 5)
    assert chunk.endswith(b"\r\n")
    return chunk[:-2]


def test_streaming_response_is_cut_off_by_lifetime(tmp_path):
    """S-P1-14: a stream trickling 1 byte inside the 30s per-chunk
    timeout must still end at the total lifetime cap, and its
    execution slot must be freed for the next request."""
    async def run():
        closed = asyncio.Event()

        async def chunks():
            try:
                while True:
                    yield b"x"
                    await asyncio.sleep(0.2)
            finally:
                closed.set()

        async def handler():
            return StreamingResponse(chunks())

        app = _stream_app(tmp_path, handler)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              max_concurrent=1, acquire_timeout_ms=100,
                              streaming_max_lifetime_secs=2)
        reader, writer = await _connect(server)
        try:
            assert await _get_first_http1_chunk(reader, writer) == b"x"
            # The generator is closed by the lifetime cap, well before
            # any 30s per-chunk timeout could fire.
            await asyncio.wait_for(closed.wait(), 8)
            remaining = await asyncio.wait_for(reader.read(), 8)
            assert not remaining.endswith(b"0\r\n\r\n")
            # The freed execution slot serves the next request.
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5) as client:
                assert (await client.get("/quick")).status_code == 200
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_streaming_response_under_lifetime_is_unaffected(tmp_path):
    """S-P1-14: a stream that finishes before the cap ends cleanly
    with the terminating chunk."""
    async def run():
        async def chunks():
            yield b"done"

        async def handler():
            return StreamingResponse(chunks())

        app = _stream_app(tmp_path, handler)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              streaming_max_lifetime_secs=2)
        reader, writer = await _connect(server)
        try:
            assert await _get_first_http1_chunk(reader, writer) == b"done"
            # Clean end of stream: the terminating zero-size chunk.
            # (read-until-EOF would hang: the keep-alive connection
            # stays open after a clean chunked response.)
            line = await asyncio.wait_for(reader.readline(), 5)
            assert line == b"0\r\n", line
        finally:
            writer.close()
            await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_body_slot_per_ip_cap_blocks_single_ip_monopoly(tmp_path):
    """S-P1-15: with body_capacity=2 but body_capacity_per_ip=1, one
    stalled upload from 127.0.0.1 must not let a second upload from
    the same IP take the remaining global slot."""
    async def run():
        app = create_app(_app_dir(tmp_path))
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              body_capacity=2, body_capacity_per_ip=1,
                              body_timeout_ms=60000,
                              body_min_rate_bytes_per_sec=0)
        host, port = server.address.rsplit(":", 1)
        port = int(port)
        try:
            first_reader, first_writer = await asyncio.open_connection(host, port)
            try:
                first_writer.write(b"POST /api/input HTTP/1.1\r\nHost: localhost\r\n"
                                   b"Content-Length: 100000\r\n\r\n0123456789")
                await first_writer.drain()
                # The first upload stalls, holding its (only) per-IP slot.
                await asyncio.sleep(0.3)
                second_reader, second_writer = await asyncio.open_connection(host, port)
                try:
                    second_writer.write(b"POST /api/input HTTP/1.1\r\nHost: localhost\r\n"
                                        b"Content-Length: 1000\r\n\r\n" + b"x" * 1000)
                    await second_writer.drain()
                    status = await asyncio.wait_for(second_reader.readline(), timeout=5)
                    assert status.startswith(b"HTTP/1.1 503 "), status
                finally:
                    second_writer.close()
                    await second_writer.wait_closed()
            finally:
                first_writer.close()
                await first_writer.wait_closed()
            # Once the stalled upload goes away its per-IP slot is freed.
            async with httpx.AsyncClient(base_url=f"http://{server.address}", timeout=5) as client:
                assert (await client.post("/api/input", content=b"x" * 1000)).status_code == 200
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_body_slot_per_ip_cap_allows_up_to_the_cap(tmp_path):
    """S-P1-15: the per-IP cap is a cap, not a ban — two concurrent
    uploads from one IP succeed with body_capacity_per_ip=2, while a
    third is refused with 503."""
    async def run():
        app = create_app(_app_dir(tmp_path))
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              body_capacity=4, body_capacity_per_ip=2,
                              body_timeout_ms=60000,
                              body_min_rate_bytes_per_sec=0)
        host, port = server.address.rsplit(":", 1)
        port = int(port)
        writers = []
        try:
            for _ in range(2):
                reader, writer = await asyncio.open_connection(host, port)
                writers.append(writer)
                writer.write(b"POST /api/input HTTP/1.1\r\nHost: localhost\r\n"
                             b"Content-Length: 100000\r\n\r\n0123456789")
                await writer.drain()
            await asyncio.sleep(0.3)
            third_reader, third_writer = await asyncio.open_connection(host, port)
            try:
                third_writer.write(b"POST /api/input HTTP/1.1\r\nHost: localhost\r\n"
                                   b"Content-Length: 1000\r\n\r\n" + b"x" * 1000)
                await third_writer.drain()
                status = await asyncio.wait_for(third_reader.readline(), timeout=5)
                assert status.startswith(b"HTTP/1.1 503 "), status
            finally:
                third_writer.close()
                await third_writer.wait_closed()
        finally:
            for writer in writers:
                writer.close()
                await writer.wait_closed()
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_trickle_body_below_min_rate_gets_408(tmp_path):
    """S-P1-15: 1 byte every 100 ms (10 B/s) is far below the 1024 B/s
    minimum, but each byte arrives before the 250 ms stall check would
    run — the per-frame cumulative check must still reject it with a
    408 shortly after the 2s grace window, not at the 10s body timeout."""
    async def run():
        app = create_app(_app_dir(tmp_path))
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              body_min_rate_bytes_per_sec=1024)
        host, port = server.address.rsplit(":", 1)
        try:
            reader, writer = await asyncio.open_connection(host, int(port))
            try:
                writer.write(b"POST /api/input HTTP/1.1\r\nHost: localhost\r\n"
                             b"Content-Length: 100000\r\n\r\n0123456789")
                await writer.drain()
                started = time.monotonic()
                first = b""
                for _ in range(40):
                    await asyncio.sleep(0.1)
                    writer.write(b"x")
                    await writer.drain()
                    try:
                        first = await asyncio.wait_for(reader.readline(), timeout=0.05)
                        break
                    except TimeoutError:
                        continue
                else:
                    first = await asyncio.wait_for(reader.readline(), timeout=8)
                elapsed = time.monotonic() - started
                assert first.startswith(b"HTTP/1.1 408 "), first
                assert elapsed < 8, elapsed
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


class _RawWSClient:
    """Minimal blocking WebSocket client (RFC 6455), no auto-pong:
    the caller decides whether pings are answered."""

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, address, path, timeout=5):
        host, port = address.split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self.sock.settimeout(timeout)
        self.key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {address}\r\nOrigin: http://{address}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {self.key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
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

    def _read_exact(self, count):
        data, self._pending = self._pending[:count], self._pending[count:]
        while len(data) < count:
            chunk = self.sock.recv(count - len(data))
            if not chunk:
                raise AssertionError("connection closed while reading a frame")
            data += chunk
        return data

    def recv(self):
        """Return (opcode, payload) for one complete frame."""
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
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(self._read_exact(length)))
        else:
            payload = self._read_exact(length)
        assert fin, "fragmented frames are not expected in these tests"
        return opcode, payload

    def shutdown(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _ws_app(tmp_path):
    """Minimal echo WebSocket app in a temp dir (no islands, so no
    esbuild is needed to build it)."""
    ws_dir = tmp_path / "app" / "ws"
    ws_dir.mkdir(parents=True)
    (ws_dir / "route.py").write_text(
        "from pynext import WebSocket, WebSocketDisconnect, websocket\n"
        "@websocket\n"
        "async def handler(websocket: WebSocket):\n"
        "    await websocket.accept()\n"
        "    try:\n"
        "        while True:\n"
        "            kind, data = await websocket.receive()\n"
        "            if kind == 'text':\n"
        "                await websocket.send_text(data)\n"
        "            else:\n"
        "                await websocket.send_bytes(data)\n"
        "    except WebSocketDisconnect:\n"
        "        pass\n"
    )
    return create_app(tmp_path)

def _ws_close(address, path="/ws", *, send_interval=None, reply_ping=True,
              timeout=20):
    """Connect, optionally send text every ``send_interval`` seconds and
    answer pings, and return (close code, reason, seconds until close)."""
    client = _RawWSClient(address, path, timeout=2)
    started = time.monotonic()
    last_send = started
    try:
        while time.monotonic() - started < timeout:
            try:
                opcode, payload = client.recv()
            except socket.timeout:
                if send_interval is not None and time.monotonic() - last_send >= send_interval:
                    client.send_text("keepalive")
                    last_send = time.monotonic()
                continue
            if opcode == 9:  # ping
                if reply_ping:
                    client.send(10, payload)
                continue
            if opcode == 8:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
                return code, payload[2:].decode("utf-8", "replace"), time.monotonic() - started
        raise AssertionError("no close frame within timeout")
    finally:
        client.shutdown()


def test_idle_websocket_is_closed_by_idle_timeout(tmp_path):
    """S-P1-16: an idle WebSocket session is closed with 1000 'idle
    timeout' once the idle timeout passes (pings and lifetime disabled
    so only the idle timeout can fire)."""
    async def run():
        app = _ws_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              ws_idle_timeout_secs=2, ws_max_lifetime_secs=0,
                              ws_ping_interval_secs=0)
        try:
            code, reason, elapsed = await asyncio.to_thread(
                _ws_close, server.address, timeout=15)
            assert code == 1000, (code, reason)
            assert reason == "idle timeout", reason
            assert elapsed < 10, elapsed
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_websocket_max_lifetime_closes_active_session(tmp_path):
    """S-P1-16: a session that keeps sending messages (so it is never
    idle) is still closed at its maximum lifetime."""
    async def run():
        app = _ws_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              ws_idle_timeout_secs=0, ws_max_lifetime_secs=3,
                              ws_ping_interval_secs=0)
        try:
            code, reason, elapsed = await asyncio.to_thread(
                _ws_close, server.address, send_interval=1, timeout=15)
            assert code == 1000, (code, reason)
            assert reason == "session lifetime exceeded", reason
            assert elapsed < 10, elapsed
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_websocket_unanswered_ping_closes_session(tmp_path):
    """S-P1-16: a peer that never answers the server's pings is closed
    with 1011 'ping timeout' after the 10s pong grace."""
    async def run():
        app = _ws_app(tmp_path)
        server = start_native(app, asyncio.get_running_loop(), port=0,
                              ws_idle_timeout_secs=300, ws_max_lifetime_secs=0,
                              ws_ping_interval_secs=1)
        try:
            code, reason, elapsed = await asyncio.to_thread(
                _ws_close, server.address, reply_ping=False, timeout=25)
            assert code == 1011, (code, reason)
            assert reason == "ping timeout", reason
            assert elapsed < 20, elapsed
        finally:
            await asyncio.to_thread(server.stop)
            await app.isr.close()
    asyncio.run(run())


def test_server_rejects_out_of_range_dos_limits():
    """New limits are validated like the other server limits."""
    base = (64, 1000, None, 16, 10000, 1024, 128, 10000, 1024, 30.0)
    with pytest.raises(ValueError, match="body_capacity_per_ip"):
        _validate_server_limits(*base, 300, 0, 300, 3600, 60)
    with pytest.raises(ValueError, match="streaming_max_lifetime_secs"):
        _validate_server_limits(*base, -1, 4, 300, 3600, 60)
    with pytest.raises(ValueError, match="ws_idle_timeout_secs"):
        _validate_server_limits(*base, 300, 4, -1, 3600, 60)
